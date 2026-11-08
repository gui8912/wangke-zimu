# -*- coding: utf-8 -*-
"""术语表读写：错词校正 / 简称膨胀 / 识别热词。

三张表都是纯文本，且带着用户多年维护的分区注释。界面编辑必须做到
「只动条目行」：注释、空行、分区顺序、换行风格（LF/CRLF）、末尾换行
一律原样保留，否则一保存就会把整份文件变成「全部重写」，git 也无从审阅。

对外只有两个入口：
    snapshot()       → 三张表的条目（带行号，供前端定位）
    apply_changes()  → 按 adds / updates / deletes 精确改动指定行
"""
from pathlib import Path
import os
import uuid

try:                          # 【新增 v2.7】「试一句」要复现清洗链路
    import termfix as TF
except Exception:
    TF = None

BASE = Path(__file__).resolve().parent

KINDS = {
    "corrections": {"path": BASE / "terms" / "corrections.txt",
                    "mode": "map", "label": "错词校正"},
    "aliases": {"path": BASE / "terms" / "aliases.txt",
                "mode": "map", "label": "简称表"},
    "hotwords": {"path": BASE / "terms" / "hotwords.txt",
                 "mode": "list", "label": "识别热词"},
}

MAX_KEY = 40        # 错词 / 简称 / 热词的最大长度
MAX_VAL = 120       # 正确词 / 全称
MAX_WEIGHT = 100


# ─────────────────────────── 读写 ───────────────────────────

def _read(path: Path):
    """返回 (统一成 LF 的文本, 原换行符, 是否有末尾换行, 是否带 BOM, 原编码)。

    【修改 v2.7】用 utf-8-sig 解码并记住 BOM：用记事本另存过的词表常带 BOM，
    旧实现会把 BOM 当成第一行的第一个字符，导致第一条词条永远匹配不上
    （界面上看是「这条明明在表里却不生效」）。保存时按原样写回 BOM。

    另外兼容 GBK：中文 Windows 上用记事本「另存为 ANSI」会把文件存成 GBK，
    旧实现直接 UnicodeDecodeError，整个术语表弹窗打不开。这里按原编码读、
    按原编码写，不改用户的文件格式。
    """
    if not path.exists():
        return "", "\n", False, False, "utf-8"
    raw_bytes = path.read_bytes()
    bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    try:
        raw, enc = raw_bytes.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        try:
            raw, enc = raw_bytes.decode("gbk"), "gbk"
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"{path.name} 既不是 UTF-8 也不是 GBK 编码，请用记事本另存为 UTF-8") from exc
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), nl, raw.endswith("\n"), bom, enc


def _split(text: str):
    if text == "":
        return [], False
    trailing = text.endswith("\n")
    body = text[:-1] if trailing else text
    return body.split("\n"), trailing


def parse_items(path: Path, mode: str) -> list:
    """解析条目行。注释、空行、无法识别的行一律跳过（也绝不改动）。"""
    text, _, _, _, _ = _read(path)
    lines, _ = _split(text)
    items = []
    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if mode == "map":
            if "=>" not in s:
                continue
            a, b = s.split("=>", 1)
            a, b = a.strip(), b.strip()
            if a and b:
                items.append({"line": i, "from": a, "to": b})
        else:
            weight = None
            word = s
            if "|" in s:
                head, tail = s.split("|", 1)
                word, tail = head.strip(), tail.strip()
                if tail.isdigit():
                    weight = int(tail)
                elif tail:
                    word = s          # 「|」后面不是数字，整行当词，不乱解释
            if word:
                items.append({"line": i, "word": word, "weight": weight})
    return items


def snapshot() -> dict:
    out = {}
    for kind, meta in KINDS.items():
        try:
            rel = str(meta["path"].relative_to(BASE)).replace("\\", "/")
        except ValueError:      # 单测会把路径指到临时目录，不能因此抛错
            rel = str(meta["path"])
        out[kind] = {
            "label": meta["label"],
            "mode": meta["mode"],
            "file": rel,
            "items": parse_items(meta["path"], meta["mode"]),
        }
    return {"files": out}


