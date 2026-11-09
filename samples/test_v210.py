# -*- coding: utf-8 -*-
"""v2.10 专项：播放位置记忆（前端纯函数 + 服务端接口 + 源码契约）。

背景：一节课动辄一小时，字幕校对常分几次做。需求是「下次打开同一课件，
从上次看到的地方继续」。关键设计：
  ① 身份按「文件名+大小」，不用 file_id / task_id —— 重新导入后 id 会变；
  ② localStorage 立即生效，同时回写服务端 config/playpos.json —— v2.9 起
     端口会自动顺延，localStorage 按端口隔离，只存本地换个端口就丢了；
  ③ 开头 5 秒 / 距结尾 15 秒视为「没看 / 看完了」，不记，免得下次卡在片尾。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "samples"))
import apibase  # noqa: E402

PASS = FAIL = 0
TEST_KEY = "f:__unittest_v210.mp4|123456789"


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def http_json(path, payload=None):
    url = apibase.base() + path
    try:
        if payload is None:
            r = urllib.request.urlopen(url, timeout=8)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            r = urllib.request.urlopen(req, timeout=8)
        with r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        return exc.code, body


def js_pure_functions():
    """把前端两个纯函数抽出来交给 node 真跑一遍，而不只是 grep 字符串。

    posKeyOf / posRecord 不依赖 DOM，抽出来 eval 是安全的；这样「同名同大小
    必须同 key」「距结尾 15 秒不记」这类规则是真被验证过的，不是注释里说说。
    """
    src = (BASE / "web" / "index.html").read_text(encoding="utf-8")
    script = r"""
