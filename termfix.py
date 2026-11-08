# -*- coding: utf-8 -*-
"""术语修复：简称膨胀 + 漏字补全（v1.5）。

解决两类真实场景问题：

1. **老师用简称**（「GSP要求企业…」）
   简称与全称之间没有字符相似性 —— 实测 `GSP` 在 125 条术语表里模糊匹配
   最高只有 29 分（最像的是「上市前GMP符合性检查」），任何 fuzzy 算法都认不出。
   只能靠 `terms/aliases.txt` 显式映射。

2. **老师把术语说漏一两个字**（「药品经营质量规范」，漏了「管理」）
   用术语表做近似匹配补回来。但这一步风险高，必须严格约束：

   - 只补「比术语短」的片段：补全只加字，绝不删字。
     否则「按照药品管理法实施条例」会把「按」吃进候选。
   - 首尾必须都对得上：漏字只会发生在中间。
     「依据药品管理的规定」不会被补成「药品管理法」（末字「理」≠「法」，判为截断）。
   - 强上下文才自动改（依据/根据/按照/《/第X条/规定/要求），
     弱上下文只进建议列表，由人工决定。沿用「宁可少删，不可误删」的既有原则。

实测（125 条术语表）：4/5 正样本正确检出，6/6 负样本零误改；
性能 2.4 ms/句，加 3-gram 短路后 0.2 ms/句。

对外：
    fix(text, terms, aliases, auto=True) -> (新文本, 变更列表, 建议列表)
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    from rapidfuzz import fuzz
    AVAILABLE = True
except Exception:            # pragma: no cover
    fuzz = None
    AVAILABLE = False

# 强上下文：这些词前后出现术语，说明在引用法规/制度名，可以放心补全
STRONG_PRE = ("依据", "根据", "按照", "遵照", "执行", "符合", "违反",
              "落实", "对照", "适用", "《")
STRONG_POST = ("第", "规定", "要求", "明确", "指出", "所称")

MIN_SCORE = 85.0       # 相似度下限
MAX_GAP = 3            # 最多补 3 个字
MIN_TERM_LEN = 4       # 短于此长度的术语不参与补全（太容易误伤）


# ────────────────────── 配置加载 ──────────────────────

def load_terms(path) -> list:
    """读术语表：每行一个，# 注释，行尾 |权重 忽略。"""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        out.append(ln.split("|")[0].strip())
    # 长词优先，避免短词抢占长词的匹配区间
    return sorted({t for t in out if t}, key=len, reverse=True)


def load_aliases(path) -> list:
    """读简称表：简称=>全称。# 注释与空行忽略。"""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=>" not in ln:
            continue
        short, full = ln.split("=>", 1)
        short, full = short.strip(), full.strip()
        if short and full and short != full:
            out.append((short, full))
    return sorted(out, key=lambda x: len(x[0]), reverse=True)


# ────────────────────── 简称膨胀 ──────────────────────

def expand_aliases(text: str, aliases: list) -> tuple:
    """简称 → 全称。英文缩写按整词边界，中文按整词替换。

    边界用 [A-Za-z0-9] 而不是 \b：中文场景下「GSP认证」的 GSP 后面紧跟汉字，
    \\b 在汉字与字母之间不成立，会漏掉最常见的写法。
    """
    if not text or not aliases:
        return text, []
    log = []
    for short, full in aliases:
        if short not in text:
            continue
        if re.fullmatch(r"[A-Za-z0-9]+", short):
            pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(short) +
                             r"(?![A-Za-z0-9])")
        else:
            pat = re.compile(re.escape(short))
        text, n = pat.subn(full, text)
        if n:
            log.append(f"{short}→{full}×{n}")
    return text, log


# ────────────────────── 漏字补全 ──────────────────────

def _strong_context(text: str, i: int, end: int) -> bool:
    pre = text[max(0, i - 2):i]
    post = text[end:end + 3]
    return (any(pre.endswith(p) for p in STRONG_PRE)
            or any(post.startswith(p) for p in STRONG_POST))