# ─────────────────────────── 校验 ───────────────────────────

def _clean(value, limit: int, name: str) -> str:
    s = str(value if value is not None else "").strip()
    s = s.replace("\r", "").replace("\n", "")
    if not s:
        raise ValueError(f"{name}不能为空")
    if "=>" in s:
        raise ValueError(f"{name}里不能出现「=>」")
    if len(s) > limit:
        raise ValueError(f"{name}太长（上限 {limit} 字）")
    return s


def _clean_weight(value):
    if value in (None, "", "null"):
        return None
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ValueError("权重必须是 1–100 的整数")
    if not 1 <= n <= MAX_WEIGHT:
        raise ValueError(f"权重必须在 1–{MAX_WEIGHT} 之间")
    return n


def _render(mode: str, item: dict) -> str:
    if mode == "map":
        return f"{item['from']}=>{item['to']}"
    w = item["word"]
    return f"{w}|{item['weight']}" if item.get("weight") else w


# ─────────────────────────── 保存 ───────────────────────────

def apply_changes(payload: dict) -> dict:
    """按行号精确应用改动。行号以 snapshot() 返回的为准，从大往小处理防漂移。"""
    kind = str((payload or {}).get("kind") or "")
    if kind not in KINDS:
        raise ValueError("未知的术语表类型")
    meta = KINDS[kind]
    path, mode = meta["path"], meta["mode"]

    text, nl, trailing, bom, enc = _read(path)
    lines, _ = _split(text)
    current = {it["line"]: it for it in parse_items(path, mode)}

    deletes = []
    for x in (payload.get("deletes") or []):
        if str(x).strip().isdigit():
            n = int(str(x).strip())
            if n in current:
                deletes.append(n)

    updates = []
    for u in (payload.get("updates") or []):
        if not isinstance(u, dict):
            continue
        n = int(u.get("line") or -1)
        if n not in current:
            raise ValueError(f"要修改的条目已不存在（第 {n + 1} 行），请刷新后重试")
        if mode == "map":
            item = {"from": _clean(u.get("from"), MAX_KEY, "错词 / 简称"),
                    "to": _clean(u.get("to"), MAX_VAL, "正确词 / 全称")}
            if item["from"] == item["to"]:
                raise ValueError("错词与正确词不能相同")
        else:
            item = {"word": _clean(u.get("word"), MAX_KEY, "热词"),
                    "weight": _clean_weight(u.get("weight"))}
        updates.append((n, item))

    adds = []
    for a in (payload.get("adds") or []):
        if not isinstance(a, dict):
            continue
        if mode == "map":
            item = {"from": _clean(a.get("from"), MAX_KEY, "错词 / 简称"),
                    "to": _clean(a.get("to"), MAX_VAL, "正确词 / 全称")}
            if item["from"] == item["to"]:
                raise ValueError(f"「{item['from']}」的错词与正确词相同，没有意义")
        else:
            item = {"word": _clean(a.get("word"), MAX_KEY, "热词"),
                    "weight": _clean_weight(a.get("weight"))}
        adds.append(item)

    # 查重：现有条目（排除将被删/改的行）与新增之间都不允许重复
    dropped = set(deletes) | {n for n, _ in updates}
    seen = {}
    for n, it in current.items():
        if n in dropped:
            continue
        seen[it.get("from") or it.get("word")] = n

    for n, it in updates:
        key = it.get("from") or it.get("word")
        if key in seen:
            raise ValueError(f"「{key}」与第 {seen[key] + 1} 行重复")
        seen[key] = n

    new_adds = []
    for it in adds:
        key = it.get("from") or it.get("word")
        if key in seen:
            raise ValueError(f"「{key}」已经在表里了")
        seen[key] = -1
        new_adds.append(it)

    # 从大往小改，行号不会漂移
    for n in sorted(set(deletes), reverse=True):
        del lines[n]
    for n, item in sorted(updates, key=lambda x: -x[0]):
        indent = lines[n][:len(lines[n]) - len(lines[n].lstrip())]
        lines[n] = indent + _render(mode, item)

    if new_adds:
        # 追加到文件末尾，前面留一个空行，不破坏现有分区
        if lines and lines[-1].strip():
            lines.append("")
        for it in new_adds:
            lines.append(_render(mode, it))

    out = nl.join(lines)
    if trailing and lines:
        out += nl
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        try:
            data = out.encode(enc)    # 【新增 v2.7】按原编码写回（UTF-8 / GBK）
        except UnicodeEncodeError as exc:
            # GBK 装不下新加的罕见字：明确让用户转存 UTF-8，不要写半个文件
            tmp.unlink(missing_ok=True)
            raise ValueError(
                f"该词含 {enc.upper()} 不支持的字符，请先把 {path.name} 另存为 UTF-8") from exc
        if bom:                       # 原文件带 BOM，按原样写回
            data = b"\xef\xbb\xbf" + data
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise

    return {"ok": True, "kind": kind,
            "added": len(new_adds), "updated": len(updates),
            "deleted": len(set(deletes)),
            "total": len(parse_items(path, mode))}


