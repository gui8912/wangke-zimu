# -*- coding: utf-8 -*-
"""v2.12-p9 专项：编辑态落盘（刷新 / 关页不丢校对稿）回归。

背景（参考 AudioScript 的 SPEC §1.2「真相源唯一」）：
  字幕只活在页面内存里，靠 1.2 秒防抖写 <stem>.edited.srt；刷新后
  /api/draft 又按 mtime 判新旧。同一节课重识别、两个课件同名、或别的
  工具碰过时间戳，旧稿就会被静默跳过，看起来就是「校对稿丢了」。

本脚本分三段：
  ① 存储层 edit_state.py —— 键安全化、写入/读取、修订号、冲突、超限、淘汰；
  ② 代码接线静态检查 —— server.py 的 /api/edit 与前端保存/恢复顺序；
  ③ 真实接口冒烟 —— 服务已重启到 p9 时跑一轮写-读-冲突-清理。

②③ 之外的用例纯离线，不碰服务与队列，可在识别任务运行中安全执行。
"""
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import edit_state as ES      # noqa: E402

PASS = FAIL = SKIP = 0
REAL_DIR = ES.DIR


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def skip(name, why=""):
    global SKIP
    SKIP += 1
    print(f"  SKIP {name}  {why}")


def cues(n=2):
    return [{"start": i * 1.0, "end": i * 1.0 + 0.9, "text": f"第{i + 1}句"}
            for i in range(n)]


def unit_tests():
    tmp = Path(tempfile.mkdtemp(prefix="asr_edit_state_"))
    ES.DIR = tmp
    try:
        print("[键安全化：task_id 优先，stem 兜底，路径不可逃逸]")
        ok("有 task_id 时用 t_ 前缀", ES.key_for("abc123", "任意名字") == "t_abc123")
        ok("没有 task_id 时退回 stem", ES.key_for("", "第1讲 器械") == "s_第1讲_器械")
        ok("两者都空返回空串", ES.key_for("", "") == "")
        ok("斜杠 / 反斜杠被替换", "/" not in ES.key_for("", "../../evil\\x")
           and "\\" not in ES.key_for("", "../../evil\\x"))
        ok("开头的点被去掉，不能是 ..", not ES.key_for("", "../../evil").startswith("s_."))
        ok("超长名字截断到 120 字以内",
           len(ES.key_for("", "长" * 500)) <= 122)
        ok("_path 落在 DIR 内",
           ES._path("s_evil").parent == tmp)

        print("[写入 / 读取：修订号递增，内容原样回来]")
        k = "t_roundtrip"
        r1 = ES.save(k, cues=cues(3), task_id="roundtrip", stem="测试课", mode="clean")
        ok("首次写入 revision=1", r1["revision"] == 1, r1)
        ok("chars 按文本长度统计", r1["chars"] == len("第1句第2句第3句"))
        d = ES.load(k)
        ok("读回来的 cues 条数一致", len(d["cues"]) == 3)
        ok("读回来的文本一致", d["cues"][1]["text"] == "第2句")
        ok("带 saved_at 与可读时间", d["saved_at"] > 0 and d["saved_at_text"])
        r2 = ES.save(k, cues=cues(4), task_id="roundtrip", stem="测试课")
        ok("第二次写入 revision=2", r2["revision"] == 2)
        ok("内容已更新为 4 条", len(ES.load(k)["cues"]) == 4)
        ok("目录里没有 .tmp 残留", not list(tmp.glob("*.tmp")))

        print("[冲突检测：base_revision 落后就拒写]")
        try:
            ES.save(k, cues=cues(2), base_revision=1)
            ok("落后修订应抛 Conflict", False)
        except ES.Conflict as exc:
            ok("落后修订抛 Conflict", True)
            ok("Conflict 带上对方当前版本", exc.current.get("revision") == 2)
            ok("冲突时没有写入（仍是 4 条）", len(ES.load(k)["cues"]) == 4)
        ok("base_revision 等于当前版本可写", ES.save(k, cues=cues(2), base_revision=2)["revision"] == 3)
        ok("base_revision=None 不校验", ES.save(k, cues=cues(2))["revision"] == 4)
        ok("base_revision=0 视为不校验", ES.save(k, cues=cues(2), base_revision=0)["revision"] == 5)

        print("[输入校验：坏数据不进盘]")
        for label, bad in [
            ("空数组", []),
            ("不是数组", {"start": 0, "end": 1}),
            ("缺时间码", [{"text": "x"}]),
            ("时间为字符串", [{"start": "0", "end": "1", "text": "x"}]),
            ("text 不是字符串", [{"start": 0, "end": 1, "text": 123}]),
        ]:
            try:
                ES.save("t_bad", cues=bad)
                ok(f"{label} 应被拒绝", False)
            except ES.EditError:
                ok(f"{label} 被拒绝", True)
        ok("坏数据没有覆盖已有文件", ES.load("t_bad") is None)

        print("[超限保护]")
        try:
            ES.save("t_big", cues=[{"start": 0, "end": 1, "text": "字" * (ES.MAX_BYTES)}])
            ok("超限应被拒绝", False)
        except ES.TooLarge:
            ok("超过 8MB 抛 TooLarge", True)
        ok("超限没有留下文件", ES.load("t_big") is None)

        print("[损坏文件：当成不存在，绝不抛异常]")
        p = tmp / "t_broken.json"
        p.write_text("{不是 JSON", encoding="utf-8")
        ok("坏 JSON 返回 None", ES.load("t_broken") is None)
        p.write_text(json.dumps({"schema": 99, "cues": cues()}), encoding="utf-8")
        ok("schema 不符返回 None", ES.load("t_broken") is None)
        p.write_text(json.dumps({"schema": ES.SCHEMA, "cues": []}), encoding="utf-8")
        ok("空 cues 返回 None", ES.load("t_broken") is None)

        print("[淘汰：数量上限，keep 永不删]")
        for i in range(ES.MAX_FILES + 12):
            ES.save(f"t_bulk{i:04d}", cues=cues(1),
                    now=time.time() - i)          # 越往后越旧
        keep_key = "t_bulk0000"
        ES.save(keep_key, cues=cues(2), now=time.time() + 5)
        files = list(tmp.glob("t_bulk*.json"))
        ok(f"文件数不超过上限（{len(files)} ≤ {ES.MAX_FILES}）", len(files) <= ES.MAX_FILES)
        ok("刚写入的 keep 仍在", (tmp / f"{keep_key}.json").exists())

        ES.save("t_remove_me", cues=cues(1))
        ok("remove 能删掉一份", ES.remove("t_remove_me") and ES.load("t_remove_me") is None)
        ok("remove 不存在的键返回 False", ES.remove("t_nothing") is False)

        print("[stats]")
        s = ES.stats()
        ok("统计带目录与份数", s["dir"] == str(tmp) and s["count"] >= 1)
        ok("统计体积大于 0", s["bytes"] > 0)
    finally:
        ES.DIR = REAL_DIR
        shutil.rmtree(tmp, ignore_errors=True)


