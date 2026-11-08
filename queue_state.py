# -*- coding: utf-8 -*-
"""夜间批量队列：状态持久化 + 可排序队列。

调研参考（2026-11-08）：
  · Buzz（本地 Whisper 图形工具）：Ctrl+O 多选入队、按添加顺序跑、
    Folder Watch 自动收新文件、CLI `buzz add` 供计划任务调用、
    「跳过已转录」避免重复消耗算力、并提醒大文件别一次压太多；
  · 云端转写服务：任务用唯一 ID 落盘，关掉浏览器仍在跑，
    重连后能按 ID 找回进度。

我们据此落地：
  · 队列状态（暂停 / 定时开始 / 失败自动重试 / 文件夹监控）落 config/queue.json；
  · queued 任务重启后自动重新排队（不是粗暴标成「中断」）；
  · 队列可上移下移，方便把急用的课提到前面；
  · 完成汇总报告，早上起来一眼看清夜里跑得怎么样。

配置不入库（.gitignore 已排除 config/），不含任何密钥。
"""
import collections
import json
import os
import queue
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config" / "queue.json"

DEFAULTS = {
    "paused": False,
    "start_at": None,          # Unix 秒；到点自动开跑，跑完清空
    "auto_retry": 1,           # 单个任务失败后自动重排次数（0 关闭）
    "watch": False,            # 监控 media/inbox 自动入队
    "watch_interval": 60,      # 秒
    "skip_done": True,         # 同名同大小的已完成任务不再重复跑
}


def load():
    st = dict(DEFAULTS)
    try:
        if CONFIG.exists():
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                st.update({k: v for k, v in raw.items() if k in DEFAULTS})
    except Exception:
        pass
    return st


def save(st):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_name(f"queue.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)
    return st


class JobQueue:
    """可排序队列，用 deque + Condition 自己实现。

    为什么不用 queue.Queue：暂停时 worker 会阻塞在 get() 上，此时入队的第一个
    任务会被它「拿在手里但还没跑」，队列快照里凭空少一条（实测批量入队 2 个
    只显示 1 个）。这里改成「闸门通过后才取，且取完再确认一次闸门」，
    被暂停拦下的任务立刻放回队首，顺序与可见性都不变。
    """

    def __init__(self):
        self._dq = collections.deque()
        self._cv = threading.Condition()

    def put(self, tid):
        with self._cv:
            self._dq.append(tid)
            self._cv.notify()

    def push_front(self, tid):
        with self._cv:
            self._dq.appendleft(tid)
            self._cv.notify()

    def get(self, timeout=None):
        with self._cv:
            if not self._cv.wait_for(lambda: bool(self._dq), timeout):
                raise queue.Empty()
            return self._dq.popleft()

    def task_done(self):
        pass                            # 兼容原调用；没有 join 需求

    def snapshot(self):
        with self._cv:
            return list(self._dq)

    def move(self, tid, delta):
        with self._cv:
            try:
                i = self._dq.index(tid)
            except ValueError:
                return False
            j = max(0, min(len(self._dq) - 1, i + delta))
            if i == j:
                return False
            items = list(self._dq)
            items.insert(j, items.pop(i))
            self._dq.clear()
            self._dq.extend(items)
            return True

    def remove(self, tid):
        with self._cv:
            try:
                self._dq.remove(tid)
                return True
            except ValueError:
                return False


class Gate:
    """队列闸门：暂停 / 定时开始。改状态后 notify() 立刻放行，不用等轮询。"""

    def __init__(self, state):
        self.state = state
        self.event = threading.Event()

    def notify(self):
        self.event.set()

    def allowed(self):
        """只判断、不产生副作用（不清 start_at），供取到任务后二次确认。"""
        st = self.state
        if st.get("paused"):
            return False
        at = st.get("start_at")
        return not (at and time_now() < float(at))

    def wait(self):
        while True:
            # 【修复 v2.4】必须放在 allowed() 之前：原来 allowed() 先 return，
            # 「到点清标记」这段永远执行不到，过期的定时会一直留在
            # config/queue.json，前端每次刷新都显示一个昨天的「定时」。
            at = self.state.get("start_at")
            if at and not self.state.get("paused") and time_now() >= float(at):
                self.state["start_at"] = None
                save(self.state)
                return
            if self.allowed():
                return
            self.event.wait(timeout=2)
            self.event.clear()


def time_now():
    import time
    return time.time()
