#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
七遍改稿引擎：口播稿 → 阅读稿
==============================
把"五遍法"落到可执行的分遍流程上，每遍只做一件事：

    P1 删填充词     全自动
    P2 分段         全自动（可调密度）
    P3 去绕圈       半自动，人决定留哪句
    P4 语法修复     半自动，人确认每条建议
    P5 书面化替换   半自动，"然后"这类必须按语境选
    P6 事实核对     纯人工，工具只负责把待核项挑出来
    P7 通读朗读     纯人工，工具提供朗读音频

三条硬约束（来自实战经验，写死在代码里）：
1. 原稿永不覆盖，每遍单独存 v0..v7；
2. 不确定的一律不动 —— 只标注、给建议，不擅自改写；
3. 事实保真 —— 本引擎从不生成新的事实、数字、法条名。

对外接口只有一个：

    run_pass(n, text, opts) -> {"text", "issues", "stats", "notes"}
"""

import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
RULES = BASE / "rules" / "filler_words.json"

CJK = re.compile(r"[\u4e00-\u9fff]")
SENT_RE = re.compile(r"[^。！？；!?;]+[。！？；!?;]?")


# ────────────────────────── 基础工具 ──────────────────────────

def cjk_len(s: str) -> int:
    return len(CJK.findall(s))


def load_rules() -> dict:
    with open(RULES, encoding="utf-8") as f:
        return json.load(f)


def lines_of(text: str) -> list:
    """一行一段（本引擎的文本约定）"""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def sentences_with_line(text: str) -> list:
    """→ [(行号, 句子)]"""
    out = []
    for i, ln in enumerate(lines_of(text)):
        for s in SENT_RE.findall(ln):
            s = s.strip()
            if s:
                out.append((i, s))
    return out


try:
    from rapidfuzz import fuzz

    def similar(a: str, b: str) -> float:
        return fuzz.ratio(a, b) / 100.0
except ImportError:                                   # 兜底：字符二元组 Jaccard
    def similar(a: str, b: str) -> float:
        ga = {a[i:i + 2] for i in range(len(a) - 1)}
        gb = {b[i:i + 2] for i in range(len(b) - 1)}
        if not ga or not gb:
            return 0.0
        return len(ga & gb) / len(ga | gb)


def stats(text: str) -> dict:
    ls = lines_of(text)
    return {
        "chars": cjk_len(text),
        "paras": len(ls),
        "sentences": sum(len(SENT_RE.findall(ln)) for ln in ls),
    }


def _issue(pass_no, kind, line, quote, title, detail, options=None, severity="mid"):
    return {
        "pass": pass_no, "kind": kind, "line": line, "quote": quote,
        "title": title, "detail": detail, "options": options or [],
        "severity": severity,
    }


# ────────────────────────── P1 删填充词 ──────────────────────────

def _tidy(s: str) -> str:
    """删除口语词后，把悬空的标点收干净（「齐全，？」→「齐全？」）"""
    s = re.sub(r"[，,、；;：:]+\s*(?=[。！？!?])", "", s)
    s = re.sub(r"([。！？!?])\s*[，,、；;：:]+", r"\1", s)
    s = re.sub(r"[，,]{2,}", "，", s)
    s = re.sub(r"[、]{2,}", "、", s)
    s = re.sub(r"^[，,、；;：:。！？\s]+", "", s)
    s = re.sub(r"\s+([。！？，、])", r"\1", s)
    return s.strip()


def pass1(text: str, opts: dict) -> dict:
    """只删填充词，不动别的。可指定保留某些词的前 N 处（标志性口头语）。"""
    rules = load_rules()
    keep = {k: int(v) for k, v in (opts.get("keep") or {}).items() if int(v) > 0}

    groups = rules.get("groups") or {}
    if not groups:            # 兼容旧的 level1/2/3 结构
        groups = {
            "语气词": {"mode": "always", "words": rules.get("level1_pure_filler", [])},
            "句首填充": {"mode": "sentence_start", "words": rules.get("level2_pause_word", [])},
            "组合填充": {"mode": "phrase", "words": rules.get("level3_filler_phrase", [])},
        }

    always_spec = next((g for g in groups.values() if g.get("mode") == "always"), {})
    head_spec = next((g for g in groups.values() if g.get("mode") == "sentence_start"), {})
    end_spec = next((g for g in groups.values() if g.get("mode") == "sentence_end"), {})

    l_always = list(always_spec.get("words", []))
    l_tail = list(always_spec.get("tail_only", []))
    l_phrase = []
    for g in groups.values():
        if "phrase" in (g.get("mode") or ""):
            l_phrase += list(g.get("words", []))
    l_head = list(head_spec.get("words", []))
    l_bare = list(head_spec.get("bare", []))       # 就是说：不要求后接标点
    l_end = list(end_spec.get("words", []))

    l_phrase.sort(key=len, reverse=True)     # 长词先删，否则「这个就是说」会被拆碎
    l_always.sort(key=len, reverse=True)

    pat_phrase = {w: re.compile(re.escape(w) + r"[，,、]?") for w in l_phrase}
    pat_tail = {w: re.compile(re.escape(w) + r"(?=[，。、；：？！,.;:?!…\s]|$)") for w in l_tail}
    # 句首：允许前面是行首或标点，替换时保留那个标点
    pat_head = {
        w: re.compile(r"(^|[，。、；：？！,.;:?!…\s])" + re.escape(w)
                      + r"(?=[，。、；：？！,.;:?!…\s]|$)") for w in l_head
    }
    pat_bare = {
        w: re.compile(r"(^|[，。、；：？！,.;:?!…\s])" + re.escape(w)) for w in l_bare
    }
    pat_end = {w: re.compile(re.escape(w) + r"(?=[，。、；：？！,.;:?!…]|$)") for w in l_end}

    removed = {}
    kept = {}
    out = []

    for ln in lines_of(text):
        # 先把要保留的出现位置换成占位符，处理完再换回来
        holds = []
        for w, quota in keep.items():
            done = kept.get(w, 0)
            while done < quota:
                idx = ln.find(w)
                if idx < 0:
                    break
                token = f"\ue000{len(holds)}\ue001"
                holds.append((token, w))
                ln = ln[:idx] + token + ln[idx + len(w):]
                done += 1
            kept[w] = done

        def _hit(w, n):
            if n:
                removed[w] = removed.get(w, 0) + n

        for w in l_phrase:                    # 组合填充 / 口头冗余：整段删
            ln, n = pat_phrase[w].subn("", ln)
            _hit(w, n)
        for w in l_end:                       # 句尾确认：必须在语气词之前
            for m in reversed(list(pat_end[w].finditer(ln))):
                before = ln[max(0, m.start() - 12):m.start()]
                after = ln[m.end():m.end() + 1]
                ln = ln[:m.start()] + ln[m.end():]
                _hit(w, 1)
                # 「处理，对吧？」删掉「对吧」后是陈述句，问号要改回句号
                if after in "？?" and not any(
                        t in before for t in ("是不是", "有没有", "是否", "能不能", "要不要")):
                    if m.start() < len(ln) and ln[m.start()] in "？?":
                        ln = ln[:m.start()] + "。" + ln[m.start() + 1:]
        for w in l_always:                    # 语气词：无条件删
            if w in ln:
                n = ln.count(w)
                ln = ln.replace(w, "")
                _hit(w, n)
        for w in l_tail:                      # 句尾叹词：只删句尾
            ln, n = pat_tail[w].subn("", ln)
            _hit(w, n)
        for w in l_bare:                      # 纯虚词：句首即可删
            ln, n = pat_bare[w].subn(r"\1", ln)
            _hit(w, n)
        for w in l_head:                      # 句首填充：只在句首删
            ln, n = pat_head[w].subn(r"\1", ln)
            _hit(w, n)

        for token, w in holds:
            ln = ln.replace(token, w)

        ln = _tidy(ln)
        ln = re.sub(r"\s{2,}", " ", ln)
        if ln and CJK.search(ln):
            out.append(ln)

    total = sum(removed.values())
    top = sorted(removed.items(), key=lambda kv: -kv[1])[:5]
    return {
        "text": "\n".join(out),
        "issues": [],
        "stats": {**stats("\n".join(out)), "removed": total},
        "notes": {
            "removed_by_word": removed,
            "top": [{"word": w, "count": n} for w, n in top],
            "kept_signature": kept,
        },
    }


# ────────────────────────── P2 分段 ──────────────────────────

# 分段的三个信号强度（来自实战：强标记必断，弱标记攒够字数才断）
STRONG = re.compile(
    r"(第一个|第二个|第三个|第四个|第一点|第二点|第三点|第四点|首先|其次|再次|"
    r"另外|还有一点|再一个|下面|接下来|先说|再看|补充一点|关于|这部分)"
)
WEAK = re.compile(r"(那么|这里|至于|然后)")
SUMMARY = re.compile(r"(总的来说|总之|基本上|小结|就这些|最后一点)")


def split_long(sent: str, limit: int = 60) -> list:
    """把一口气说完的长句拆开（只在逗号处断，不硬切字）"""
    if cjk_len(sent) <= limit:
        return [sent]
    body = sent.rstrip("。！？；")
    tail = sent[len(body):]
    parts, buf = [], ""
    for piece in re.split(r"(?<=[，,])", body):
        if not piece:
            continue
        if buf and cjk_len(buf + piece) > limit * 0.8:
            parts.append(buf.rstrip("，,") + "。")
            buf = piece
        else:
            buf += piece
    if buf:
        parts.append(buf)
    if len(parts) > 1:
        parts[-1] = parts[-1].rstrip("，,") + (tail or "")
    return [p for p in parts if p.strip()]


def _should_break(cur: list, ln: str, cap: int) -> bool:
    """判断是否在此处断段。宁可多断一点 —— 一堵墙是最难读的。"""
    if not cur:
        return False
    n = cjk_len("".join(cur))
    prev = cur[-1]

    if STRONG.search(ln[:14]) and n >= 12:
        return True
    if SUMMARY.search(ln[:10]) and n >= 20:
        return True
    if WEAK.search(ln[:6]) and n >= 60:
        return True
    if re.search(r"[？?]\s*$", prev) and n >= 22:      # 一个设问结束，往往就是一小节
        return True
    if n >= cap:
        return True
    if len(cur) >= 3 and similar(prev, ln) < 0.06:      # 话锋转了
        return True
    return False


def pass2(text: str, opts: dict) -> dict:
    """按话题与长句切段。口语本身没有段落，这一步性价比最高。"""
    cap = {"low": 300, "mid": 150, "high": 90}.get(opts.get("density", "mid"), 150)
    paras, cur = [], []
    for ln in lines_of(text):
        if _should_break(cur, ln, cap):
            paras.append("".join(cur))
            cur = []
        cur.append(ln)
    if cur:
        paras.append("".join(cur))

    # 段内长句拆分
    fixed = []
    for p in paras:
        sents = [s.strip() for s in SENT_RE.findall(p) if s.strip()]
        buf = ""
        for s in sents:
            for piece in split_long(s):
                buf += piece
        fixed.append(buf or p)

    result = "\n".join(fixed)
    return {
        "text": result,
        "issues": [],
        "stats": stats(result),
        "notes": {
            "paras": len(fixed),
            "longest": max((cjk_len(p) for p in fixed), default=0),
            "spec": [
                {"para": i + 1, "chars": cjk_len(p), "head": p[:24]}
                for i, p in enumerate(fixed)
            ],
        },
    }


# ────────────────────────── P3 去绕圈 ──────────────────────────

def pass3(text: str, opts: dict) -> dict:
    """找出"同一件事说三遍"，给出对照，由人决定留哪一句。"""
    thr = float(opts.get("threshold", 0.72))
    sents = sentences_with_line(text)
    issues = []
    seen = set()
    for i in range(len(sents)):
        li, si = sents[i]
        for j in range(i + 1, min(i + 5, len(sents))):
            lj, sj = sents[j]
            if (i, j) in seen:
                continue
            if cjk_len(si) < 8 or cjk_len(sj) < 8:
                continue
            if similar(si, sj) < thr:
                continue
            seen.add((i, j))
            longer_is_second = cjk_len(sj) >= cjk_len(si)
            issues.append(_issue(
                3, "repeat", lj, sj,
                "疑似绕圈",
                f"第 {i + 1} 句与第 {j + 1} 句说的是同一件事："
                f"「{si}」／「{sj}」。书面稿只留一句，留最完整、最准确的那句。",
                [
                    {"label": f"删掉这句（留前一句）", "find": sj, "with": ""},
                    {"label": f"删掉前一句（留这句）", "find": si, "with": "", "line": li},
                ],
                severity="high" if similar(si, sj) >= 0.85 else "mid",
            ))
    # 同一行里相邻小句的重复（「就是说我们首先要看证照，就是说第一要看证照」）
    CLAUSE = re.compile(r"[^，,、。！？；!?;]+")
    for ln_no, ln in enumerate(lines_of(text)):
        cls = [c.strip() for c in CLAUSE.findall(ln) if cjk_len(c) >= 8]
        for k in range(len(cls) - 1):
            a, b = cls[k], cls[k + 1]
            if similar(a, b) < 0.80:
                continue
            issues.append(_issue(
                3, "repeat", ln_no, b,
                "小句重复",
                f"同一句话里连着说了两遍：「{a}」／「{b}」。留一句。",
                [{"label": "删掉后一句", "find": b, "with": ""},
                 {"label": "删掉前一句", "find": a, "with": ""}],
                severity="mid"))
    return {
        "text": text,
        "issues": issues,
        "stats": stats(text),
        "notes": {"count": len(issues)},
    }

# ────────────────────────── P4 语法修复 ──────────────────────────

# 量词冗余：有个很重要的一条 → 很重要的一条
RE_QUANT = re.compile(r"有个([^。！？；，]{0,10}?)的(一[条个种件项])")
# 句式杂糅：由于……造成的（两个都要，冲突）
RE_MIX = re.compile(r"(由于|因为)([^。！？；]{2,40}?)(造成的|所造成|所致|而引起的|引起的)")
# 时间词重复：然后……这个时候
RE_TIME = re.compile(r"然后([^。！？；]{0,12}?)(这个时候|这时|此时)")
# 介词滥用：当……使/让
RE_PREP = re.compile(r"当([^。！？；]{2,30}?)(使|让)")
# 指代不明：那个地方 / 这个规定
RE_VAGUE = re.compile(r"(这个|那个|这种|那种)(地方|东西|规定|文件|标准|要求|条款|单位|企业|品种|产品|环节|部分|问题|情况)")

CLICHE = ["高度重视", "稳步推进", "切实加强", "进一步深化", "不断提高", "全面推进",
          "认真落实", "深入开展", "着力打造", "持续优化", "狠抓落实", "务求实效",
          "大力加强", "切实抓好", "扎实做好"]


def pass4(text: str, opts: dict) -> dict:
    """语法层修复：只给建议，逐条由人确认。"""
    issues = []
    for ln_no, ln in enumerate(lines_of(text)):
        for m in RE_QUANT.finditer(ln):
            issues.append(_issue(
                4, "grammar", ln_no, m.group(0), "量词冗余",
                f"「{m.group(0)}」里「个」和「{m.group(2)}」重复，删掉「个」。",
                [{"label": "删「个」", "find": m.group(0),
                  "with": m.group(1) + "的" + m.group(2)}]))
        for m in RE_MIX.finditer(ln):
            issues.append(_issue(
                4, "grammar", ln_no, m.group(0), "句式杂糅",
                "「由于…造成的」把两种句式套在一起了，二选一：留「由于…」，或改成「由…造成」。",
                [{"label": "删「造成的」", "find": m.group(3), "with": ""},
                 {"label": "改「由…造成」", "find": m.group(0),
                  "with": "由" + m.group(2) + m.group(3)}]))
        for m in RE_TIME.finditer(ln):
            issues.append(_issue(
                4, "grammar", ln_no, m.group(0), "时间词重复",
                "「然后」和「这个时候」都在表时间，留一个即可。",
                [{"label": "删「然后」", "find": "然后", "with": ""},
                 {"label": "删「" + m.group(2) + "」", "find": m.group(2), "with": ""}]))
        for m in RE_PREP.finditer(ln):
            issues.append(_issue(
                4, "grammar", ln_no, m.group(0), "介词滥用",
                "「当…使…」容易把主语弄丢，建议改成「在…时，…」。",
                [], severity="low"))
        for m in RE_VAGUE.finditer(ln):
            issues.append(_issue(
                4, "vague", ln_no, m.group(0), "指代不明",
                f"「{m.group(0)}」口播时能指着实物说，纸面上读者看不到，必须补出具体名称。",
                [], severity="high"))
        for w in CLICHE:
            if w in ln:
                issues.append(_issue(
                    4, "cliche", ln_no, w, "空话套话",
                    f"「{w}」要替换成具体举措或数据，不能一删了之 —— 直接删会让站位塌陷。",
                    [], severity="high"))
        for s in SENT_RE.findall(ln):
            if cjk_len(s) > 60:
                issues.append(_issue(
                    4, "grammar", ln_no, s.strip(), "长句过长",
                    f"这句话 {cjk_len(s)} 字，一口气读不完，建议在逗号处拆成两句。",
                    [], severity="low"))
    return {
        "text": text,
        "issues": issues,
        "stats": stats(text),
        "notes": {"count": len(issues)},
    }


# ────────────────────────── P5 书面化 ──────────────────────────

def _ranhou_first(sent: str) -> str:
    """「然后」按实际关系选词：因果→因此，顺承→随后

    modified: 原来判断里带 (就|才)，这两个字在中文里太常见——
    「然后我们就开始」「然后才能办」都会命中，结果几乎全判成因果、
    一律建议「因此」，反而误导人。现在只认明确的因果连接词。
    """
    if re.search(r"(所以|因此|因而|于是|结果|导致|造成|以致|以至)", sent):
        return "因此"
    return "随后"


def pass5(text: str, opts: dict) -> dict:
    """口语→书面替换。凡是需要看语境的，一律给多个选项，不搞统一替换。"""
    issues = []
    for ln_no, ln in enumerate(lines_of(text)):
        if "然后" in ln:
            for s in SENT_RE.findall(ln):
                if "然后" not in s:
                    continue
                first = _ranhou_first(s)
                # modified: 原来推荐项和固定项会重复（「随后」出现两次），改为去重后取前 4 个
                choices = [first] + [w for w in ("接着", "因此", "随后", "于是")
                                     if w != first]
                issues.append(_issue(
                    5, "formal", ln_no, "然后", "「然后」按关系选词",
                    f"「{s.strip()}」——「然后」在这里表"
                    f"{'因果' if first == '因此' else '顺承'}，建议改为「{first}」；"
                    "不要全篇统一换成一个词。",
                    [{"label": f"改为「{w}」", "find": "然后", "with": w}
                     for w in choices[:4]]))
                break
        for word, opts_list in [
            ("就是说", ["即", ""]),
            ("这个样子", ["这样"]),
            ("基本上", ["基本"]),
            ("什么的", ["等", ""]),
            ("对吧", [""]),
            ("是不是", [""]),
            ("对不对", [""]),
        ]:
            if word in ln:
                options = [{"label": f"改为「{o}」" if o else "删除",
                            "find": word, "with": o} for o in opts_list]
                issues.append(_issue(
                    5, "formal", ln_no, word, "口头词书面化",
                    f"「{word}」是口语词，书面稿里改掉。",
                    options))
        # modified: 原来同一个 re.search 调了 3 次；并对「我们」「咱们」同句混用时
        # 只替换第一个词的所有出现，另一类会漏掉。这里按实际命中的词分别出建议。
        for who in ("我们", "咱们"):
            if who not in ln:
                continue
            issues.append(_issue(
                5, "formal", ln_no, who, "人称处理",
                "「我们」要看指代：泛指听众的删掉或改「下面」，指本单位的改「我局」。",
                [
                    {"label": "改为「我局」", "find": who, "with": "我局"},
                    {"label": "改为「下面」", "find": who, "with": "下面"},
                    {"label": "删除", "find": who, "with": ""},
                ], severity="low"))
        parts = re.findall(r"(\S)([呢吧啊嘛])(?=[，,。！？!?])", ln)
        if parts:
            issues.append(_issue(
                5, "formal", ln_no, "".join(p[1] for p in parts), "句末语气词",
                f"「{ln.strip()[:30]}」里有 {len(parts)} 处「呢/吧/啊/嘛」，书面稿里删掉。",
                [{"label": f"删除这 {len(parts)} 处语气词",
                  "find": None, "with": None,
                  "regex": r"([\u4e00-\u9fff])([呢吧啊嘛])(?=[，,。！？!?])", "replace": r"\1"}],
                severity="low"))
    return {
        "text": text,
        "issues": issues,
        "stats": stats(text),
        "notes": {"count": len(issues)},
    }


# ────────────────────────── P6 事实核对 ──────────────────────────

RE_FACT = [
    ("法条", re.compile(r"第[一二三四五六七八九十百千零〇\d]+条")),
    ("法规名", re.compile(r"《[^》]{2,30}》")),
    ("日期", re.compile(r"\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?")),
    ("期限", re.compile(r"\d+\s*个?\s*(?:工作日|自然日|日|天|个月|月|年)")),
    ("数字", re.compile(r"\d+(?:\.\d+)?\s*%?")),
]


def pass6(text: str, opts: dict) -> dict:
    """把需要回听核对的东西挑出来，做成清单 —— 核对本身必须人工。"""
    items = []
    for ln_no, ln in enumerate(lines_of(text)):
        # 先收集所有匹配，再按"同起点取最长、被包含的丢弃"去重
        raw = []
        for kind, pat in RE_FACT:
            for m in pat.finditer(ln):
                raw.append((m.start(), m.end(), kind, m.group(0).strip()))
        raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        kept = []
        for s, e, kind, val in raw:
            if not val:
                continue
            if any(s2 <= s and e <= e2 for s2, e2, _, _ in kept):
                continue
            kept.append((s, e, kind, val))
        kept.sort(key=lambda x: x[0])
        for s, e, kind, val in kept:
            items.append({
                "kind": kind, "value": val, "line": ln_no,
                "context": ln.strip()[:60],
            })
    return {
        "text": text,
        "issues": [],
        "stats": stats(text),
        "notes": {"count": len(items), "items": items},
    }


# ────────────────────────── P7 通读 ──────────────────────────

def pass7(text: str, opts: dict) -> dict:
    """朗读前的最后自检：把可能读着别扭的地方标出来。"""
    issues = []
    for ln_no, ln in enumerate(lines_of(text)):
        for s in SENT_RE.findall(ln):
            s = s.strip()
            n = cjk_len(s)
            if n > 55:
                issues.append(_issue(7, "read", ln_no, s, "读起来太长",
                                     f"{n} 字，念下来会断气，建议再拆。", [], "low"))
            if re.search(r"([\u4e00-\u9fff])\1{2,}", s):
                issues.append(_issue(7, "read", ln_no, s, "连续叠字",
                                     "同一个字连用三次以上，可能是识别或口误。", [], "low"))
    return {
        "text": text,
        "issues": issues,
        "stats": stats(text),
        "notes": {"count": len(issues)},
    }


# ────────────────────────── 调度 ──────────────────────────

PASSES = {
    0: ("原稿", "转写出来的原始口播稿，一个字都不改。后面每一遍都从上一遍的结果出发。", "manual"),
    1: ("删填充词", "只删「嗯啊这个那个就是说」，不动其他任何内容。", "auto"),
    2: ("分段", "口语本身没有段落。按话题转换、时间转换切开，再把一口气说完的长句拆开。这是性价比最高的一步。", "auto"),
    3: ("去绕圈", "同一件事说三遍的，只留最准的那一句；顺序讲乱的，归位。", "semi"),
    4: ("语法修复", "修量词冗余、句式杂糅、时间词重复、主语重复、介词滥用，补指代。", "semi"),
    5: ("书面化", "把口语词换成书面语。「然后」这类必须按实际关系选，不能统一替换。", "semi"),
    6: ("事实核对", "数字、法条、日期逐条回听原音。这一步工具只挑不核，必须人工。", "manual"),
    7: ("通读朗读", "念一遍，磕绊处改，读着不像本人处恢复。全磨平就没有说话人的味道了。", "manual"),
}


def run_pass(n: int, text: str, opts: dict = None) -> dict:
    opts = opts or {}
    fn = {1: pass1, 2: pass2, 3: pass3, 4: pass4,
          5: pass5, 6: pass6, 7: pass7}.get(int(n))
    if not fn:
        raise ValueError(f"未知的遍次：{n}")
    res = fn(text, opts)
    res["pass"] = int(n)
    res["name"] = PASSES[int(n)][0]
    return res


def export_markdown(text: str, title: str = "成稿") -> str:
    """一行一段 → Markdown（段间空行）"""
    body = "\n\n".join(p.strip() for p in lines_of(text))
    return f"# {title}\n\n{body}\n"


def to_docx(text: str, title: str = "讲稿") -> bytes:
    """按公文版式导出 Word：标题方正小标宋二号，正文仿宋_GB2312 三号，行距 28 磅。"""
    import io

    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    def set_font(run, name: str, size_pt: float):
        run.font.name = name
        run.font.size = Pt(size_pt)
        run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), name)

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.top_margin, sec.bottom_margin = Cm(3.7), Cm(3.5)
    sec.left_margin, sec.right_margin = Cm(2.8), Cm(2.6)

    normal = doc.styles["Normal"]
    normal.font.name = "仿宋_GB2312"
    normal.font.size = Pt(16)
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "仿宋_GB2312")
    normal.paragraph_format.line_spacing = Pt(28)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)

    head = doc.add_paragraph()
    head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    head.paragraph_format.space_after = Pt(18)
    head.paragraph_format.line_spacing = Pt(34)
    set_font(head.add_run(title), "方正小标宋简体", 22)

    for para in lines_of(text):
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Pt(32)     # 首行缩进 2 字符
        set_font(p.add_run(para), "仿宋_GB2312", 16)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    sample = (
        "呃，各位同事啊，大家好。\n"
        "那么今天下午呢，我们主要是讲一下这个，就是药品零售企业，那个，日常检查的一些要点。\n"
        "首先呢，第一个，就是证照是不是齐全，对吧？\n"
        "就是说我们到现场，第一件事就是要看证照，就是说第一件事就是看证照。\n"
        "然后呢，有个很重要的一条，就是执业药师是不是在岗。\n"
        "然后这个时候，我们还要看药品陈列，那个，处方药和非处方药有没有分开。\n"
        "由于企业没有按规定储存，造成的药品质量问题，这个要重点看。\n"
        "那个地方要特别注意。\n"
        "我们高度重视这项工作，稳步推进日常检查。\n"
        "基本上就是这些，发现问题呢，我们就要依法依规地进行处理，对吧？"
    )
    cur = sample
    for i in range(1, 8):
        r = run_pass(i, cur, {})
        cur = r["text"]
        print(f"\n===== P{i} {r['name']} =====")
        print(cur)
        if r["issues"]:
            for it in r["issues"]:
                print(f"  · [{it['title']}] {it['quote']}")
    print(f"\n字数：{cjk_len(sample)} → {cjk_len(cur)}")