# ─────────────────────────── 试改（字幕 / 讲稿共用同一套规则）───────────────────────────

def preview(text: str) -> dict:
    """【新增 v2.7】把一句话按「错词校正 → 简称膨胀 → 术语补全」跑一遍。

    存在的理由：用户反复问「术语表改了，字幕清理到底用不用」。
    与其解释，不如让他在弹窗里输入一句原文，直接看到三步各自改了什么。
    这里刻意复用 clean_transcript 的加载函数与 termfix 的实现，
    不复写一套规则——预览结果与真实清理结果必须一致。
    """
    text = str(text or "").strip()[:400]
    if not text:
        raise ValueError("请先输入一句要试的原文")

    import clean_transcript as CT
    corrections = CT.load_corrections(KINDS["corrections"]["path"])
    terms, aliases = CT.load_glossary()

    stage, steps = text, []
    hits = []
    for wrong, right in corrections:
        if wrong in stage:
            n = stage.count(wrong)
            stage = stage.replace(wrong, right)
            hits.append(f"{wrong} → {right}×{n}")
    if hits:
        steps.append({"name": "错词校正", "detail": "；".join(hits), "after": stage})

    alog = []
    if TF is not None:
        stage, alog = TF.expand_aliases(stage, aliases)
        if alog:
            steps.append({"name": "简称膨胀", "detail": "；".join(alog), "after": stage})

        stage, changes, suggestions = TF.complete(stage, terms, auto=True)
        if changes:
            steps.append({"name": "术语补全",
                          "detail": "；".join(f"{a}：{b}" for a, b, _ in changes),
                          "after": stage})
    else:
        suggestions = []

    return {"before": text, "after": stage, "steps": steps,
            "suggestions": suggestions[:8],
            "note": "预览与「重新清理 / 识别后清理」走同一套规则；"
                    "实际清理还会删语气词、合并重复，这里只演示术语表的部分。"}


def reload_caches() -> bool:
    """让改完的术语立即生效：清洗模块的术语/简称按 mtime 缓存，刷新一次即可。

    【修改 v2.7】原来只改模块级 `_TERMS/_ALIASES`；现在 Cleaner 在构造时
    自己取词表，必须让缓存失效才会读到新内容。返回是否刷新成功，
    界面据此提示「已生效 / 下次清理生效」，不再含糊。
    """
    try:
        import clean_transcript as CT
        if getattr(CT, "TF", None) is None:
            return False
        terms, aliases = CT.load_glossary(force=True)
        # 兼容仍直接读这两个名字的旧代码/测试
        CT._TERMS, CT._ALIASES = terms, aliases
        return True
    except Exception:
        # 刷新失败不影响保存本身；下次重启自然生效
        return False
