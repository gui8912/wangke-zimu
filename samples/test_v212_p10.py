# -*- coding: utf-8 -*-
"""v2.12-p10 专项：逐片实时预览 + 领域用语一致性回归。

背景
────
长课要跑几十分钟，原来完成前什么都看不到。现在每识别完一片就推一片：
服务端放内存缓冲（`live_state.py`），前端按 `since=revision` 拉增量，
按片号合并、按时间排序，边转边看。

**用户硬要求：不能为了实时预览丢掉行业用语优化。**
所以预览文本走的是与成稿同一份词表、同一组开关（错词校正 / 简称展开 /
数字规整 / 口语删除，含「我们 / 咱们」无条件删），只是用独立的 Cleaner
实例，避免预览影响最终清理的跨句计数——成稿必须与加这个功能前逐字节一致。

本脚本纯离线（最后一段查真实接口），不启动识别、不加载模型权重。
"""
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import live_state as LS       # noqa: E402
import transcribe as T        # noqa: E402

PASS = FAIL = SKIP = 0


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


def buffer_tests():
    print("[live_state：乱序片、增量、并发]")
    b = LS.LiveBuffer()
    ok("第 3 片先到，revision=1", b.put(2, 4, [{"start": 20, "end": 21, "text": "三"}]) == 1)
    b.put(0, 4, [{"start": 0, "end": 1, "text": "一"}])
    b.put(1, 4, [{"start": 10, "end": 11, "text": "二"}])
    snap = b.snapshot(0)
    ok("按片号排序返回", [s["i"] for s in snap["slices"]] == [0, 1, 2])
    ok("每片带独立 revision", [s["rev"] for s in snap["slices"]] == [2, 3, 1])
    ok("每片标 final", all(s["state"] == "final" for s in snap["slices"]))
    ok("done=3 / complete=False", b.done() == 3 and b.complete() is False)
    ok("since=2 只回第 2 片（rev=3）",
       [(s["i"], s["rev"]) for s in b.snapshot(2)["slices"]] == [(1, 3)])
    ok("since 大于当前版本时为空", b.snapshot(99)["slices"] == [])
    ok("已出字数统计", b.chars() == 3)
    b.put(0, 4, [{"start": 0, "end": 1, "text": "一改"}])
    ok("同片覆盖不新增条目", b.done() == 3 and b.snapshot(0)["slices"][0]["cues"][0]["text"] == "一改")

    b2 = LS.LiveBuffer()
    b2.put(0, 2, [{"start": 0, "end": 1, "text": "旧"}])
    b2.put(1, 2, [{"start": 1, "end": 2, "text": "旧2"}])
    ok("片数变化视为新识别，清空旧片",
       b2.put(0, 3, [{"start": 0, "end": 1, "text": "新"}]) == 3
       and b2.snapshot(0)["total"] == 3 and b2.done() == 1)
    ok("片齐了 complete=True", b2.put(1, 3, []) and b2.put(2, 3, []) and b2.complete())

    b3 = LS.LiveBuffer()
    def writer(k):
        for j in range(50):
            b3.put(k, 4, [{"start": j, "end": j + 1, "text": f"{k}-{j}"}])
    ts = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok("4 线程各写 50 次：revision=200 且不丢片",
       b3.revision == 200 and b3.done() == 4, f"{b3.revision}/{b3.done()}")


def domain_tests():
    print("[领域用语：预览与成稿同源]")
    cleaner = T._make_cleaner()
    sents = [
        {"text": "我们影片的贮藏条件要符合要求", "start": 0, "end": 2400},
        {"text": "管代要负责，二零二六年起执行", "start": 2400, "end": 6000},
        {"text": "咱们下节课讲飞检", "start": 6000, "end": 8000},
        {"text": "嗯", "start": 8000, "end": 8200},
    ]
    got = []
    T._emit_live(lambda i, t, c: got.append((i, t, c)), 0, 3, sents, cleaner)
    ok("回调拿到片号 / 总片数", got and got[0][0] == 0 and got[0][1] == 3)
    cues = got[0][2] if got else []
    texts = [c["text"] for c in cues]
    ok("「我们」被无条件删除（硬规则）", not any("我们" in t for t in texts), texts)
    ok("「咱们」被无条件删除（硬规则）", not any("咱们" in t for t in texts), texts)
    ok("影片→饮片（行业语境优先）", any("饮片" in t for t in texts), texts)
    ok("贮藏不被误改", any("贮藏" in t for t in texts), texts)
    ok("管代→管理者代表（简称展开）", any("管理者代表" in t for t in texts), texts)
    ok("二零二六→2026（数字规整）", any("2026" in t for t in texts), texts)
    ok("纯语气词整条不展示", len(texts) == 3 and all(t.strip() for t in texts), texts)
    ok("时间转成秒（3 位小数）",
       cues and cues[0]["start"] == 0.0 and 2.3 <= cues[0]["end"] <= 2.5, cues[:1])

    raw = []
    T._emit_live(lambda i, t, c: raw.append(c), 0, 1,
                 [{"text": "我们影片的贮藏", "start": 0, "end": 1000}], None)
    ok("不传清理器时保留识别原文（便于排查）",
       raw and raw[0][0]["text"] == "我们影片的贮藏", raw)

    try:
        T._emit_live(lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
                     0, 1, [{"text": "测试", "start": 0, "end": 1}], None)
        ok("回调抛异常不影响识别", True)
    except Exception:
        ok("回调抛异常不影响识别", False)

    ok("长句按成稿规则断行", all(len(c["text"]) <= T.MAX_CUE_CHARS
                                 for c in T._live_cues(
                                     [{"text": "这是一句很长的话" * 8, "start": 0, "end": 20000}])))


