# -*- coding: utf-8 -*-
"""编辑态落盘（【新增 v2.12-p9】）。

为什么单独做一层
────────────────
`/api/save` 写的 `<stem>.edited.srt` 是**导出产物**：键是文件名、没有修订号，
`/api/draft` 又靠 mtime 判新旧。于是有三类「刷新后校对稿不见了」的隐患：

① 两个课件同名（都叫「第1讲」）→ 草稿互相覆盖；
② 同一节课重新识别 → `.raw.srt` 的 mtime 变新，`/api/draft` 静默不恢复；
③ 任何工具碰一下 `.edited.srt` 的 mtime，旧稿就被判成「比原文旧」。

这里改成独立的一层：
  · 键优先用 **task_id**（没有任务时退回 stem），一节课一份，永不串稿；
  · 自己带 `revision` 与 `saved_at`，恢复时只看文件在不在，不看 mtime；
  · 原子写（同目录临时文件 + os.replace），写一半断电也不会留半截 JSON；
  · 有修订号即可做「两个标签页同时改」的冲突检测（base_revision 落后就拒写）。

对应前端：`saveEditState()` 防抖写入、`restoreEditState()` 优先读取，
页面隐藏/关闭时用 keepalive 再补一次，避免最后 1.2 秒的编辑丢掉。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

# 单个编辑态上限：约 8MB。2000 条字幕通常 200KB 上下，留足余量；
# 真超了说明 cues 里混进了别的东西，宁可拒绝也不要写一个读不动的文件。
MAX_BYTES = 8 * 1024 * 1024
# 目录内最多保留多少份。一节课一份，300 份足够覆盖几年的课程；
# 超出按 mtime 淘汰最旧的，绝不因为「磁盘目录太长」拖慢启动。
MAX_FILES = 300
# 超过这个天数没动过的编辑态自动淘汰（0 表示不按时间淘汰）。
MAX_AGE_DAYS = 365

# 文件结构版本。以后字段变了就 +1，旧文件会被 load() 当成不存在，
# 而不是拿旧结构去喂新前端。
SCHEMA = 1

DIR = Path(__file__).resolve().parent / "out" / "_edit"

_LOCK = threading.Lock()


class EditError(Exception):
    """编辑态的基类异常：调用方统一捕获，转成 4xx，不让它冒成 500。"""


class InvalidKey(EditError):
    pass


class TooLarge(EditError):
    pass


class Conflict(EditError):
    """base_revision 落后于磁盘上的版本（另一个标签页/窗口先写了）。"""

    def __init__(self, current: dict):
        super().__init__("编辑态已被另一处更新")
        self.current = current or {}


def safe_key(raw) -> str:
    """把 task_id / stem 变成安全文件名。

    与 server._safe 的差别：额外去掉开头的 `.`，否则 `..`、`.hidden`
    这类名字在 Windows 上仍然是坑；长度截到 120，避免超过路径上限。
    """
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f\s]+', "_", str(raw or ""))
    s = s.strip("._ ")
    return s[:120]


def key_for(task_id="", stem="") -> str:
    """编辑态键：有任务用任务号，没有任务退回文件名。

    前缀 `t_` / `s_` 不只是好看——它让「任务 id 恰好等于某个 stem」
    这种情况也不会互相覆盖，同时在目录里一眼能看出这条记录属于哪种来源。
    """
    tid = safe_key(task_id)
    if tid:
        return "t_" + tid
    st = safe_key(stem)
    if st:
        return "s_" + st
    return ""


def _path(key: str) -> Path:
    k = safe_key(key)
    if not k:
        raise InvalidKey("编辑态键为空")
    return DIR / f"{k}.json"


def _check_cues(cues) -> list:
    """只校验形状，不修正时间码——修正由 server._normalize_cues 统一做。

    这一层再查一遍的理由：万一以后有别的调用方绕过 HTTP 直接写，
    也不能把 `start: "1.2"` 这种数据写进去，否则刷新后整页字幕画不出来。
    """
    if not isinstance(cues, list) or not cues:
        raise EditError("cues 不能为空")
    if len(cues) > 200000:
        raise EditError("cues 条数超出上限")
    for i, c in enumerate(cues):
        if not isinstance(c, dict):
            raise EditError(f"第 {i + 1} 条不是对象")
        for k in ("start", "end"):
            v = c.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise EditError(f"第 {i + 1} 条 {k} 不是数字")
            if v != v or v in (float("inf"), float("-inf")):
                raise EditError(f"第 {i + 1} 条 {k} 非法")
        if not isinstance(c.get("text"), str):
            raise EditError(f"第 {i + 1} 条 text 不是字符串")
    return cues


def _atomic_write(p: Path, text: str):
    """同目录临时文件 + os.replace（与 server._atomic_write 同思路）。

    临时名带随机后缀：自动保存与手动保存可能同时写同一份，
    固定 `.tmp` 会互相覆盖，后一个 os.replace 时源文件已被搬走。
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                # Windows 上杀软/索引服务瞬时占用目标文件，重试一次多半就好
                if attempt == 2:
                    raise
                time.sleep(0.05)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load(key: str):
    """读编辑态；不存在 / 损坏 / 版本不符一律返回 None，绝不抛给调用方。"""
    try:
        p = _path(key)
    except InvalidKey:
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("schema") != SCHEMA:
        return None
    if not isinstance(d.get("cues"), list) or not d["cues"]:
        return None
    return d


