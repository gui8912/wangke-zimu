# -*- coding: utf-8 -*-
"""v2.0 性能基准：CPU 到底吃满没有、分片能不能并行。

三组对比（都用同一段真实课程音频的两片 120s 窗口）：
  A 顺序 + 默认线程数        —— 现状
  B 顺序 + torch 线程数减半  —— 看是不是线程开太多反而互相抢
  C 两片并行（各 1 线程）    —— 看能不能把空闲核心用起来

输出：墙钟时间、CPU 平均占用、内存峰值。结论直接决定 v2.1 怎么改。

运行：python samples/probe_v20_bench.py
"""
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import psutil  # noqa: E402
import torch  # noqa: E402

SRC = (BASE / "out" /
       "e78e052163be_新版《医疗器械生产质量管理规范检查指导原则》深度解读.recode.16k.wav")
ASR_DIR = r"D:\AI\models\modelscope\Qwen\Qwen3-ASR-1___7B"
ALIGNER_DIR = r"D:\AI\models\modelscope\Qwen\Qwen3-ForcedAligner-0___6B"
WINDOW = 120          # 每片秒数
CORES = psutil.cpu_count(logical=True)
PHYS = psutil.cpu_count(logical=False)
print(f"[env] 逻辑核 {CORES} / 物理核 {PHYS} / torch 默认线程 {torch.get_num_threads()}",
      flush=True)


def cut_clip(dst: Path, start: float, dur: float):
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([exe, "-y", "-ss", str(start), "-t", str(dur), "-i", str(SRC),
                        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "ignore")[-400:]


class Sampler:
    """后台采样整机 CPU 与进程 RSS，取均值/峰值。"""

    def __init__(self):
        self.cpu, self.rss, self.stop = [], [], threading.Event()
        self.p = psutil.Process()

    def _run(self):
        psutil.cpu_percent(None)
        while not self.stop.is_set():
            self.cpu.append(psutil.cpu_percent(None))
            self.rss.append(self.p.memory_info().rss / 1048576)
            time.sleep(0.5)

    def __enter__(self):
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join(timeout=2)

    def report(self):
        return (statistics.mean(self.cpu) if self.cpu else 0,
                max(self.rss) if self.rss else 0)


def load_model():
    from qwen_asr import Qwen3ASRModel
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    t0 = time.time()
    m = Qwen3ASRModel.from_pretrained(
        ASR_DIR, dtype=torch.bfloat16, device_map="cpu",
        forced_aligner=ALIGNER_DIR,
        forced_aligner_kwargs=dict(dtype=torch.bfloat16, device_map="cpu"),
        max_inference_batch_size=1, max_new_tokens=2048)
    print(f"[load] {time.time() - t0:.1f}s", flush=True)
    return m


def read_pcm(p: Path):
    with wave.open(str(p), "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    import numpy as np
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0, 16000


def run_one(model, clip):
    t0 = time.time()
    r = model.transcribe(audio=clip, language="Chinese", return_time_stamps=True)
    return time.time() - t0, (r[0].text if r else "")


def bench(name, model, clips, threads=None, parallel=False):
    if threads:
        torch.set_num_threads(threads)
    with Sampler() as s:
        t0 = time.time()
        if parallel:
            out, errs = {}, []

            def job(i, c):
                try:
                    out[i] = run_one(model, c)
                except Exception as exc:      # noqa: BLE001
                    errs.append(exc)

            ths = [threading.Thread(target=job, args=(i, c)) for i, c in enumerate(clips)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            if errs:
                raise errs[0]
            costs = [out[i][0] for i in range(len(clips))]
        else:
            costs = [run_one(model, c)[0] for c in clips]
        wall = time.time() - t0
    cpu, rss = s.report()
    print(f"[{name}] 墙钟 {wall:.0f}s / 单片 {[round(c) for c in costs]} / "
          f"CPU {cpu:.0f}% / 峰值 RSS {rss:.0f}MB", flush=True)
    return wall, cpu, rss


def main():
    if not SRC.exists():
        print("缺少源音频")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="bench20_"))
    a, b = tmp / "a.wav", tmp / "b.wav"
    cut_clip(a, 300, WINDOW)
    cut_clip(b, 500, WINDOW)
    clip_a, clip_b = read_pcm(a), read_pcm(b)
    print(f"[clip] 两片各 {WINDOW}s，就绪", flush=True)

    model = load_model()
    torch.set_num_threads(CORES)

    results = []
    results.append(("A 顺序/默认线程", *bench("A", model, [clip_a, clip_b], threads=CORES)))
    results.append(("B 顺序/线程减半", *bench("B", model, [clip_a, clip_b],
                                          threads=max(4, CORES // 2))))
    results.append(("C 两片并行/各 8 线程", *bench("C", model, [clip_a, clip_b],
                                             threads=8, parallel=True)))

    print("\n=== 汇总（两片合计 %.0fs 音频）===" % (WINDOW * 2), flush=True)
    base = results[0][1]
    for name, wall, cpu, rss in results:
        print(f"{name:22} 墙钟 {wall:6.0f}s  加速 {base / wall:4.2f}×  "
              f"CPU {cpu:3.0f}%  峰值 {rss:5.0f}MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