def static_tests():
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    web = (BASE / "web/index.html").read_text(encoding="utf-8")

    print("[server.py 接线]")
    ok("导入 edit_state", "import edit_state as ES" in srv)
    ok("有 GET /api/edit", '@app.get("/api/edit")' in srv)
    ok("有 POST /api/edit", '@app.post("/api/edit")' in srv)
    ok("按 task_id + stem 求键", "ES.key_for(tid, stem)" in srv)
    ok("写入前复用 _normalize_cues 校验",
       '_normalize_cues(payload.get("cues"))' in srv)
    ok("冲突返回 409", "except ES.Conflict" in srv and "status_code=409" in srv)
    ok("冲突把对方版本一起回给前端", '"conflict": True' in srv
       and "_edit_public(exc.current, key)" in srv)
    ok("超限返回 413", "except ES.TooLarge" in srv and "status_code=413" in srv)
    ok("/api/health 暴露 edit_state 能力", '"edit_state": True' in srv)
    ok("没有明文密钥写进编辑态", "api_key" not in srv.split('@app.post("/api/edit")', 1)[1]
       .split("@app.", 1)[0])

    print("[web/index.html 保存]")
    ok("state 带编辑态镜像字段", "editRev:0, editSavedAt:0" in web)
    ok("有 saveEditState()", "async function saveEditState()" in web)
    ok("保存带 base_revision", "base_revision: state.editRev || null" in web)
    ok("先写编辑态再写导出稿",
       web.find("ed = await saveEditState()") < web.find("fetch('/api/save'"))
    ok("服务端旧版 404 时自动降级", "if(r.status === 404)" in web
       and "state.editOff = true" in web)
    ok("成功保存显示修订号", "编辑态修订" in web)
    ok("导出稿失败不冒充完全成功", "已存编辑态 · 导出失败" in web)

    print("[web/index.html 恢复]")
    ok("有 restoreEditState()", "async function restoreEditState()" in web)
    ok("恢复请求走 /api/edit", "fetch('/api/edit?task_id='" in web)
    ok("逐条比较而非串比较", "function cuesSame" in web)
    ok("恢复时同步 mode", "state.mode = src.mode" in web)
    ok("恢复后不标脏、不触发回写", re.search(
        r"state\.dirty = false;[\s\S]{0,120}?snapshot\('恢复上次编辑', \{autosave:false\}\)", web)
        is not None)
    ok("renderResult 先读编辑态，再退回旧草稿",
       web.find("const got = await restoreEditState()") > 0
       and web.find("if(!got) restoreDraft()") > web.find("const got = await restoreEditState()"))
    ok("新结果重置修订号", "state.editRev = 0; state.editSavedAt = 0; state.editConflict = null;" in web)

    print("[web/index.html 关页兜底与冲突]")
    ok("pagehide 触发 flush", "window.addEventListener('pagehide', flushEditState)" in web)
    ok("切到后台也 flush", "document.visibilityState === 'hidden'" in web
       and "flushEditState()" in web)
    ok("小稿走 keepalive，大稿改普通请求（浏览器配额）",
       "keepalive: body.length <= KEEPALIVE_MAX" in web
       and "const KEEPALIVE_MAX = 24 * 1024" in web)
    ok("flush 后修订号置未知，避免自撞", re.search(
        r"state\.editRev = 0;\s*\n\s*\}catch", web) is not None)
    ok("关页前先写本地镜像", "mirrorEditState();" in web
       and web.find("mirrorEditState();") < web.find("fetch('/api/edit'", web.find("function flushEditState")))
    ok("本地镜像带 saved_at / cues / mode",
       "saved_at: Date.now() / 1000" in web and "cues: state.cues" in web)
    ok("恢复时谁的 saved_at 新用谁",
       "mirror.saved_at || 0) > (d.saved_at || 0)" in web)
    ok("镜像被采用后补推服务端", "if(useMirror) pushMirror()" in web)
    ok("配额满时先清其它任务备份", "k.startsWith(EDIT_MIRROR) && k !== mk" in web)
    ok("保存成功后清掉本地镜像", "clearEditMirror();               // 服务端已经有这份了" in web)
    ok("冲突有明确出口", "function resolveEditConflict" in web)
    ok("自动保存遇冲突不弹窗、只亮黄标",
       "if(manual) resolveEditConflict();" in web and "setSaveState('另一处已更新','warn')" in web)
    ok("冲突未决时不再自动重试",
       re.search(r"if\(state\.editConflict\)\{ setSaveState\('另一处已更新','warn'\); return; \}", web)
       is not None)