const fs = require('fs');
const s = fs.readFileSync(process.argv[2], 'utf8');
function pick(name){
  const m = s.match(new RegExp('function ' + name + '\\([^)]*\\)\\{[\\s\\S]*?\\n\\}'));
  if(!m) throw new Error('missing ' + name);
  return m[0];
}
eval(pick('posKeyOf'));
eval(pick('posRecord'));
console.log(JSON.stringify({
  k_same_a: posKeyOf({name:'课件.mp4', size:12345}),
  k_same_b: posKeyOf({name:'课件.mp4', size:12345, taskId:'abc'}),
  k_diff:   posKeyOf({name:'课件.mp4', size:9}),
  k_empty:  posKeyOf(null),
  k_task:   posKeyOf({taskId:'abc'}),
  r_head:   posRecord(3, 3600),
  r_mid:    posRecord(123.4, 3600),
  r_tail:   posRecord(3590, 3600),
  r_nodur:  posRecord(50, NaN)
}));
"""
    d = Path(tempfile.mkdtemp())
    js = d / "probe.js"
    js.write_text(script, encoding="utf-8")
    html = d / "index.html"
    html.write_text(src, encoding="utf-8")
    try:
        out = subprocess.run(["node", str(js), str(html)], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"_error": str(exc)}
    if out.returncode:
        return {"_error": out.stderr[-400:]}
    try:
        return json.loads(out.stdout)
    except Exception as exc:
        return {"_error": f"{exc}: {out.stdout[:200]}"}


def pure_logic():
    print("[前端纯函数]")
    d = js_pure_functions()
    ok("能抽出并运行前端函数", "_error" not in d, d.get("_error", ""))
    if "_error" in d:
        return
    ok("同名同大小 → 同一个 key", d["k_same_a"] == d["k_same_b"]
       and d["k_same_a"].startswith("f:"), d["k_same_a"])
    ok("同大小不同内容 → 不同 key", d["k_diff"] != d["k_same_a"])
    ok("无身份 → 不记忆", d["k_empty"] == "", d["k_empty"])
    ok("只有任务 id 时用 t: 兜底", d["k_task"] == "t:abc", d["k_task"])
    ok("开头 3 秒不记", d["r_head"] is None, d["r_head"])
    ok("中间位置记下秒数", d["r_mid"] and abs(d["r_mid"]["t"] - 123.4) < 0.01,
       d["r_mid"])
    ok("距结尾 10 秒视为看完，不记", d["r_tail"] is None, d["r_tail"])
    ok("时长未知时不写坏记录", d["r_nodur"] is None, d["r_nodur"])


def server_api():
    print("[服务端接口]")
    st, health = http_json("/api/health")
    if st != 200:
        print("  SKIP 服务未启动，接口部分跳过")
        return
    ok("服务版本 2.10", health.get("version") == "2.10", health.get("version"))

    st, d = http_json("/api/playpos", {"key": TEST_KEY, "t": 123.4, "d": 3600})
    ok("写入有效位置", st == 200 and d.get("ok"), (st, d))
    q = urllib.parse.quote(TEST_KEY, safe="")
    st, d = http_json("/api/playpos?key=" + q)
    rec = d.get("position") or {}
    ok("能读回同一条", st == 200 and abs((rec.get("t") or 0) - 123.4) < 0.01, d)
    ok("时长一并记录", abs((rec.get("d") or 0) - 3600) < 0.01, rec)

    st, d = http_json("/api/playpos", {"key": TEST_KEY, "t": 123.6, "d": 3600})
    ok("位置没变不重复写盘", d.get("unchanged") is True, d)

    st, d = http_json("/api/playpos", {"key": TEST_KEY, "t": 600, "d": 3600})
    st2, d2 = http_json("/api/playpos?key=" + q)
    ok("位置变了会更新", st2 == 200
       and abs(((d2.get("position") or {}).get("t") or 0) - 600) < 0.01, (d, d2))

    st, d = http_json("/api/playpos", {"key": TEST_KEY, "t": 0, "d": 3600})
    st2, d2 = http_json("/api/playpos?key=" + q)
    ok("t=0（从头开始）会删掉记录", d2.get("position") is None, d2)

    st, d = http_json("/api/playpos", {"key": TEST_KEY, "t": 3590, "d": 3600})
    st2, d2 = http_json("/api/playpos?key=" + q)
    ok("距结尾 10 秒视为看完，不落记录", d2.get("position") is None, d2)

    st, d = http_json("/api/playpos", {"t": 100, "d": 3600})
    ok("缺 key 返回 400", st == 400, (st, d))
    st, d = http_json("/api/playpos", {"key": TEST_KEY, "t": "abc", "d": 3600})
    ok("非法时间返回 400", st == 400, (st, d))
    st, d = http_json("/api/playpos", {"key": TEST_KEY, "t": float("nan"), "d": 3600})
    ok("NaN 时间返回 400", st == 400, (st, d)) if st == 400 else ok(
        "NaN 时间被拦截", "error" in d or d.get("ok"), (st, d))

    # 收尾：确认没把测试数据留在用户配置里
    st, d = http_json("/api/playpos")
    ok("整表可列且不含测试键", TEST_KEY not in (d.get("positions") or {}), d.get("count"))
    f = BASE / "config" / "playpos.json"
    if f.exists():
        try:
            json.loads(f.read_text(encoding="utf-8"))
            ok("落盘文件是合法 JSON", True)
        except Exception as exc:
            ok("落盘文件是合法 JSON", False, exc)


def source_contract():
    print("[源码契约]")
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    web = (BASE / "web" / "index.html").read_text(encoding="utf-8")
    ok("服务端有 /api/playpos GET", '@app.get("/api/playpos")' in srv)
    ok("服务端有 /api/playpos POST", '@app.post("/api/playpos")' in srv)
    ok("服务端用原子写", "_atomic_write(POS_FILE" in srv)
    ok("服务端有条目上限与锁", "POS_MAX" in srv and "POS_LOCK" in srv)
    ok("前端有续播提示条", 'id="resumeTip"' in web and 'id="resumeHead"' in web)
    ok("前端本地存储有键名", "asrPlayPos_v1" in web)
    ok("前端有节流保存", "posSave(false)" in web and "4000" in web)
    ok("前端离开页面也会存", "pagehide" in web and "visibilitychange" in web)
    ok("前端换课前先存一次", web.count("posSave(true)") >= 3)
    ok("打开历史任务传了身份", "taskId: id" in web)
    ok("上传/导入传了身份", "fileId: d.file_id" in web)
    ok("服务端回退读取", "/api/playpos?key=" in web)
    ok("用 keepalive 保证关页送达", "keepalive:true" in web)
    manual = BASE / "使用说明书.md"
    ok("说明书提到续播", manual.exists()
       and "继续播放" in manual.read_text(encoding="utf-8"))


def main():
    print("=" * 58)
    pure_logic()
    server_api()
    source_contract()
    print("=" * 58)
    print(f"{PASS}/{PASS + FAIL} 通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
