# -*- coding: utf-8 -*-
"""v2.0 任务栏专项回归：进度回调 + 批量操作。

unit() —— 假模型验证进度百分比 / 阶段文本 / 回调异常不拖垮识别；
batch() —— 直接调 server.tasks_batch（把注册表指到临时目录，绝不碰真实任务）；
guard() —— 前端 / 接口的关键接线做静态检查。

运行：python samples/test_v20.py
"""
import importlib
import queue
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
import transcribe as T  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")


# ────────────────────────── 进度回调 ──────────────────────────

class FakeStamp:
    def __init__(self, s, e):
        self.start_time, self.end_time = s, e


class FakeResult:
    def __init__(self, text, stamps):
        self.text, self.time_stamps = text, stamps


class FakeModel:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, language=None, return_time_stamps=False):
        self.calls += 1
        return [FakeResult(f"片{self.calls}。", [FakeStamp(0.0, 1.0)])]


def fake_pipeline(path, progress_list):
    """用假模型跑一遍 _run_qwen3，收集所有进度事件。"""
    old = T.QWEN3_WINDOW_SEC
    T.QWEN3_WINDOW_SEC = 40.0
    cfg = {"model": "FAKE/v20", "aligner": "FAKE/aligner", "dtype": "float32"}
    T._QWEN3_CACHE[cfg["model"]] = FakeModel()
    try:
        T._run_qwen3(path, cfg, "cpu", "Chinese", lambda m: None,
                     lambda pct, stage: progress_list.append((round(pct, 1), stage)))
    finally:
        T.QWEN3_WINDOW_SEC = old
        T._QWEN3_CACHE.pop(cfg["model"], None)