def _http(method, path, payload=None, timeout=10):
    url = "http://127.0.0.1:7870" + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # 本机直连必须绕开环境代理，否则偶发 502
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body[:200]}
    except Exception as exc:
        return "ERR", {"error": f"{type(exc).__name__}: {exc}"[:160]}


def live_tests():
    print("[真实接口：写 → 读 → 冲突 → 清理]")
    st, health = _http("GET", "/api/health")
    if st != 200 or not health.get("edit_state"):
        skip("服务端 /api/edit 冒烟",
             "服务未启动或仍是旧版（重启后本项才会真正执行）")
        return

    tid = "p9selftest" + str(int(time.time()))
    stem = "p9_selftest_" + str(int(time.time()))
    body = {"task_id": tid, "stem": stem, "mode": "clean", "cues": cues(3)}
    st, d = _http("POST", "/api/edit", body)
    ok("POST /api/edit 返回 200", st == 200 and d.get("ok"), (st, d))
    ok("首次 revision=1", d.get("revision") == 1, d.get("revision"))

    st, d = _http("GET", "/api/edit?task_id=" + tid + "&stem=" + stem)
    ok("GET 返回 exists=True", st == 200 and d.get("exists") is True, (st, d))
    ok("读回 3 条字幕且文本一致",
       len(d.get("cues") or []) == 3 and d["cues"][2]["text"] == "第3句")
    ok("读回绑定 task_id", d.get("task_id") == tid)

    st, d = _http("POST", "/api/edit", {**body, "base_revision": 1})
    ok("带正确 base_revision 可写", st == 200 and d.get("revision") == 2, (st, d))
    st, d = _http("POST", "/api/edit", {**body, "base_revision": 1})
    ok("落后的 base_revision 返回 409", st == 409 and d.get("conflict"), (st, d))
    ok("409 带回对方修订号", d.get("revision") == 2, d.get("revision"))

    st, d = _http("POST", "/api/edit", {"task_id": tid, "stem": stem, "cues": []})
    ok("空 cues 返回 400", st == 400, (st, d))
    st, d = _http("GET", "/api/edit?task_id=&stem=")
    ok("缺少身份返回 400", st == 400, (st, d))

    ok("清理测试记录", ES.remove("t_" + tid))
    st, d = _http("GET", "/api/edit?task_id=" + tid + "&stem=" + stem)
    ok("清理后 exists=False", st == 200 and d.get("exists") is False, (st, d))


def main():
    print("[edit_state：键与存储]")
    unit_tests()
    print("\n[server.py / web/index.html 接线]")
    static_tests()
    print("\n[真实服务]")
    live_tests()
    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
