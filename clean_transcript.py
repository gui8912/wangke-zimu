#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
授课录音文稿清洗引擎 v2
========================
输入：SRT 字幕 / 纯文本 / Markdown
输出：清洗后文稿 + 逐条变更报告（可人工审核、可回滚）

两类规则：
  groups —— 五类填充词，可直接删（组合填充 / 口头冗余 / 语气词 / 句首填充 / 句尾确认）
  guards —— 七组要判断的，多数只出「建议改写」不动原文（自称 / 指代 / 转折 / 约量 / 解释 / 引导）

设计原则：宁可少删，不可误删。凡是要判断的，宁可留原词加一条建议，也不擅自改。

用法：
    python clean_transcript.py -i out/lesson.srt
    python clean_transcript.py -i out/lesson.srt --no-repeat
"""

import argparse
import functools
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
RULES = BASE / "rules" / "filler_words.json"
CORRECTIONS = BASE / "terms" / "corrections.txt"

# 【新增 v1.4】受限 ITN：只转明确计量语境的中文数字，条款号与监管术语不动
try:
    import itn_zh as ITN
except Exception:            # 缺模块时不影响原有清洗
    ITN = None

# 【新增 v1.5】术语修复：简称膨胀 + 漏字补全
# 术语表与简称表按文件 mtime 缓存，避免每句都读盘，又能在用户
# 直接编辑 terms/*.txt 后（不必重启服务）于下一次清理时自动生效。
try:
    import termfix as TF
    _TERMS = TF.load_terms(BASE / "terms" / "hotwords.txt")
    _ALIASES = TF.load_aliases(BASE / "terms" / "aliases.txt")
except Exception:            # 缺模块或缺文件时不影响原有清洗
    TF = None
    _TERMS, _ALIASES = [], []

_GLOSSARY_CACHE = {"key": None, "terms": [], "aliases": []}


def _file_key(path: Path):
    """用于判断词表是否被改过；文件不存在也返回稳定值。"""
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), 0, 0)


def load_glossary(force: bool = False):
    """【新增 v2.7】读取热词与简称，按 mtime 缓存。

    为什么不再只靠模块级常量：用户多年来习惯直接用记事本改 terms/*.txt，
    旧实现只在服务启动时读一次，改完必须重启（或走界面保存触发 reload），
    表现就是「术语表改了、重新清理却没反应」。现在每次构造 Cleaner
    都核对一次 mtime，外部编辑立即生效；文件没变时零读盘开销。
    """
    key = (_file_key(BASE / "terms" / "hotwords.txt"),
           _file_key(BASE / "terms" / "aliases.txt"))
    if not force and _GLOSSARY_CACHE["key"] == key:
        return _GLOSSARY_CACHE["terms"], _GLOSSARY_CACHE["aliases"]
    terms = TF.load_terms(BASE / "terms" / "hotwords.txt") if TF else []
    aliases = TF.load_aliases(BASE / "terms" / "aliases.txt") if TF else []
    _GLOSSARY_CACHE.update(key=key, terms=terms, aliases=aliases)
    return terms, aliases


# 句首 / 句尾判定用的标点集合
_PUNCT = "，。！？；：、,.;:?!…"
_HEAD_RE = r"(^|[%s\s])" % re.escape(_PUNCT)
_TAIL_RE = r"(?=[%s\s]|$)" % re.escape(_PUNCT)


# ────────────────────────── 配置加载 ──────────────────────────

def load_rules(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_corrections(path: Path) -> list:
    """读取 `错误=>正确` 映射，跳过注释与空行"""
    pairs = []
    if not path.exists():
        return pairs
    # 【修改 v2.7】兼容记事本另存的 GBK；UTF-8 仍是首选
    text = (TF.read_text_any(path) if TF is not None
            else path.read_text(encoding="utf-8-sig"))
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        wrong, right = line.split("=>", 1)
        wrong, right = wrong.strip(), right.strip()
        if wrong and right and wrong != right:
            pairs.append((wrong, right))
    # 长词优先，避免「就是说」被「就是」截断
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _legacy_groups(cfg: dict) -> dict:
    """兼容 v1 的三层结构（level1/2/3），避免旧配置直接失效"""
    return {
        "语气词": {"mode": "always",
                   "words": list(cfg.get("level1_pure_filler", []))},
        "句首填充": {"mode": "sentence_start",
                     "words": list(cfg.get("level2_pause_word", []))},
        "组合填充": {"mode": "phrase_first",
                     "words": list(cfg.get("level3_filler_phrase", []))},
    }


@functools.lru_cache(maxsize=8192)
def _first_pos(snippet: str) -> str:
    """取片段第一个实词的词性，用于判断「这个/那个」后面是不是名词"""
    if not snippet.strip():
        return ""
    try:
        import jieba.posseg as pseg
        for w, f in pseg.cut(snippet):
            if w.strip():
                return f
    except Exception:
        pass
    return ""


# ────────────────────────── 清洗核心 ──────────────────────────

class Cleaner:
    def __init__(self, cfg: dict, corrections: list, strict: bool = False,
                 itn: bool = True, termfix: bool = True,
                 termfix_auto: bool = True):
        self.cfg = cfg
        self.corrections = corrections
        self.strict = strict
        # 【新增 v1.4】数字规整开关；只影响「百分之九十八点五→98.5%」这类
        # 明确计量表述，条款号（第一百二十条）与监管术语（四不两直）永不改动
        self.itn = itn
        # 【新增 v1.5】术语修复：简称膨胀 + 漏字补全
        # termfix_auto=True 时，强上下文（依据/根据/《/第X条）自动改，
        # 弱上下文只进建议；False 则全部只建议，一个字都不动
        self.termfix = termfix
        self.termfix_auto = termfix_auto
        # 【新增 v2.7】构造时取一次术语/简称（按 mtime 缓存）。
        # 这样「改完术语表 → 点重新清理」一定用的是新词，不依赖服务重启。
        self.terms, self.aliases = load_glossary() if termfix else ([], [])
        self.protect = [re.compile(p) for p in cfg.get("protect_patterns", [])]
        self.warn_ratio = cfg.get("warn_shrink_ratio", 0.5)

        self.groups = cfg.get("groups") or _legacy_groups(cfg)
        self.guards = cfg.get("guards", {})
        self.order = [g for g in (cfg.get("process_order") or list(self.groups))
                      if g in self.groups]

        # 对外暴露：本轮产生的改写建议（单位自称、含数据、解释功能等）
        self.last_suggestions = []
        # 引导语的跨句计数（同一篇里反复出现才删）
        self.seen_guide = {}

        self._compile()

    # ---------- 正则预编译 ----------

    def _compile(self):
        # 前缀保护：介词后面的「这个」是介词宾语，删掉会让介词悬空
        # 「于」覆盖 关于/对于/至于/由于
        self.prep_guard = r"(?<![把被对给向跟与和于])"

        self.g_phrase = {}       # 组合填充 / 口头冗余：整段删
        self.g_always = []       # 语气词：直接 replace
        self.g_solo = {}         # 【新增 v1.4】单字语气词：仅独立出现才删
        self.g_tail = {}         # 语气词：仅句尾
        self.g_head = {}         # 句首填充
        self.g_loose = {}        # 这个/那个 后接标点即删
        self.g_end = {}          # 句尾确认

        for name, spec in self.groups.items():
            words = sorted(spec.get("words", []), key=len, reverse=True)
            mode = spec.get("mode", "phrase")
            if mode in ("phrase", "phrase_first"):
                punct_only = set(spec.get("punct_only", []))
                for w in words:
                    if w in punct_only:
                        # 可能被当成实词的（你看那边），只在后接标点或句末时删
                        self.g_phrase[w] = re.compile(
                            self.prep_guard + re.escape(w) + _TAIL_RE + r"[，,、]?")
                    else:
                        self.g_phrase[w] = re.compile(
                            self.prep_guard + re.escape(w) + r"[，,、]?")
            elif mode == "always":
                tails = set(spec.get("tail_only", []))
                # tail_only 里的词只走句尾规则，不能被无条件删掉
                # 【新增 v1.4】standalone_only：会组成常用词的单字语气词，
                # 只在「独立出现」时删 —— 前面是句首/标点/空白，或后面是标点/末尾。
                # 起因：「货值金额」被无条件 replace 成「货值金」（实测复现）。
                # 同类风险词：呢绒/酒吧/哈欠，本版未纳入（药监场景组词率极低），
                # 若遇到同类问题，把该字加进 standalone_only 即可。
                solo = set(spec.get("standalone_only", []))
                for w in sorted(solo, key=len, reverse=True):
                    self.g_solo[w] = re.compile(
                        r"(?:^|(?<=[%s\s]))%s|%s(?=[%s\s]|$)"
                        % (re.escape(_PUNCT), re.escape(w),
                           re.escape(w), re.escape(_PUNCT)))
                self.g_always = [w for w in words
                                 if w not in tails and w not in solo]
                for w in sorted(spec.get("tail_only", []), key=len, reverse=True):
                    unless = "".join("(?<!%s)" % re.escape(c)
                                     for c in spec.get("tail_unless_prev", []))
                    # 不吃后面的标点：「今天下午呢，我们」→「今天下午，我们」
                    self.g_tail[w] = re.compile(unless + re.escape(w) + _TAIL_RE)
            elif mode == "sentence_start":
                for w in words:
                    self.g_head[w] = re.compile(
                        _HEAD_RE + re.escape(w) + _TAIL_RE)
            elif mode == "sentence_end":
                for w in words:
                    self.g_end[w] = re.compile(
                        re.escape(w) + r"(?=[%s]|$)" % re.escape(_PUNCT))

        # 句首填充里需要看后接成分是不是名词的
        gs = self.groups.get("句首填充", {})
        self.referential = set(gs.get("referential", []))
        self.noun_pos = set(gs.get("noun_pos", []))
        # 「这个/那个」后接逗号顿号：不论位置一律删（后接标点=没有修饰对象）
        # 顺带吃掉紧跟的「就是」：「讲一下这个，就是药品零售企业」→「讲一下药品零售企业」
        for w in gs.get("loose_referential", []):
            self.g_loose[w] = re.compile(
                self.prep_guard + re.escape(w) + r"[，,、](?:就是说|就是)?")
        # 纯虚词（就是说）：句首或停顿后直接删，不要求后接标点
        self.g_head_bare = {
            w: re.compile(_HEAD_RE + re.escape(w))
            for w in gs.get("bare", [])
        }

    # ---------- 主入口 ----------

    def clean(self, text: str) -> tuple:
        """返回 (清洗后文本, 变更列表, 告警列表)。建议改写记录在 self.last_suggestions"""
        orig = text
        changes = []
        warnings = []
        self.last_suggestions = []
        self._head_notes = []

        # 1) 术语校正
        for wrong, right in self.corrections:
            if wrong in text:
                n = text.count(wrong)
                text = text.replace(wrong, right)
                changes.append(("术语校正", f"{wrong} → {right}", n))

        # 2) 按声明的顺序处理五类填充词
        for name in self.order:
            if name == "语气词":
                text, n = self._do_always(text)
            elif "phrase" in self.groups[name].get("mode", ""):
                text, n = self._do_phrase(text, name)
            elif self.groups[name].get("mode") == "sentence_start":
                text, n = self._do_head(text, name)
            elif self.groups[name].get("mode") == "sentence_end":
                text, n = self._do_end(text, name)
            else:
                n = 0
            if n:
                changes.append((name, f"×{n}", n))

        # 3) 需要判断的七组
        text = self._guard_self_ref(text, changes)
        text = self._guard_turn(text, changes)
        text = self._guard_quantity(text, changes)
        text = self._guard_explain(text, changes)
        text = self._guard_guide(text, changes)

        # 4) 重复词合并
        if self.cfg.get("repeat_merge", {}).get("enabled", True) and not self.strict:
            merged, n = self._merge_repeat(text)
            if n:
                text = merged
                changes.append(("重复合并", f"{n} 处", n))

        # 【新增 v1.5】4.4) 术语修复：简称膨胀 + 漏字补全
        # 放在填充词与重复合并之后：此时句子已定型，补字的上下文判断才准。
        # 强上下文自动改，弱上下文只进建议（last_suggestions）。
        if self.termfix and TF is not None:
            text, tch, tsg = TF.fix(text, self.terms, self.aliases,
                                    auto=self.termfix_auto)
            if tch:
                changes.extend(tch)
            if tsg:
                self.last_suggestions.extend(tsg)

        # 【新增 v1.4】4.5) 数字规整（受限 ITN）
        # 放在标点收尾之前：先让「百分之九十八点五」变成「98.5%」，
        # 再统一处理逗号句号，避免 % 与标点各自收尾时产生「，%」这类残迹。
        # 只改数字写法，不改字、不删词，故对时间轴与对齐无影响。
        if self.itn and ITN is not None:
            normed, itn_log = ITN.normalize(text)
            if itn_log and normed != text:
                text = normed
                changes.append(("数字规整", f"×{len(itn_log)}", len(itn_log)))

        # 5) 标点与空白收尾
        text = self._tidy_punct(text)

        # 6) 保护项校验
        for pat in self.protect:
            for m in pat.finditer(orig):
                token = m.group()
                if token not in text:
                    warnings.append(f"保护词丢失：「{token}」")

        # 7) 收缩比例告警
        if orig and len(text) / max(len(orig), 1) < self.warn_ratio:
            warnings.append(
                f"文本长度收缩 {100 * (1 - len(text) / max(len(orig), 1)):.0f}%，请人工复核"
            )

        return text, changes, warnings

    # ---------- 五类填充词 ----------

    def _do_always(self, text: str) -> tuple:
        """语气词：无条件删；末尾叹词类只在句尾删"""
        total = 0
        for w in self.g_always:
            if w in text:
                n = text.count(w)
                text = text.replace(w, "")
                total += n
        # 【新增 v1.4】standalone_only：仅独立出现才删，保护词内字（金额/额度/余额）
        for w, pat in self.g_solo.items():
            text, n = pat.subn("", text)
            total += n
        for w, pat in self.g_tail.items():
            text, n = pat.subn("", text)
            total += n
        return text, total

    def _do_phrase(self, text: str, name: str) -> tuple:
        """整段填充语：长词优先，带条件的看上下文"""
        total = 0
        cond = self.groups[name].get("conditional", {})
        for w in sorted(self.groups[name].get("words", []), key=len, reverse=True):
            if w not in text:
                continue
            rule = cond.get(w)
            if rule:
                text, n = self._phrase_conditional(text, w, rule)
            else:
                text, n = self.g_phrase[w].subn("", text)
            total += n
        return text, total

    def _phrase_conditional(self, text: str, w: str, rule: dict) -> tuple:
        """「所以说」「这样的话」这类要看前后文才决定删不删"""
        total = 0
        keep_before = rule.get("keep_if_before", [])
        keep_after = rule.get("keep_if_after", [])
        look_back = rule.get("lookback", 24)
        look_fwd = rule.get("lookahead", 3)
        note = rule.get("when_keep", "保留")

        pat = self.g_phrase[w]
        out, last = [], 0
        for m in pat.finditer(text):
            before = text[max(0, m.start() - look_back):m.start()]
            after = text[m.end():m.end() + look_fwd]
            if any(c in before for c in keep_before) or \
                    (keep_after and any(after.startswith(c) for c in keep_after)):
                self.last_suggestions.append(f"「{w}」（{note}）")
                continue                        # 保留原词，不删
            out.append(text[last:m.start()])
            last = m.end()
            total += 1
        if total:
            out.append(text[last:])
            text = "".join(out)
        return text, total

    def _do_head(self, text: str, name: str) -> tuple:
        """句首填充：只在句首/停顿后删；「这个/那个」后接名词时保留"""
        # 先删「这个，」「那个、」——后接标点说明它没有修饰对象，不论位置都删
        total = 0
        for pat in self.g_loose.values():
            text, n = pat.subn("", text)
            total += n

        # 「就是说」这类纯虚词，句首/停顿后直接删，不用等标点
        for pat in self.g_head_bare.values():
            while True:
                m = pat.search(text)
                if not m:
                    break
                text = text[:m.start()] + m.group(1) + text[m.end():]
                total += 1

        for w in sorted(self.groups[name].get("words", []), key=len, reverse=True):
            pat = self.g_head.get(w)
            if not pat:
                continue
            pos = 0
            while True:
                m = pat.search(text, pos)
                if not m:
                    break
                if w in self.referential and self._has_referent(text, m.end()):
                    self._head_notes.append(f"「{w}」后接名词，按指代保留")
                    pos = m.end()          # 跳过这一处，从词尾继续往后找
                    continue
                text = text[:m.start()] + m.group(1) + text[m.end():]
                total += 1
                pos = m.start() + len(m.group(1))   # 删掉后回到原位置继续
        return text, total

    def _has_referent(self, text: str, pos: int) -> bool:
        """判断「这个/那个」后面是不是跟着名词性成分"""
        tail = text[pos:pos + 8].lstrip(" 　")
        if not tail:
            return False
        if tail[0] in _PUNCT or tail[0].isspace():
            return False
        if tail.startswith(("就是", "就是", "呢", "啊", "的话")):
            return False
        pos_tag = _first_pos(tail[:6])
        return bool(pos_tag) and pos_tag[0] in self.noun_pos

    def _do_end(self, text: str, name: str) -> tuple:
        """句尾确认：只在句尾删。

        删掉「对吧/是吧」后，若句尾原来是问号但前面没有疑问词，
        说明疑问语气是「对吧」带来的，要把问号改回句号——
        否则陈述句会变成疑问句。
        """
        total = 0
        tags = ("是不是", "有没有", "是否", "能不能", "要不要", "会不会", "多少", "什么", "怎么")
        for w in sorted(self.groups[name].get("words", []), key=len, reverse=True):
            pat = self.g_end.get(w)
            if not pat:
                continue
            for m in reversed(list(pat.finditer(text))):
                before = text[max(0, m.start() - 12):m.start()]
                after = text[m.end():m.end() + 1]
                text = text[:m.start()] + text[m.end():]
                total += 1
                if after in "？?" and not any(t in before for t in tags):
                    pos = m.start()
                    if pos < len(text) and text[pos] in "？?":
                        text = text[:pos] + "。" + text[pos + 1:]
        if total:
            text = re.sub(r"[，,]\s*$", "。", text)
        return text, total

    # ---------- 七组判别 ----------

    def _guard_self_ref(self, text: str, changes: list) -> str:
        """我们/咱们：单位自称只建议改写；泛称引导删「我们」；其余不动"""
        g = self.guards.get("自称")
        if not g:
            return text
        cues = g.get("official_cues", [])
        leads = g.get("lead_cues", [])
        rewrite = g.get("rewrite", {})
        ahead = g.get("lookahead", 16)
        for w in g.get("words", []):
            hits = list(re.finditer(re.escape(w), text))
            for m in reversed(hits):
                tail = text[m.end():m.end() + ahead]
                if any(c in tail for c in cues):
                    self.last_suggestions.append(
                        f"「{w}」后接「{tail[:8]}」，属单位自称，建议改「{rewrite.get(w, '我局')}」，不能当语气词删")
                    continue
                if any(tail.startswith(c) for c in leads):
                    text = text[:m.start()] + text[m.end():]
                    changes.append(("泛称引导", f"删「{w}」", 1))
        return text

    def _guard_turn(self, text: str, changes: list) -> str:
        """实际上/其实/事实上：后接标点或口头禅时删，表转折强调时留"""
        g = self.guards.get("转折")
        if not g:
            return text
        keep = g.get("keep_cues", [])
        total = 0
        for w in sorted(g.get("words", []), key=len, reverse=True):
            if w not in text:
                continue
            out, last = [], 0
            for m in re.finditer(re.escape(w), text):
                nxt = text[m.end():m.end() + 2].lstrip(" 　")
                if any(nxt.startswith(c) for c in keep):
                    continue                     # 转折/强调用法，保留
                out.append(text[last:m.start()])
                last = m.end()
                total += 1
            if last:
                out.append(text[last:])
                text = "".join(out)
        if total:
            changes.append(("口头禅", f"×{total}", total))
        return text

    def _guard_quantity(self, text: str, changes: list) -> str:
        """基本上/大概/差不多：后跟数字、法条、金额时保留并提醒回听"""
        g = self.guards.get("约量")
        if not g:
            return text
        pats = [re.compile(p) for p in g.get("data_re", [])]
        ahead = g.get("lookahead", 10)
        total = 0
        for w in sorted(g.get("words", []), key=len, reverse=True):
            if w not in text:
                continue
            out, last = [], 0
            for m in re.finditer(re.escape(w), text):
                tail = text[m.end():m.end() + ahead]
                if any(p.search(tail) for p in pats):
                    self.last_suggestions.append(
                        f"「{w}{tail[:8]}」含数据，保留原词并回听核对")
                    continue
                out.append(text[last:m.start()])
                last = m.end()
                total += 1
            if last:
                out.append(text[last:])
                text = "".join(out)
        if total:
            changes.append(("无信息填充", f"×{total}", total))
        return text

    def _guard_explain(self, text: str, changes: list) -> str:
        """应该讲/可以说/换句话说：当填充删，有解释功能只建议改「即」"""
        g = self.guards.get("解释")
        if not g:
            return text
        filler_next = g.get("filler_next", [])
        min_tail = g.get("min_tail", 6)
        total = 0
        for w in sorted(g.get("words", []), key=len, reverse=True):
            if w not in text:
                continue
            out, last = [], 0
            for m in re.finditer(re.escape(w), text):
                rest = text[m.end():].lstrip(" 　，,")
                nxt = rest[:3]
                is_filler = (not rest) or len(rest) < min_tail or \
                    any(nxt.startswith(c) for c in filler_next)
                if not is_filler:
                    self.last_suggestions.append(f"「{w}」有解释功能，建议精简为「即」")
                    continue
                out.append(text[last:m.start()])
                last = m.end()
                total += 1
            if last:
                out.append(text[last:])
                text = "".join(out)
        if total:
            changes.append(("解释性填充", f"×{total}", total))
        return text

    def _guard_guide(self, text: str, changes: list) -> str:
        """大家可以看到/请注意：首次建议精简，超过 repeat_limit 次的删掉"""
        g = self.guards.get("引导")
        if not g:
            return text
        limit = g.get("repeat_limit", 2)
        rewrite = g.get("rewrite", {})
        total = 0
        for w in sorted(g.get("words", []), key=len, reverse=True):
            hits = list(re.finditer(re.escape(w), text))
            if not hits:
                continue
            self.seen_guide[w] = self.seen_guide.get(w, 0) + len(hits)
            if self.seen_guide[w] > limit:
                # 本篇已出现超过上限，本轮出现的一律删
                for m in reversed(hits):
                    text = text[:m.start()] + text[m.end():]
                    total += 1
                self.last_suggestions.append(
                    f"「{w}」本课已出现 {self.seen_guide[w]} 次，重复部分已删")
            else:
                self.last_suggestions.append(
                    f"「{w}」属课件引导句，建议精简为「{rewrite.get(w, '注意')}」")
        if total:
            changes.append(("重复引导语", f"×{total}", total))
        return text

    # ---------- 工具 ----------

    def _merge_repeat(self, text: str) -> tuple:
        """合并口误重复。
        单字重复一律不动（谢谢/看看/慢慢 属正常叠词），
        只处理代词口吃（我我我）与 2-4 字词组重复（我们我们）。
        """
        cfg = self.cfg.get("repeat_merge", {})
        total = 0

        # 先压缩连续标点：删词后会留下「，，，，」，否则会被当成重复单元合并掉
        text = re.sub(r"[，,]{2,}", "，", text)
        text = re.sub(r"^[，,、；;：:\s]+", "", text)
        text = re.sub(r"[，,]\s*([。！？!?])", r"\1", text)

        for ch in cfg.get("single_char_dupes", []):
            pat = re.compile("(?:%s){3,}" % re.escape(ch))
            text, n = pat.subn(ch, text)
            total += n

        for w in cfg.get("words", []):
            pat = re.compile("(?:%s){2,}" % re.escape(w))
            text, n = pat.subn(w, text)
            total += n

        lo = cfg.get("min_unit_len", 2)
        hi = cfg.get("max_unit_len", 4)
        for _ in range(cfg.get("max_rounds", 3)):
            changed = False
            for unit_len in range(hi, lo - 1, -1):
                # 单元内不含标点与空白，避免把「，。，。」当成重复
                pat = re.compile(
                    r"((?:(?![，。！？；：、,.;:?!…\s]).){%d})\1+" % unit_len)
                text, n = pat.subn(r"\1", text)
                if n:
                    changed = True
                    total += n
            if not changed:
                break
        return text, total

    @staticmethod
    def _tidy_punct(text: str) -> str:
        text = re.sub(r"[，,]{2,}", "，", text)
        text = re.sub(r"^[，,、；;：:\s]+", "", text)
        text = re.sub(r"[，,]\s*([。！？!?])", r"\1", text)
        text = re.sub(r"([。！？])\s*[，,、]", r"\1", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()


# ────────────────────────── SRT 解析 ──────────────────────────

def parse_srt(raw: str) -> list:
    """返回 [{'idx':int,'time':str,'lines':[str]}]"""
    blocks = re.split(r"\r?\n\r?\n+", raw.strip())
    cues = []
    for blk in blocks:
        lines = blk.splitlines()
        if len(lines) < 2:
            continue
        time_line_idx = 1 if lines[0].strip().isdigit() else 0
        if time_line_idx >= len(lines) or "-->" not in lines[time_line_idx]:
            continue
        cues.append({
            "idx": len(cues) + 1,
            "time": lines[time_line_idx].strip(),
            "lines": [ln.strip() for ln in lines[time_line_idx + 1:] if ln.strip()],
        })
    return cues


def build_srt(cues: list) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{c['time']}\n{' '.join(c['lines'])}\n")
    return "\n".join(out)


# ────────────────────────── 主流程 ──────────────────────────

def main():
    ap = argparse.ArgumentParser(description="授课文稿清洗")
    ap.add_argument("-i", "--input", required=True, help="输入文件 .srt/.txt/.md")
    ap.add_argument("-o", "--outdir", default=str(BASE / "out"), help="输出目录")
    ap.add_argument("--strict", action="store_true", help="严格模式：关闭重复合并")
    ap.add_argument("--no-repeat", action="store_true", help="同 --strict")
    # 【新增 v1.4】默认开启数字规整；需要保留纯汉字数字时关闭
    ap.add_argument("--no-itn", action="store_true",
                    help="关闭数字规整（保留「百分之九十八点五」原样）")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"找不到输入文件：{src}")

    cleaner = Cleaner(load_rules(RULES), load_corrections(CORRECTIONS),
                      strict=args.strict or args.no_repeat,
                      itn=not args.no_itn)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    is_srt = src.suffix.lower() == ".srt"

    rows = []
    suggestions = []
    total_orig = total_new = 0

    if is_srt:
        cues = parse_srt(src.read_text(encoding="utf-8-sig"))
        kept = []
        for c in cues:
            orig = " ".join(c["lines"])
            new, changes, warns = cleaner.clean(orig)
            for s in cleaner.last_suggestions:
                suggestions.append((c["idx"], s))
            total_orig += len(orig)
            total_new += len(new)
            if not new.strip("，。、；：？！,.;:?! "):
                rows.append((c["idx"], c["time"], orig, "【整条删除：清理后为空】", changes, warns))
                continue
            kept.append({"idx": c["idx"], "time": c["time"], "lines": [new]})
            rows.append((c["idx"], c["time"], orig, new, changes, warns))
        (outdir / f"{stem}.cleaned.srt").write_text(build_srt(kept), encoding="utf-8")
        print(f"SRT：{len(cues)} 条 → 保留 {len(kept)} 条")
    else:
        lines = src.read_text(encoding="utf-8-sig").splitlines()
        out_lines = []
        for i, line in enumerate(lines, 1):
            if not line.strip():
                out_lines.append(line)
                continue
            new, changes, warns = cleaner.clean(line)
            for s in cleaner.last_suggestions:
                suggestions.append((i, s))
            total_orig += len(line)
            total_new += len(new)
            out_lines.append(new)
            if changes or warns:
                rows.append((i, "", line, new, changes, warns))
        (outdir / f"{stem}.cleaned{src.suffix}").write_text("\n".join(out_lines), encoding="utf-8")
        print(f"{src.suffix}：{len(lines)} 行已处理")

    # 变更报告
    rep = [f"# 清洗报告 · {src.name}", ""]
    ratio = 100 * (1 - total_new / max(total_orig, 1))
    rep.append(f"- 原始字数：{total_orig}")
    rep.append(f"- 清洗后字数：{total_new}")
    rep.append(f"- 删除比例：{ratio:.1f}%")
    rep.append(f"- 改动条目：{len(rows)}")
    rep.append(f"- 待人工判断：{len(suggestions)} 条")
    rep.append("")
    rep.append("## 逐条变更")
    rep.append("")
    rep.append("| 序号 | 时间 | 原始文本 | 清洗后 | 变更类型 | 告警 |")
    rep.append("|---|---|---|---|---|---|")
    for idx, tm, orig, new, changes, warns in rows:
        ctypes = "；".join(f"{c[0]}:{c[1]}" for c in changes) or "—"
        w = "；".join(warns) or ""
        esc = lambda s: str(s).replace("|", "\\|")
        rep.append(f"| {idx} | {tm} | {esc(orig)} | {esc(new)} | {esc(ctypes)} | {esc(w)} |")

    if suggestions:
        rep.append("")
        rep.append("## 待人工判断（工具没动原文）")
        rep.append("")
        rep.append("| 序号 | 建议 |")
        rep.append("|---|---|")
        for idx, s in suggestions:
            rep.append(f"| {idx} | {str(s).replace('|', chr(92) + '|')} |")

    (outdir / f"{stem}.report.md").write_text("\n".join(rep), encoding="utf-8")

    print(f"删除比例 {ratio:.1f}%，{len(rows)} 条有改动，{len(suggestions)} 条待人工判断")
    print(f"输出目录：{outdir}")


if __name__ == "__main__":
    main()
