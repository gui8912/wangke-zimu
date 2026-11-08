# -*- coding: utf-8 -*-
"""v1.9 真机验证：拿真实课程音频里的一段 6 分 40 秒，走修复后的分片识别。

目的不是比精度，而是确认三件事：
  ① 多片识别不会再把进程打死（原实现 exit 139）；
  ② 片间时间戳按偏移拼回后单调递增，末尾能落在音频总时长范围内；
  ③ 内存峰值恒定在「单片」水平，不随音频变长而线性上涨。

运行：python samples/probe_v19_longaudio.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import transcribe as T  # noqa: E402

SRC = BASE / "out" / "e78e052163be_新版《医疗器械生产质量管理规范检查指导原则》深度解读.recode.16k.wav"
START, DUR = 300, 400          # 从第 5 分钟起截 400 秒（约 3 个窗口）


def rss_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1048576
    except Exception:
        return -1.0


def wav_seconds(p: Path) -> float:
    with wave.open(str(p), "rb") as w:
        return w.getnframes() / w.getframerate()


def main():
    if not SRC.exists():
        print("缺少源音频：", SRC)
        return 2
    assert wav_seconds(SRC) > START + DUR, "源音频长度不足"

    tmp = Path(tempfile.mkdtemp(prefix="probe19_"))
    clip = tmp / "clip.wav"
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [exe, "-y", "-ss", str(START), "-t", str(DUR), "-i", str(SRC),
           "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(clip)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print("切片失败：", r.stderr.decode("utf-8", "ignore")[-500:])
        return 2
    print(f"[clip] {wav_seconds(clip):.1f}s  RSS={rss_mb():.0f}MB", flush=True)

    base_rss = rss_mb()
    peak = [base_rss]
    logs = []

    def log(msg):
        logs.append(str(msg))
        cur = rss_mb()
        peak[0] = max(peak[0], cur)
        print(f"  · {msg}   [RSS {cur:.0f}MB]", flush=True)

    t0 = time.time()
    item = T._run_qwen3(clip, T.MODELS["qwen3-asr"], "cpu", "Chinese", log)
    cost = time.time() - t0

    ts = item["timestamp"]
    print(f"\n[结果] {len(item['text'])} 字 / {len(ts)} 个字级时间戳 / 耗时 {cost:.0f}s "
          f"（{DUR / cost:.2f}× 实时）", flush=True)
    print(f"[内存] 起始 {base_rss:.0f}MB → 峰值 {peak[0]:.0f}MB", flush=True)
    print(f"[文本] {item['text'][:120]}…", flush=True)
    if ts:
        print(f"[时间戳] 首 {ts[0]} 末 {ts[-1]}（音频 {DUR}s）", flush=True)

    ok = True
    if ts and ts[-1][1] > (DUR + 2) * 1000:
        print("✗ 末尾时间戳超出音频时长"); ok = False
    if ts and any(ts[i][0] < ts[i - 1][0] for i in range(1, len(ts))):
        print("✗ 时间戳出现回退"); ok = False
    if cost > DUR * 4:
        print("✗ 慢得离谱（>4× 实时）"); ok = False
    print("✓ 真机验证通过" if ok else "✗ 真机验证未通过", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
