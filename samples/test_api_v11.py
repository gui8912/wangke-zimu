# -*- coding: utf-8 -*-
"""v1.1 接口全链路测试：时间戳 / 波形 / 对齐 / 保存 / 草稿。"""
import json
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:7870"
# 【修改 v2.7】不再写死任务 ID：从 /api/tasks 取最近一条已完成任务，
# 否则换机器/清过任务记录就 404，看起来像接口坏了。
TID = sys.argv[1] if len(sys.argv) > 1 else ""
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path):
    with opener.open(API + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


if not TID:
    listing = get("/api/tasks")
    done = [x for x in listing.get("tasks", []) if x.get("status") == "done"]
    assert done, "服务里没有已完成任务，请先转写一条再跑本测试"
    TID = done[0]["id"]

task = get("/api/task/" + TID)
assert task["status"] == "done", task
res = task["result"]
stem = res["stem"]
cues = res["cues"]
print("task done:", stem, len(cues), "cues")

meta = json.loads((BASE / "out" / f"{stem}.json").read_text(encoding="utf-8"))
ts = meta.get("timestamps") or []
print("timestamps:", len(ts))
assert len(ts) > 0

peaks = get(f"/api/peaks?stem={stem}&n=400")
print("peaks:", peaks.get("duration"), "s,", len(peaks.get("mins", [])), "points")
assert peaks.get("duration", 0) > 5 and len(peaks.get("mins", [])) > 100

doc = (BASE / "out" / f"{stem}.raw.txt").read_text(encoding="utf-8")
al = post("/api/align", {"stem": stem, "script": doc})
print("align:", al.get("similarity"), "matched", al.get("matched"), "/", al.get("total"),
      "cues", len(al.get("cues", [])))
assert al.get("similarity", 0) > 0.8 and al.get("cues")

bad = doc + "\n这句话是讲义里多出来的，录音里没有。"
al2 = post("/api/align", {"stem": stem, "script": bad})
warn = sum(1 for c in al2["cues"] if c.get("warn"))
print("align with extra sentence:", al2.get("similarity"), "warn cues", warn)
assert warn >= 1

save_cues = res.get("clean_cues") or res.get("cues")
save = post("/api/save", {"stem": stem, "cues": save_cues})
print("save:", save.get("ok"), save.get("srt"), len(save_cues), "cues")
assert save.get("ok")

draft = get(f"/api/draft?stem={stem}")
print("draft:", draft.get("exists"), len(draft.get("cues", [])),
      "newer", draft.get("draft_mtime", 0) > draft.get("raw_mtime", 0))
assert draft.get("exists") and len(draft["cues"]) == len(save_cues)
print("ALL V1.1 API TESTS PASSED")
