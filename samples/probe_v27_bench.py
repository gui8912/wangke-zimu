# -*- coding: utf-8 -*-
"""v2.7 基准：Paraformer 分片并行到底几路最划算（20 核 Ultra 7 265K）。

v2.4 只测过 1 / 2 / 4 路，默认取 2。20 核机器上 2 路各 8 线程只用到 16 线程，
还剩 4 个核空转；本脚本补测 3 路（各 5 线程），并复测 2 / 4 路，
用同一段 600 秒真实课程、同样的低能量切片，比较墙钟、整机 CPU、文本一致率。

为什么每路线程数取 16//workers：v2.4 已实测「单路 20 线程」只比 4 线程快 6%，
说明瓶颈不在线程数，而在单条解码链喂不满核心；多路才是有效手段。

运行：python samples/probe_v27_bench.py
"""
import difflib
import statistics
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np               # noqa: E402
import torch                     # noqa: E402
import transcribe as T           # noqa: E402

SRC = sorted((BASE / "out").glob("*16k.wav"), key=lambda p: p.stat().st_size)[-1]
SECONDS = 600
SR = 16000
CHUNK = 150                       # 600s → 4 片，足够喂 2/3/4 路
TMP = Path(tempfile.mkdtemp(prefix="v27bench_"))


class Sampler:
    def __init__(self):
        self.cpu, self.stop = [], threading.Event()

    def _run(self):
        try:
            import psutil
        except Exception:
            return
        psutil.cpu_percent(None)
        while not self.stop.is_set():
            self.cpu.append(psutil.cpu_percent(None))
            time.sleep(0.4)

    def __enter__(self):
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join(timeout=2)

    def avg(self):
        return statistics.mean(self.cpu) if self.cpu else 0.0


def cut():
    with wave.open(str(SRC), "rb") as w:
        n = min(int(SECONDS * w.getframerate()), w.getnframes())
        raw = w.readframes(n)
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    dst = TMP / "clip600.wav"
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
    return dst


def main():
    print(f"音源 {SRC.name}  取前 {SECONDS}s", flush=True)
    wav = cut()
    hotword = T.load_hotwords(T.HOTWORD_FILE)
    cfg = T.MODELS["paraformer"]
    model = T.get_model(cfg, "cpu")
    gen = dict(batch_size_s=300, disable_pbar=True)
    if hotword:
        gen["hotword"] = hotword
    T.FUNASR_CHUNK_SEC = CHUNK

    results = []
    for workers in (2, 3, 4):
        T.FUNASR_WORKERS = workers
        with Sampler() as s:
            t0 = time.time()
            item = T._run_funasr_parallel(wav, model, dict(gen), print, None)
            dt = time.time() - t0
        results.append({"w": workers, "sec": round(dt, 1),
                        "cpu": round(s.avg()),
                        "text": item.get("text") or ""})
        print(f"{workers} 路 / 各 {max(2, 16 // workers)} 线程："
              f"{dt:6.1f}s  CPU {s.avg():5.1f}%  字数 {len(item.get('text') or '')}",
              flush=True)

    base = results[0]
    print("\n=== 汇总（600s 音频，4 片）===")
    for r in results:
        ratio = difflib.SequenceMatcher(None, base["text"], r["text"]).ratio()
        print(f"{r['w']} 路：{r['sec']:6.1f}s  相对 2 路 {base['sec']/r['sec']:4.2f}×  "
              f"CPU {r['cpu']:3d}%  文本一致 {ratio*100:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
