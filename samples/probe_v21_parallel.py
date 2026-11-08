# -*- coding: utf-8 -*-
"""v2.1 真机验证：共享同一个模型实例，两片并行跑出来的稿子必须和顺序跑一致。

线程安全是这次并行改动最大的风险点：qwen_asr 的 transcribe() 本身无共享可变状态，
但必须用真实模型确认一次，不能只靠假模型。同一段 240s 真实课程跑两遍：
  W=1 顺序   → 基准
  W=2 并行   → 对比文本、字级时间戳、耗时、内存

运行：python samples/probe_v21_parallel.py
"""
import difflib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import psutil  # noqa: E402
import transcribe as T  # noqa: E402

SRC = (BASE / "out" /
       "e78e052163be_新版《医疗器械生产质量管理规范检查指导原则》深度解读.recode.16k.wav")
DUR = 240


def make_clip(dst: Path):
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([exe, "-y", "-ss", "300", "-t", str(DUR), "-i", str(SRC),
                        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "ignore")[-400:]


def run(clip, workers, tag):
    T.QWEN3_WORKERS = workers
    T.QWEN3_WINDOW_SEC = 120.0
    T.QWEN3_EXPAND_SEC = min(5.0, 120.0 / 6.0)
    logs = []
    proc = psutil.Process()
    peak = [proc.memory_info().rss / 1048576]
    t0 = time.time()
    r = T._run_qwen3(clip, T.MODELS["qwen3-asr"], "cpu", "Chinese",
                     lambda m: logs.append(m),
                     lambda p, s: peak.append(max(peak[0], proc.memory_info().rss / 1048576)))
    cost = time.time() - t0
    print(f"[{tag}] {cost:.0f}s / {len(r['text'])} 字 / {len(r['timestamp'])} 个时间戳 "
          f"/ 峰值 {max(peak):.0f}MB", flush=True)
    for m in logs:
        print("   ·", m, flush=True)
    return r, cost


def main():
    if not SRC.exists():
        print("缺少源音频")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="para21_"))
    clip = tmp / "clip.wav"
    make_clip(clip)
    print(f"[clip] {DUR}s 就绪", flush=True)

    a, ta = run(clip, 1, "顺序 W=1")
    b, tb = run(clip, 2, "并行 W=2")

    same_text = a["text"] == b["text"]
    ratio = difflib.SequenceMatcher(None, a["text"], b["text"]).ratio()
    same_ts = len(a["timestamp"]) == len(b["timestamp"]) and all(
        abs(x[0] - y[0]) <= 2 and abs(x[1] - y[1]) <= 2
        for x, y in zip(a["timestamp"], b["timestamp"]))
    print("\n=== 结论 ===", flush=True)
    print(f"文本完全一致：{same_text}（相似度 {ratio:.4f}）", flush=True)
    print(f"字级时间戳一致（±2ms）：{same_ts}", flush=True)
    print(f"加速：{ta / tb:.2f}×", flush=True)
    if not same_text:
        sm = difflib.SequenceMatcher(None, a["text"], b["text"])
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op != "equal":
                print(f"  差异 {op}: 顺序[{i1}:{i2}]={a['text'][i1:i2]!r} "
                      f"并行[{j1}:{j2}]={b['text'][j1:j2]!r}", flush=True)
                break
    ok = same_ts and ratio > 0.999
    print("✓ 并行可用" if ok else "✗ 并行结果不可靠，请保持 W=1", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
