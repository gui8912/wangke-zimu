# -*- coding: utf-8 -*-
"""实测 Paraformer 能否吃满 20 核并缩短等待（v2.4 CPU 基准）。

拿真实课程 wav 的前 300 秒，比较：
  A 单路 / torch 4 线程（现状）
  B 单路 / torch 20 线程（把核数给满）
  C 2 路分片并行 / 每路 8 线程
  D 4 路分片并行 / 每路 4 线程

【重要实测结论】同一进程创建多个 FunASR AutoModel 实例会在加载第 2 个时
直接段错误（exit 139），所以本脚本只加载 1 个实例，并发档复用同一实例，
测的是「单实例多线程是否能安全并行」。先跑安全的 A/B，再跑可能崩的 C/D，
即使 C/D 崩了，前两档数据也已经 flush 到日志里。
用法：python samples/probe_v24_cpu.py
"""
import difflib
import os
import sys
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import torch                     # noqa: E402
import numpy as np               # noqa: E402

WAV = sorted((BASE / "out").glob("*16k.wav"), key=lambda p: p.stat().st_size)[-1]
SECONDS = 300
SR = 16000
TMP = Path(tempfile.mkdtemp(prefix="v24cpu_"))


def read_wav(path, seconds):
    with wave.open(str(path), "rb") as w:
        n = min(int(seconds * w.getframerate()), w.getnframes())
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def write_wav(path, pcm):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())


def make_parts(pcm, n):
    """在低能量处切，避免把一句话从中间劈开。"""
    step = len(pcm) // n
    win = int(0.02 * SR)
    energy = np.convolve(np.abs(pcm), np.ones(win, dtype=np.float32) / win, mode="same")
    bounds = [0]
    for i in range(1, n):
        center = i * step
        lo, hi = max(1, center - 8 * SR), min(len(pcm) - 1, center + 8 * SR)
        bounds.append(int(lo + int(np.argmin(energy[lo:hi]))))
    bounds.append(len(pcm))
    out = []
    for i in range(n):
        p = TMP / f"part{n}_{i}.wav"
        write_wav(p, pcm[bounds[i]:bounds[i + 1]])
        out.append((p, bounds[i] / SR))
    return out


def load_models(n):
    from funasr import AutoModel
    print(f"预热加载 {n} 个模型实例…", flush=True)
    t0 = time.time()
    ms = []
    for i in range(n):
        ms.append(AutoModel(model="paraformer-zh", vad_model="fsmn-vad",
                            punc_model="ct-punc", device="cpu",
                            disable_update=True, log_level="ERROR"))
        print(f"  实例 {i+1}/{n} 就绪 {time.time()-t0:.1f}s", flush=True)
    return ms


def run_one(model, path):
    r = model.generate(input=str(path), batch_size_s=300, disable_pbar=True)
    d = r[0] if isinstance(r, list) and r else {}
    return str(d.get("text") or ""), d.get("timestamp") or []


def bench(name, nthreads, models, parts):
    torch.set_num_threads(nthreads)
    t0 = time.time()
    if len(parts) == 1:
        text, ts = run_one(models[0], parts[0][0])
    else:
        with ThreadPoolExecutor(max_workers=len(parts)) as ex:
            futs = [ex.submit(run_one, models[i % len(models)], p)
                    for i, (p, _) in enumerate(parts)]
            got = [f.result() for f in futs]
        text = "".join(g[0] for g in got)
        ts = []
        for (_, off), (_, stamps) in zip(parts, got):
            off_ms = int(off * 1000)
            ts.extend([[a + off_ms, b + off_ms] for a, b in stamps])
    dt = time.time() - t0
    print(f"{name:<28} {dt:7.1f}s  字数 {len(text):5d}  时间戳 {len(ts):5d}"
          f"  速度 {SECONDS/dt:5.2f}x 实时", flush=True)
    return {"name": name, "sec": round(dt, 1), "text": text, "ts": ts}


def main():
    print(f"音源 {WAV.name}  取前 {SECONDS}s  临时目录 {TMP}", flush=True)
    pcm = read_wav(WAV, SECONDS)
    print(f"读到 {len(pcm)/SR:.0f}s 音频", flush=True)
    full = TMP / "full.wav"; write_wav(full, pcm)
    p1 = [(full, 0.0)]
    p2 = make_parts(pcm, 2)
    p4 = make_parts(pcm, 4)
    models = load_models(1)      # 多实例会段错误，见模块 docstring

    results = []
    results.append(bench("A 单路 4 线程（现状）", 4, models, p1))
    results.append(bench("B 单路 20 线程", 20, models, p1))
    results.append(bench("C 2 路并行 / 各 8 线程", 8, models, p2))
    results.append(bench("D 4 路并行 / 各 4 线程", 4, models, p4))

    base = results[0]["text"]
    print("\n=== 与现状的文本一致率 ===", flush=True)
    for r in results[1:]:
        ratio = difflib.SequenceMatcher(None, base, r["text"]).ratio()
        print(f"  {r['name']:<28} {ratio*100:5.1f}%   加速 "
              f"{results[0]['sec']/r['sec']:.2f}x", flush=True)
    print("\n结论见 reports；临时文件保留在", TMP, flush=True)


if __name__ == "__main__":
    main()