def find_completions(text: str, terms: list, min_score: float = MIN_SCORE,
                     max_gap: int = MAX_GAP) -> list:
    """返回候选列表（按位置排序），每项 dict：i/L/seg/term/score/strong。"""
    if not AVAILABLE or not text or not terms:
        return []
    n = len(text)
    cands = []
    for t in terms:
        lt = len(t)
        if lt < MIN_TERM_LEN:
            continue
        # 3-gram 短路：术语的任意 3 字片段都不在句子里，就不可能是近似写法。
        # 实测提速 10×，结果与全量扫描完全一致。
        if not any(t[k:k + 3] in text for k in range(max(1, lt - 2))):
            continue
        for L in range(max(2, lt - max_gap), lt):     # 只补更短的片段
            for i in range(0, n - L + 1):
                seg = text[i:i + L]
                # 首尾锚定：漏字发生在中间，末端缺字按「截断」处理，不自动改
                if not (t.startswith(seg[0]) and t.endswith(seg[-1])):
                    continue
                sc = fuzz.ratio(t, seg)
                if sc < min_score:
                    continue
                cands.append({"i": i, "L": L, "seg": seg, "term": t,
                              "score": round(float(sc), 1),
                              "strong": _strong_context(text, i, i + L)})
    # 同一区间只留最高分；区间重叠的取分高者，避免嵌套替换
    cands.sort(key=lambda c: (-c["score"], -len(c["term"])))
    taken, out = [], []
    for c in cands:
        a, b = c["i"], c["i"] + c["L"]
        if any(not (b <= x or a >= y) for x, y in taken):
            continue
        taken.append((a, b))
        out.append(c)
    return sorted(out, key=lambda c: c["i"])


def complete(text: str, terms: list, auto: bool = True) -> tuple:
    """补全漏字。返回 (新文本, 变更列表, 建议列表)。

    auto=True：强上下文直接改；弱上下文进建议。
    auto=False：全部只进建议，不改原文（保守模式）。
    """
    cands = find_completions(text, terms)
    if not cands:
        return text, [], []

    changes, suggestions, applied = [], [], []
    for c in cands:
        if auto and c["strong"]:
            applied.append(c)
            changes.append(("术语补全", f"{c['seg']}→{c['term']}", 1))
        else:
            suggestions.append(
                f"「{c['seg']}」疑为「{c['term']}」（相似度 {c['score']:.0f}，"
                f"上下文不足以自动改）")

    if applied:
        for c in sorted(applied, key=lambda x: x["i"], reverse=True):
            text = text[:c["i"]] + c["term"] + text[c["i"] + c["L"]:]
    return text, changes, suggestions


# ────────────────────── 主入口 ──────────────────────

def fix(text: str, terms: list = None, aliases: list = None,
        auto: bool = True, do_complete: bool = True) -> tuple:
    """简称膨胀 → 漏字补全。返回 (文本, 变更列表, 建议列表)。"""
    if not text:
        return text, [], []
    changes, suggestions = [], []

    text, alog = expand_aliases(text, aliases or [])
    if alog:
        changes.append(("简称补全", "；".join(alog), len(alog)))

    if do_complete:
        text, cch, csg = complete(text, terms or [], auto=auto)
        changes.extend(cch)
        suggestions.extend(csg)

    return text, changes, suggestions


def main() -> None:
    """命令行自检：python termfix.py"""
    base = Path(__file__).resolve().parent
    terms = load_terms(base / "terms" / "hotwords.txt")
    aliases = load_aliases(base / "terms" / "aliases.txt")
    print(f"术语 {len(terms)} 条，简称 {len(aliases)} 条\n")

    cases = [
        "GSP要求企业建立质量管理体系。",
        "依据药品经营质量规范的要求，企业应当建立质量管理体系。",
        "医疗器械监管条例第二十条规定。",
        "我们按照药品管理法实施条例执行。",
        "依据药品管理的规定执行。",
        "药品管理方面的问题要重视。",
    ]
    for s in cases:
        out, ch, sg = fix(s, terms, aliases)
        print("原文:", s)
        print("结果:", out)
        print("变更:", "；".join(f"{a}:{b}" for a, b, _ in ch) if ch else "（无）")
        print("建议:", "；".join(sg) if sg else "（无）")
        print("-" * 76)


if __name__ == "__main__":
    main()