def save(key: str, *, cues, task_id="", stem="", mode="clean",
         base_revision=None, now=None) -> dict:
    """写入一份编辑态，返回 {revision, saved_at, ...}。

    base_revision:
      · None / 0  —— 不校验（首次保存、或刚用 keepalive 写过、修订号未知）
      · 正整数    —— 必须等于磁盘上的当前修订号，否则抛 Conflict
    """
    if not key:
        raise InvalidKey("编辑态键为空")
    cues = _check_cues(cues)
    if mode not in ("clean", "raw"):
        mode = "clean"

    ts = float(now if now is not None else time.time())
    chars = sum(len(c.get("text") or "") for c in cues)
    body = {
        "schema": SCHEMA,
        "key": safe_key(key),
        "task_id": str(task_id or ""),
        "stem": str(stem or ""),
        "mode": mode,
        "cues": cues,
        "chars": chars,
        "saved_at": round(ts, 3),
        "saved_at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
    }

    with _LOCK:
        cur = load(key) or {}
        cur_rev = int(cur.get("revision") or 0)
        base = None
        try:
            base = int(base_revision) if base_revision not in (None, "") else None
        except (TypeError, ValueError):
            base = None
        if base and base > 0 and base < cur_rev:
            raise Conflict(cur)

        rev = cur_rev + 1
        body["revision"] = rev
        text = json.dumps(body, ensure_ascii=False)
        size = len(text.encode("utf-8"))
        if size > MAX_BYTES:
            raise TooLarge(
                f"编辑态过大（{size / 1048576:.1f}MB，上限 {MAX_BYTES // 1048576}MB）")
        _atomic_write(_path(key), text)
        prune(keep_key=key)

    return {"revision": rev, "saved_at": body["saved_at"],
            "saved_at_text": body["saved_at_text"],
            "chars": chars, "bytes": size}


def prune(keep_key="") -> int:
    """按数量 / 天数淘汰旧编辑态，返回删除份数。

    keep_key 那一份永不删——哪怕目录里已经堆了上千份，
    也不能因为一次保存把自己刚写进去的东西顺手淘汰掉。
    """
    keep = f"{safe_key(keep_key)}.json" if keep_key else ""
    try:
        files = [p for p in DIR.glob("*.json") if p.is_file()]
    except OSError:
        return 0

    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=mtime, reverse=True)
    cutoff = (time.time() - MAX_AGE_DAYS * 86400) if MAX_AGE_DAYS > 0 else 0
    victims = set()
    for i, p in enumerate(files):
        if p.name == keep:
            continue
        too_many = i >= MAX_FILES
        too_old = bool(cutoff) and mtime(p) < cutoff
        if too_many or too_old:
            victims.add(p)
    # keep 自己排在限额之外时，上面的规则会少删一份；补删最旧的非 keep，
    # 保证文件数真的回到上限内（否则「淘汰」形同虚设）。
    remaining = len(files) - len(victims)
    for p in reversed(files):
        if remaining <= MAX_FILES:
            break
        if p.name == keep or p in victims:
            continue
        victims.add(p)
        remaining -= 1

    removed = 0
    for p in victims:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    # 顺手清理异常退出留下的临时文件（超过 1 小时才算，避免误删正在写的）
    for p in DIR.glob("*.tmp"):
        try:
            if time.time() - p.stat().st_mtime > 3600:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def remove(key: str) -> bool:
    try:
        p = _path(key)
    except InvalidKey:
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


def stats() -> dict:
    """给自检 / 排查用：多少份、共多大、最近一次写入时间。"""
    try:
        files = [p for p in DIR.glob("*.json") if p.is_file()]
    except OSError:
        files = []
    size = 0
    newest = 0.0
    for p in files:
        try:
            st = p.stat()
            size += st.st_size
            newest = max(newest, st.st_mtime)
        except OSError:
            pass
    return {"count": len(files), "bytes": size,
            "newest": round(newest, 3) if newest else None,
            "dir": str(DIR)}
