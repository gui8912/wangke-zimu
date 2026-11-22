# -*- coding: utf-8 -*-
"""逐片实时预览缓冲（【新增 v2.12-p10】）。

定位
────
识别是「一片一片」跑的：Paraformer 默认 10 分钟一片（`ASR_FUNASR_CHUNK`），
Qwen3 默认 170 秒一片。每片识别完，这里就把这一片的字幕存进内存，
前端带 `since=revision` 拉增量，长课就能边转边看，不用等整节跑完。

为什么只放内存
──────────────
一小时课约 1500 句 / 100KB 上下，几十片加起来很可观；而且每片都会更新，
落盘会把任务日志的 1 秒节流打回原形，`_jobs/*.json` 也会越写越大。
实时预览只是「过程可视」，成稿仍由既有的 SRT/TXT/编辑态负责。

段落状态
────────
每片带 `state`：本模块当前只产出 `final`（这一片已经识别并清理完毕）。
`partial` 是「正在解码的片」——两种模型都是一次调用出一片，没有片内增量，
所以用它只表示进度，不携带文本；`edited` 属于转写完成后的编辑态，
由 v2.12-p9 的 `edit_state` 负责，两者不混。
"""

from __future__ import annotations

import threading
import time


class LiveBuffer:
    """线程安全的逐片缓冲。

    4 路并行时片是乱序完成的：`put()` 只按片号覆盖，`snapshot()` 按片号排序，
    前端再按时间排序渲染。这样某一片慢不会卡住后面的片，也不会串位。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.revision = 0
        self.total = 0
        self.slices = {}
        self.started = time.time()
        self.updated = 0.0

    def put(self, i: int, total: int, cues: list, state: str = "final") -> int:
        """写入/覆盖一片，返回新的 revision。

        total 变化视为「换了一次识别」：清空旧片，避免重试后新旧片混在一起。
        """
        total = int(total or 0)
        with self._lock:
            if total and total != self.total:
                self.total = total
                self.slices.clear()
            self.revision += 1
            self.slices[int(i)] = {
                "rev": self.revision,
                "state": state if state in ("partial", "final") else "final",
                "cues": cues or [],
            }
            self.updated = time.time()
            return self.revision

    def done(self) -> int:
        with self._lock:
            return len(self.slices)

    def complete(self) -> bool:
        with self._lock:
            return bool(self.total and len(self.slices) >= self.total)

    def snapshot(self, since: int = 0) -> dict:
        """since=0 返全部；否则只返 revision 更大的片（增量）。"""
        try:
            since = int(since or 0)
        except (TypeError, ValueError):
            since = 0
        with self._lock:
            rows = []
            for i in sorted(self.slices, key=lambda x: int(x)):
                s = self.slices[i]
                if int(s.get("rev") or 0) > since:
                    rows.append({"i": int(i), "rev": int(s.get("rev") or 0),
                                 "state": s.get("state") or "final",
                                 "cues": s.get("cues") or []})
            return {"revision": self.revision, "since": since,
                    "total": self.total, "done": len(self.slices),
                    "slices": rows,
                    "complete": bool(self.total and len(self.slices) >= self.total)}

    def chars(self) -> int:
        """已出字数（给自检 / 排查用）。"""
        with self._lock:
            return sum(len(c.get("text") or "")
                       for s in self.slices.values()
                       for c in (s.get("cues") or []))
