"""端到端测试：上传 → 转写 → 轮询 → 重新清理 → 保存"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:7870"


def post_form(path, fields, files=None):
    boundary = "----test" + str(int(time.time() * 1000))
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode()
    if files:
        for k, (name, data) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                     f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                     ).encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read())


def post_json(path, obj):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read())


def get(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BASE + path, timeout=60) as r:
        return json.loads(r.read())


src = Path("samples/test_lesson.mp3")
print("[1] 上传", flush=True)
up = post_form("/api/upload", {}, {"file": (src.name, src.read_bytes())})
print("   ", up, flush=True)

print("[2] 转写（paraformer）", flush=True)
t = post_form("/api/transcribe", {
    "file_id": up["file_id"], "model": "paraformer",
    "clean": "true", "language": "Chinese"})
tid = t["task_id"]
print("    task:", tid, flush=True)

for _ in range(180):
    time.sleep(2)
    s = get("/api/task/" + tid)
    if s["status"] in ("done", "error"):
        break
print("    状态:", s["status"], flush=True)
if s["status"] != "done":
    print("    错误:", s.get("error"), flush=True)
    print("\n".join(s["logs"][-10:]), flush=True)
    raise SystemExit(1)

res = s["result"]
print(f"    cues={len(res['cues'])} clean_cues={len(res['clean_cues'] or [])} "
      f"chars={res['chars']} elapsed={res['elapsed']}", flush=True)
print("    前两条:", json.dumps(res["clean_cues"][:2], ensure_ascii=False), flush=True)

print("[3] 重新清理", flush=True)
rc = post_form("/api/reclean", {
    "file_id": up["file_id"],
    "cues_json": json.dumps(res["cues"], ensure_ascii=False)})
print(f"    changed={rc['changed']}", flush=True)

print("[4] 保存校对稿", flush=True)
sv = post_json("/api/save", {"stem": "apitest", "cues": rc["cues"]})
print("   ", sv, flush=True)

print("[5] 下载检查", flush=True)
p = sv["srt"]
req = urllib.request.Request(BASE + "/api/download?path=" + urllib.parse.quote(p))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(req, timeout=30) as r:
    content = r.read().decode()
print("    前 3 行:", content.splitlines()[:3], flush=True)
print("ALL OK", flush=True)
