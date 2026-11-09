# -*- coding: utf-8 -*-
"""本次审计修复的回归测试：安全边界 + 参数校验 + 新功能"""

import json
import sys
from pathlib import Path

import requests

import apibase                    # 【新增 v2.9】端口可能不是 7870
BASE = apibase.base()
S = requests.Session()
S.trust_env = False        # 本机有代理环境变量，走代理会丢 POST body

ROOT = Path(__file__).resolve().parent.parent
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"[OK  ] {name}")
    else:
        fail += 1
        print(f"[FAIL] {name}  {detail}")


print("=== 1. 路径穿越（原 startswith 前缀绕过）===")
# OUT 的兄弟目录 out_evil：以 "out" 开头，旧的 startswith 会误放行
evil = ROOT / "out_evil"
evil.mkdir(exist_ok=True)
(evil / "secret.txt").write_text("should not leak", encoding="utf-8")
try:
    r = S.get(f"{BASE}/api/download", params={"path": str(evil / "secret.txt")}, timeout=10)
    check("兄弟目录 out_evil 被拒绝", r.status_code == 400, f"status={r.status_code}")
    r2 = S.get(f"{BASE}/api/download", params={"path": str(ROOT / "server.py")}, timeout=10)
    check("项目根文件被拒绝", r2.status_code == 400, f"status={r2.status_code}")
    r3 = S.get(f"{BASE}/api/download", params={"path": str(ROOT / "out" / ".." / "server.py")},
               timeout=10)
    check("../ 回溯被拒绝", r3.status_code == 400, f"status={r3.status_code}")
finally:
    (evil / "secret.txt").unlink(missing_ok=True)
    evil.rmdir()

print("\n=== 2. audio 路径穿越 ===")
r = S.get(f"{BASE}/api/audio/../../server.py", timeout=10)
check("audio ../../ 被拒绝", r.status_code in (400, 404), f"status={r.status_code}")

print("\n=== 3. stem 穿越（写到 out 之外）===")
r = S.post(f"{BASE}/api/save",
           json={"stem": "../../escape_test", "cues": [{"start": 0, "end": 1, "text": "x"}]},
           timeout=10)
check("save 返回成功", r.status_code == 200, r.text[:120])
check("没有写到项目根", not (ROOT / "escape_test.edited.srt").exists())
check("落在 out 内", (ROOT / "out" / ".._.._escape_test.edited.srt").exists()
      or any(ROOT.joinpath("out").glob("*escape_test*")), 
      str(list(ROOT.joinpath("out").glob("*escape_test*")))[:200])

print("\n=== 4. 参数校验 ===")
r = S.post(f"{BASE}/api/transcribe", data={"file_id": "nope", "model": "gpt-9"}, timeout=10)
check("非法 model 在入口被拒（400）", r.status_code == 400, f"status={r.status_code} {r.text[:80]}")

r = S.post(f"{BASE}/api/reclean", files={"file_id": (None, "x"), "cues_json": (None, "{bad json")},
           timeout=10)
check("非法 JSON 返回 400 而不是 500", r.status_code == 400, f"status={r.status_code}")

r = S.post(f"{BASE}/api/revise", json={"pass": "abc", "text": "x"}, timeout=10)
check("pass 非数字返回 400", r.status_code == 400, f"status={r.status_code}")
r = S.post(f"{BASE}/api/revise", json={"pass": 99, "text": "x"}, timeout=10)
check("pass 越界返回 400", r.status_code == 400, f"status={r.status_code}")

r = S.post(f"{BASE}/api/revise/save", json={"stem": "t", "pass": "abc", "text": "x"}, timeout=10)
check("revise/save pass 非数字返回 400", r.status_code == 400, f"status={r.status_code}")

print("\n=== 5. VTT 导出 ===")
cues = [{"start": 0.0, "end": 1.5, "text": "各位同仁，大家好。"},
        {"start": 1.5, "end": 62.345, "text": "今天讲两品一械。"}]
r = S.post(f"{BASE}/api/export", json={"stem": "测试课程", "cues": cues, "kind": "vtt"}, timeout=10)
check("vtt 返回 200", r.status_code == 200, r.text[:80])
body = r.text
check("以 WEBVTT 开头", body.startswith("WEBVTT"), body[:20])
check("时间码用点号", "00:00:01.500 --> 00:01:02.345" in body, body[:160])
check("包含字幕正文", "两品一械" in body)
cd = r.headers.get("Content-Disposition", "")
check("文件名扩展名是 .vtt", ".vtt" in cd, cd)

print("\n=== 6. SRT 毫秒进位 ===")
r = S.post(f"{BASE}/api/export",
           json={"stem": "进位", "cues": [{"start": 59.9999, "end": 60.0001, "text": "x"}],
                 "kind": "srt"}, timeout=10)
check("59.9999 不进位成 60 秒错误格式", "00:01:00,000" in r.text, r.text[:120])

print("\n=== 7. TTS limit 夹取 ===")
r = S.post(f"{BASE}/api/revise/tts", json={"text": "测试" * 5, "limit": 999999}, timeout=90)
d = r.json()
check("limit 超大不会报错", r.status_code == 200, str(d)[:120])
if r.status_code == 200:
    check("truncated 字段存在", "truncated" in d, str(d))

print(f"\n{'='*50}\n通过 {ok} / {ok + fail}")
sys.exit(0 if fail == 0 else 1)