def static_tests():
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    tr = (BASE / "transcribe.py").read_text(encoding="utf-8")
    web = (BASE / "web/index.html").read_text(encoding="utf-8")

    print("[transcribe.py：逐片上报，成稿路径不动]")
    ok("run_pipeline 增加 on_slice", "on_slice=None) -> dict:" in tr)
    ok("Paraformer 每片上报", "_publish_slice(on_slice, i, total, sents)" in tr)
    ok("Qwen3 每片用字级时间戳分句", "align_sentences(text, ts_ms)" in tr)
    ok("整段推理完成时也推一次", 'if sents:\n            _publish_slice(on_slice, 0, 1, sents)' in tr)
    ok("Paraformer 无 sentence_info 时用 timestamp 分句（分片）",
       'if not sents and d.get("timestamp"):' in tr)
    ok("Paraformer 无 sentence_info 时用 timestamp 分句（整段）",
       'if not sents and item.get("timestamp"):' in tr)
    ok("预览用独立 Cleaner", "live_cleaner = _make_cleaner(" in tr)
    ok("最终清理仍用自己的实例（成稿不受影响）",
       "cleaner = ct.Cleaner(ct.load_rules(" in tr)
    ok("热词表加载未被改动", "hotword = load_hotwords(" in tr)
    ok("预览清理遵守 itn / termfix 开关",
       "live_cleaner = _make_cleaner(itn, termfix, termfix_auto)" in tr)
    ok("成稿与预览共用断行上限", "for st, et, body in cues_with_time(sentences):" in tr)

    print("[server.py：只放内存 + 增量接口]")
    ok("导入 live_state", "import live_state as LS" in srv)
    ok("_persist 排除 live（不把预览写进 job.json）",
       'if k not in ("result", "live")' in srv)
    ok("重启丢弃实时缓冲", 'd["live"] = None' in srv)
    ok("新任务/重试清空旧片", 'task["live"] = None' in srv)
    ok("有 GET /api/task/{tid}/live", '@app.get("/api/task/{tid}/live")' in srv)
    ok("接口支持 since 增量", "live.snapshot(since)" in srv)
    ok("健康接口暴露 live 能力", '"live": True' in srv)

    print("[web/index.html：只读预览与状态切换]")
    ok("state 带 live / liveMode", "live:{rev:0,total:0,done:0,slices:{}}, liveMode:false" in web)
    ok("有 beginLive / resetLive / renderLive / fetchLive",
       all(f"function {n}(" in web for n in ("beginLive", "resetLive", "renderLive", "fetchLive")))
    ok("增量拉取带 since", "live?since=${state.live.rev}" in web)
    ok("按片合并", "state.live.slices[s.i] = s.cues" in web)
    ok("按时间排序渲染", "out.sort((a, b)=>a.start - b.start || a.end - b.end)" in web)
    ok("识别中禁止自动保存", "if(state.demo || state.liveMode || !state.stem" in web)
    ok("识别中不建输入框 / 不可编辑", "if(state.liveMode){" in web
       and "x.ondblclick = e=>{" in web and "if(!state.liveMode){" in web)
    ok("成稿到达退出预览", "resetLive();" in web.split("function renderResult", 1)[1][:400])
    ok("开始转写先落旧稿再清空", web.find("beginLive();") < web.find("state.taskId = d.task_id;"))
    ok("失败时保留已出片段", "句仅供核对" in web)
    ok("预览条标明术语已校正", "已按术语表 / 口语表校正" in web)
    ok("关掉口语清理时不冒充已校正", "未启用口语清理" in web)
    ok("刷新时接回正在识别的任务", "已接回正在识别的任务" in web)
    ok("开始识别时清掉上次任务记录",
       "localStorage.removeItem('asrLastTask')" in web.split("function beginLive", 1)[1][:900])


def _get(path, timeout=8):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open("http://127.0.0.1:7870" + path, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except ValueError:
            return e.code, {}
    except Exception as exc:
        return "ERR", {"error": f"{type(exc).__name__}: {exc}"[:120]}


def api_tests():
    print("[真实服务：接口形状]")
    st, h = _get("/api/health")
    if st != 200 or not h.get("live"):
        skip("GET /api/task/{tid}/live 冒烟", "服务未启动或仍是旧版（重启后生效）")
        return
    st, ts = _get("/api/tasks")
    rows = (ts or {}).get("tasks") or []
    if not rows:
        skip("实时接口冒烟", "任务列表为空")
        return
    tid = rows[0]["id"]
    st, d = _get(f"/api/task/{tid}/live?since=0")
    ok("已结束任务也能查实时接口", st == 200 and d.get("status") in
       ("done", "error", "cancelled", "interrupted"), (st, d))
    ok("返回结构含 revision/total/done/slices",
       all(k in d for k in ("revision", "total", "done", "slices")))
    rows = d.get("slices") or []
    ok("slices 是数组（刚跑完的任务有 live 也合法）", isinstance(rows, list))
    ok("每片字段齐全", all({"i", "rev", "state", "cues"} <= set(s) for s in rows),
       rows[:1])
    ok("done 与返回片数一致（since=0 返全量）", d.get("done") == len(rows))
    st, d = _get("/api/task/no-such-task/live")
    ok("不存在的任务返回 404", st == 404, st)


def main():
    buffer_tests()
    print()
    domain_tests()
    print()
    static_tests()
    print()
    api_tests()
    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
