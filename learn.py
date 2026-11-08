# -*- coding: utf-8 -*-
"""修订学习：把用户的修改提炼成候选规则，人工采纳后写进词表（v1.5）。

为什么不直接"学会并自动应用"：

1. **一次误改不能变成永久规则**。用户改错一个字、或者只是这次想这么说，
   如果自动进词表，下次所有课都会被改。大厂的编辑日志挖掘同样要求
   human-in-the-loop，机器只负责「找规律」，人负责「拍板」。
2. 自动保存每 1.2 秒一次，同一次修改会被重复记录几十次，
   直接累计计数会立刻把任何一次误改推到"高频"。

所以这里只做三件事：
  记录（同一内容只记一次）→ 提炼最小差异对 → 到达阈值后列为候选

采纳后才会写进 terms/corrections.txt 或 terms/aliases.txt。

对外：
    record(stem, before_cues, after_cues) -> 新增/更新的候选数
    pending() -> 候选列表
    accept(key, kind) -> 写入词表
    ignore(key) -> 标记忽略
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
LEARN_DIR = BASE / "out" / "_learn"
STATS = LEARN_DIR / "stats.json"
CORRECTIONS = BASE / "terms" / "corrections.txt"
ALIASES = BASE / "terms" / "aliases.txt"
TERMS = BASE / "terms" / "hotwords.txt"

MIN_COUNT = 2          # 同一处修改出现几次才列为候选
MIN_SEG, MAX_SEG = 2, 20   # 差异片段长度范围（单字噪声太大；太长的多半是改写）
CTX = 1                # 纯插入/删除时向左右各扩几个「词」

# jieba 用于词级 diff：字符级 diff 会把「质量规范→质量管理规范」扩成
# 「营质量规范的→营质量管理规范的」这种带脏边界的片段，词级才干净。
try:
    import warnings
    warnings.filterwarnings("ignore", message=".*pkg_resources is deprecated.*")
    import jieba
    jieba.setLogLevel(60)          # 关掉首次加载的日志噪声
    _HAS_JIEBA = True
except Exception:                  # pragma: no cover
    jieba = None
    _HAS_JIEBA = False

# 上一次已学习的内容签名：自动保存 1.2 秒一次，内容没变就不重复记
_LAST_SIG = {}


# ────────────────────── 存储 ──────────────────────

def _load() -> dict:
    if not STATS.exists():
        return {}
    try:
        return json.loads(STATS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    LEARN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATS.with_name(STATS.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATS)


# ────────────────────── 差异提炼 ──────────────────────

def _tokens(s: str) -> list:
    """切成词（jieba 不可用时退化为逐字）。"""
    if _HAS_JIEBA:
        return [w for w in jieba.cut(s) if w]
    return list(s)


def extract_edits(before: str, after: str) -> list:
    """返回可复用的差异对 [(旧片段, 新片段), ...]。

    在**词**级别做 diff，再把差异区间向左右各扩 CTX 个词：
      - replace：改了字（「管里」→「管理」、同音错字）
      - insert ：补了字（「质量规范」→「质量管理规范」）—— 补全/纠错最常见的形态
      - delete ：删了字

    为什么必须带上下文：单独一个「里→理」不能当全局规则，会把所有「里」都改掉；
    带上词级上下文变成「质量管里体系→质量管理体系」，才是可审阅、可回退的规则。
    为什么在词级扩：字符级扩会把「经营」的「营」、「的」这种边界字带进来，
    生成「营质量规范的」这种没法看也没法审的片段。
    """
    if not before or not after or before == after:
        return []
    bt, at = _tokens(before), _tokens(after)
    sm = difflib.SequenceMatcher(None, bt, at, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a, b = max(0, i1 - CTX), min(len(bt), i2 + CTX)
        old = "".join(bt[a:b])
        left = i1 - a
        # after 侧区间长度 = 原区间长度 + 本次差异造成的词数差
        new_len = (b - a) + (j2 - j1) - (i2 - i1)
        new_start = max(0, j1 - left)
        new = "".join(at[new_start:new_start + max(0, new_len)])
        if not (MIN_SEG <= len(old) <= MAX_SEG and MIN_SEG <= len(new) <= MAX_SEG):
            continue
        if old.strip() == new.strip():
            continue
        # 纯标点/空白差异不学
        if not re.search(r"[\u4e00-\u9fa5A-Za-z]", old + new):
            continue
        out.append((old, new))
    return out


def _term_set() -> set:
    if not TERMS.exists():
        return set()
    return {ln.split("|")[0].strip() for ln in
            TERMS.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def _classify(old: str, new: str, terms: set) -> str:
    """给出建议写进哪张表：correction（错词校正）/ alias（简称）/ unknown。"""
    if new in terms:
        return "correction"          # 改后的词就是标准术语 → 典型错词校正
    # 改后是某个术语的组成片段（如「质量管理规范」是「药品经营质量管理规范」的子串），
    # 也按错词校正处理：用户补的往往是术语里被漏掉的那一段
    if any(new in t for t in terms if len(new) >= 4):
        return "correction"
    if re.fullmatch(r"[A-Za-z0-9]{2,8}", old) and len(new) > len(old):
        return "alias"               # 英文缩写 → 全称
    return "unknown"


# ────────────────────── 记录 ──────────────────────

def record(stem: str, before_cues: list, after_cues: list) -> int:
    """对比系统产出与用户提交，累计候选。返回新增/更新的候选条数。"""
    if not before_cues or not after_cues:
        return 0
    sig = hashlib.md5(
        ("|".join(str(c.get("text", "")) for c in after_cues)).encode("utf-8")
    ).hexdigest()
    if _LAST_SIG.get(stem) == sig:      # 自动保存的重复提交，不重复学
        return 0
    _LAST_SIG[stem] = sig

    # 句子数差异过大 → 用户换了来源（比如改成讲义对齐稿），不是逐句校对，不学
    if abs(len(before_cues) - len(after_cues)) > max(3, len(before_cues) // 2):
        return 0

    terms = _term_set()
    hits = []
    for b, a in zip(before_cues, after_cues):
        hits.extend(extract_edits(str(b.get("text") or ""), str(a.get("text") or "")))
    if not hits:
        return 0

    d = _load()
    now = time.time()
    changed = 0
    for old, new in hits:
        key = f"{old}=>{new}"
        rec = d.get(key)
        if rec is None:
            rec = {"n": 0, "kind": _classify(old, new, terms), "stems": [],
                   "first": now, "ignored": False}
            d[key] = rec
        rec["n"] = int(rec.get("n") or 0) + 1
        rec["last"] = now
        if stem not in rec["stems"]:
            rec["stems"] = (rec.get("stems") or [])[-4:] + [stem]
        changed += 1
    if changed:
        _save(d)
    return changed


# ────────────────────── 候选与采纳 ──────────────────────

def pending(min_count: int = MIN_COUNT) -> list:
    """返回达到阈值的候选，按次数降序。"""
    d = _load()
    out = []
    for key, rec in d.items():
        if rec.get("ignored"):
            continue
        n = int(rec.get("n") or 0)
        if n < min_count:
            continue
        old, _, new = key.partition("=>")
        out.append({"key": key, "before": old, "after": new, "n": n,
                    "kind": rec.get("kind") or "unknown",
                    "stems": rec.get("stems") or []})
    out.sort(key=lambda x: (-x["n"], x["key"]))
    return out


def _append_line(path: Path, line: str) -> bool:
    """往词表追加一行；已存在则跳过。

    **保持原文件的换行风格**：origin 的 corrections.txt 是 LF，
    但它也可能是 CRLF（用户用记事本改过）。整份改掉换行会让 git 显示
    整个文件被重写，也会让其他工具（如 .bat 风格的编辑器）看出异常。
    """
    if path.exists():
        raw = path.read_bytes().decode("utf-8")
        nl = "\r\n" if "\r\n" in raw else "\n"
        txt = raw.replace("\r\n", "\n")
        if line in {ln.strip() for ln in txt.splitlines()}:
            return False
        if not txt.endswith("\n"):
            txt += "\n"
    else:
        nl = "\n"
        txt = ""
    out = (txt + line + "\n").replace("\n", nl)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(out.encode("utf-8"))
    os.replace(tmp, path)
    return True


def accept(key: str, kind: str = None) -> tuple:
    """采纳候选：写入对应词表。返回 (是否写入, 说明)。"""
    d = _load()
    rec = d.get(key)
    if not rec:
        return False, "候选不存在"
    old, _, new = key.partition("=>")
    if not old or not new:
        return False, "候选内容不完整"
    kind = kind or rec.get("kind") or "unknown"
    if kind == "alias":
        ok = _append_line(ALIASES, f"{old}=>{new}")
        target = "aliases.txt（简称表）"
    else:
        ok = _append_line(CORRECTIONS, f"{old}=>{new}")
        target = "corrections.txt（错词校正表）"
    rec["accepted"] = True
    rec["ignored"] = True
    _save(d)
    msg = f"已写入 {target}" if ok else f"{target} 里已有该条，未重复写入"
    # 短片段全局替换的误伤面更大，明确提示（不阻止，由人判断）
    if ok and len(old) <= 3:
        msg += f"；提示：「{old}」只有 {len(old)} 个字，全局替换可能波及其他表述，建议先在小样本上验证"
    return ok, msg


def ignore(key: str) -> bool:
    d = _load()
    rec = d.get(key)
    if not rec:
        return False
    rec["ignored"] = True
    _save(d)
    return True


def stats() -> dict:
    d = _load()
    return {"total": len(d),
            "pending": len(pending()),
            "accepted": sum(1 for r in d.values() if r.get("accepted"))}
