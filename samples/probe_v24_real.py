# -*- coding: utf-8 -*-
"""真机验证：79 分钟课件用 Paraformer 2 路并行 vs 之前整段跑的 400.2s。

用法：python samples/probe_v24_real.py
只读已有产物做对比，不写任何 out/ 文件。
"""
import difflib
import glob
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import transcribe as T  # noqa: E402

wav = sorted((BASE / "out").glob("*16k.wav"), key=lambda p: p.stat().st_size)[-1]
js = sorted(glob.glob(str(BASE / "out" / "*.json")), key=lambda p: Path(p).stat().st_size)[-1]
# 精确找同名 baseline
stem = wav.name.replace(".16k.wav", "")
cand = BASE / "out" / (stem + ".json")
if cand.exists():
    js = str(cand)
base = json.loads(Path(js).read_text(encoding="utf-8"))
old_text = base.get("text") or ""
old_ts = base.get("timestamps") or []

print(f"音源：{wav.name}", flush=True)
print(f"基线：{Path(js).name}  {len(old_text)} 字 / {len(old_ts)} 时间戳 / 400.2s（整段单路）",
      flush=True)
hotword = T.load_hotwords(T.HOTWORD_FILE)
events = []
t0 = time.time()
item = T.transcribe(wav, T.MODELS["paraformer"], hotword, "cpu", "Chinese",
                    log=lambda m: print("  ", m, flush=True),
                    progress=lambda pct, stage="": events.append((pct, stage)))
dt = time.time() - t0
new_text = item.get("text") or ""
new_ts = item.get("timestamp") or []
ratio = difflib.SequenceMatcher(None, old_text, new_text).ratio()
print("\n=== 结果 ===", flush=True)
print(f"并行耗时 {dt:.1f}s   提速 {400.2/dt:.2f}x", flush=True)
print(f"字数 {len(old_text)} → {len(new_text)}   时间戳 {len(old_ts)} → {len(new_ts)}",
      flush=True)
print(f"文本一致率 {ratio*100:.1f}%", flush=True)
print(f"进度事件 {len(events)} 个，首 {events[:2]}，末 {events[-2:]}", flush=True)
print(f"时间戳末 {new_ts[-1] if new_ts else '无'}", flush=True)
