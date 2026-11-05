# -*- coding: utf-8 -*-
"""波形峰值解码实测：用 samples/test_lesson.mp3。"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import server  # noqa: E402

src = BASE / "samples" / "test_lesson.mp3"
t0 = time.time()
d = server._compute_peaks(src)
cost = time.time() - t0
print("duration:", d["duration"], "s, buckets:", len(d["mins"]), "elapsed:", round(cost, 2), "s")
assert d["duration"] > 5, d
assert len(d["mins"]) == len(d["maxs"]) > 10
assert all(-1.01 <= v <= 1.01 for v in d["mins"] + d["maxs"])
print("PEAKS OK")