def make_wav(path: Path, sec=100.0, sr=16000):
    n = int(sec * sr)
    t = np.arange(n, dtype=np.float32) / sr
    a = (0.3 * 32767 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
    a[(np.arange(n) // sr) % 3 == 2] = 0
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(a.tobytes())


def unit(tmp: Path):
    print("[单元] 进度回调")
    wav = tmp / "p.wav"
    make_wav(wav)

    events = []
    fake_pipeline(wav, events)
    pcts = [p for p, _ in events]
    ok("有进度事件", len(events) >= 4, events)
    ok("从 12% 起步", pcts[0] == 12.0, pcts[:2])
    ok("识别收在 90%", pcts[-1] == 90.0, pcts[-2:])
    ok("百分比单调递增", all(pcts[i] >= pcts[i - 1] for i in range(1, len(pcts))), pcts)
    ok("百分比不越界", all(0 <= p <= 100 for p in pcts), pcts)
    ok("阶段含分片序号", any("识别 1/" in s for _, s in events), events)
    ok("阶段含完成", events[-1][1] == "识别完成", events[-1])

    # 回调抛异常不能把识别带崩：_emit 必须吞掉
    seen = []
    collect = lambda p, s: seen.append((p, s))
    T._emit(lambda p, s: (_ for _ in ()).throw(RuntimeError("前端断了")), 50, "测试")
    T._emit(collect, 120, "越界")
    T._emit(collect, -5, "越界")
    ok("回调异常被吞", True)
    ok("上限夹到 100", seen[0][0] == 100.0, seen)
    ok("下限夹到 0", seen[1][0] == 0.0, seen)
    ok("主流程接受 progress 参数", "progress=None" in
       (BASE / "transcribe.py").read_text(encoding="utf-8").split("def run_pipeline")[1][:600])


# ────────────────────────── 批量操作 ──────────────────────────

def batch(tmp: Path):
    print("[批量] 删除 / 取消 / 重试 / 清空")
    import server as S

    jobs = tmp / "jobs"; jobs.mkdir()
    media = tmp / "media"; media.mkdir()
    src = media / "lesson.mp4"; src.write_bytes(b"fake-video")
    S.JOBS, S.MEDIA = jobs, media
    import queue_state as QS
    S.JOB_Q = QS.JobQueue()        # 换掉有消费者线程的队列，重试不会真跑识别

    def mk(tid, status, path=None):
        t = {"id": tid, "status": status, "name": f"课程{tid}", "logs": [],
             "path": str(path or src), "created": 1, "progress": None}
        S.TASKS[tid] = t
        (jobs / f"{tid}.json").write_text('{"id":"%s"}' % tid, encoding="utf-8")
        return t

    S.TASKS.clear()
    mk("done", "done"); mk("err", "error"); mk("intr", "interrupted")
    mk("run", "running"); mk("que", "queued")

    r = S.tasks_batch({"action": "delete", "ids": ["done", "err", "intr", "run", "que"]})
    ok("删除终态 3 条", r["done"] == 3, r)
    ok("跳过处理中 2 条", len(r["skipped"]) == 2, r["skipped"])
    ok("运行中任务仍在", "run" in S.TASKS)
    ok("排队任务仍在", "que" in S.TASKS)
    ok("job 文件已删", not (jobs / "done.json").exists())
    ok("源文件默认保留", src.exists())

    r = S.tasks_batch({"action": "cancel", "ids": ["que", "run"]})
    ok("批量取消成功 1 条", r["done"] == 1, r)
    ok("运行中不可取消", len(r["skipped"]) == 1, r["skipped"])
    ok("排队任务已取消", S.TASKS["que"]["status"] == "cancelled")

    r = S.tasks_batch({"action": "retry", "ids": ["que"]})
    ok("批量重试成功", r["done"] == 1, r)
    ok("重试后回到 queued", S.TASKS["que"]["status"] == "queued")
    ok("重试清空旧日志/结果", S.TASKS["que"]["logs"] == [] and S.TASKS["que"]["result"] is None)

    mk("done2", "done")
    r = S.tasks_batch({"action": "clear", "ids": []})
    ok("清空只清终态", r["done"] == 1 and "run" in S.TASKS, r)
    ok("清空保留运行中", S.TASKS["run"]["status"] == "running")

    # 连源文件一起删：独占才删，被别的任务引用就保留
    p2 = media / "shared.mp4"; p2.write_bytes(b"x")
    mk("a", "done", p2); mk("b", "done", p2)
    S.tasks_batch({"action": "delete", "ids": ["a"], "purge_media": True})
    ok("共享源文件不删", p2.exists())
    p3 = media / "solo.mp4"; p3.write_bytes(b"y")
    mk("c", "done", p3)
    S.tasks_batch({"action": "delete", "ids": ["c"], "purge_media": True})
    # 【修改 v2.4】按张兆贵要求「源文件禁止删除」：即使旧客户端传
    # purge_media=True，独占的源文件也必须保留。
    ok("独占源文件也禁止删", p3.exists())
    ok("out 产物永不删（未调用删除）", (BASE / "out").exists())

    # 非法参数
    bad1 = S.tasks_batch({"action": "rm", "ids": []})
    bad2 = S.tasks_batch({"action": "delete", "ids": "x"})
    ok("非法操作被拒", getattr(bad1, "status_code", 200) == 400, type(bad1).__name__)
    ok("ids 非数组被拒", getattr(bad2, "status_code", 200) == 400, type(bad2).__name__)


def parallel(tmp: Path):
    """【新增 v2.1】并行不能打乱顺序：故意让第 1 片最慢，看拼回是否仍按片号。"""
    print("[并行] 乱序完成仍按片号拼回")
    import threading
    import time

    class SlowFake:
        def __init__(self):
            self.n, self.lock = 0, threading.Lock()

        def transcribe(self, audio, language=None, return_time_stamps=False):
            with self.lock:
                self.n += 1
                k = self.n
            if k == 1:
                time.sleep(0.12)             # 第 1 片最慢，必定最后完成
            return [FakeResult(f"片{k}。", [FakeStamp(0.0, 1.0)])]

    wav = tmp / "para.wav"
    make_wav(wav, 100.0)
    old_w, old_win = T.QWEN3_WORKERS, T.QWEN3_WINDOW_SEC
    T.QWEN3_WORKERS, T.QWEN3_WINDOW_SEC = 2, 40.0
    cfg = {"model": "FAKE/parallel", "aligner": "FAKE/a", "dtype": "float32"}
    T._QWEN3_CACHE[cfg["model"]] = SlowFake()
    events = []
    try:
        r = T._run_qwen3(wav, cfg, "cpu", "Chinese", lambda m: None,
                         lambda p, s: events.append((p, s)))
    finally:
        T.QWEN3_WORKERS, T.QWEN3_WINDOW_SEC = old_w, old_win
        T._QWEN3_CACHE.pop(cfg["model"], None)

    import re
    parts = re.findall(r"片(\d+)。", r["text"])
    ok("文本按片号升序", parts == sorted(parts, key=int), parts)
    ok("文本无丢失", len(parts) >= 2, parts)
    ts = r["timestamp"]
    ok("时间戳按片递增", all(ts[i][0] >= ts[i - 1][0] for i in range(1, len(ts))), ts)
    ok("首片偏移为 0", ts[0][0] == 0, ts[0])
    ok("末片有偏移", ts[-1][0] > 0, ts[-1])
    ok("进度仍单调", all(events[i][0] >= events[i - 1][0]
                     for i in range(1, len(events))), events)


def guards():
    print("[护栏] 前后端接线")
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    web = (BASE / "web/index.html").read_text(encoding="utf-8")
    ok("任务列表返回 progress", '"progress": t.get("progress")' in srv)
    ok("worker 传 progress", "progress=progress" in srv)
    ok("批量接口已注册", '@app.post("/api/tasks/batch")' in srv)
    for key in ("btnBatchRetry", "btnBatchCancel", "btnBatchDelete", "btnBatchClear",
                "selAll", "btnSelInv", "btnManual", "taskEta", "batchAction", "initTaskBar"):
        ok("前端 " + key, key in web)
    # 【新增 v2.4】「连源文件一起删」入口已整体下线
    ok("前端无 purgeMedia 控件", 'id="purgeMedia"' not in web)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="v20_"))
    unit(tmp)
    parallel(tmp)
    batch(tmp)
    guards()
    bad = [n for n, c in PASS if not c]
    print(f"\n{len(PASS) - len(bad)}/{len(PASS)} 通过")
    if bad:
        print("失败：" + "、".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
