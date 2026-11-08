# -*- coding: utf-8 -*-
"""v2.1–v2.3 专项回归：并行度、云端润色模型、夜间批量队列。

所有配置都重定向到临时目录，不碰真实 config/ 与任务文件。
运行：python samples/test_v21.py
"""
import importlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import transcribe as T  # noqa: E402
import queue_state as QS  # noqa: E402
import cloud_llm as CL  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")


def q_unit(tmp: Path):
    print("[队列] 可排序队列与闸门")
    q = QS.JobQueue()
    for x in ("a", "b", "c"):
        q.put(x)
    ok("初始顺序", q.snapshot() == ["a", "b", "c"], q.snapshot())
    ok("下移首条", q.move("a", 1) and q.snapshot() == ["b", "a", "c"], q.snapshot())
    ok("上移末条", q.move("c", -1) and q.snapshot() == ["b", "c", "a"], q.snapshot())
    ok("越界不动", not q.move("b", -5) and q.snapshot() == ["b", "c", "a"])
    ok("未知 id 不动", not q.move("zz", 1))
    ok("移除", q.remove("b") and q.snapshot() == ["c", "a"], q.snapshot())
    q.push_front("b")
    ok("放回队首", q.snapshot() == ["b", "c", "a"], q.snapshot())
    ok("取第一条", q.get(timeout=0.1) == "b")
    q.task_done()
    try:
        q.get(timeout=0.01)
    except Exception:
        pass

    st = {"paused": False, "start_at": None}
    g = QS.Gate(st)
    ok("默认放行", g.allowed())
    st["paused"] = True
    ok("暂停拦截", not g.allowed())
    st["paused"] = False
    st["start_at"] = time.time() + 3600
    ok("未到点拦截", not g.allowed())
    st["start_at"] = time.time() - 1
    ok("过点放行", g.allowed())

    # 【新增 v2.4】定时到点必须把 start_at 清掉并落盘，否则配置里永远留着
    # 一个昨天的时间戳，前端每次刷新都显示「定时 …」。wait() 会写文件，
    # 这里把 CONFIG 重定向到临时目录，绝不碰真实 config/queue.json。
    old_cfg = QS.CONFIG
    QS.CONFIG = tmp / "queue_gate.json"
    try:
        due = {"paused": False, "start_at": time.time() - 1}
        QS.Gate(due).wait()
        ok("到点清掉定时标记", due.get("start_at") is None, due)
        saved = json.loads(QS.CONFIG.read_text(encoding="utf-8"))
        ok("清标记已落盘", saved["start_at"] is None, saved)
        paused_future = {"paused": True, "start_at": time.time() + 3600}
        ok("手动暂停优先于定时", not QS.Gate(paused_future).allowed())
    finally:
        QS.CONFIG = old_cfg


def cloud_unit(tmp: Path):
    print("[云端] 配置、计费、OpenAI 兼容调用")
    old_cfg = CL.CONFIG
    CL.CONFIG = tmp / "cloud_llm.json"
    try:
        st = CL.status()
        ok("默认未启用", not st["enabled"] and not st["configured"], st)
        ok("预设含免费模型", any(p["id"] == "glm-4-flash" for p in st["presets"]), st["presets"])
        p = CL.apply_preset("glm-4-flash")
        ok("免费模型价格为 0", p["price_in"] == 0 and p["price_out"] == 0, p)
        d = CL.apply_preset("deepseek-v4-flash")
        ok("DeepSeek 用新模型名", d["model"] == "deepseek-v4-flash", d)

        cfg = CL.save_config({"enabled": True, "preset": "glm-4-flash",
                              "base_url": "https://open.bigmodel.cn/api/paas/v4",
                              "model": "glm-4-flash", "api_key": "sk-abcdef1234567890xyz",
                              "price_in": 0, "price_out": 0, "currency": "CNY"})
        ok("配置已落盘", CL.CONFIG.exists())
        st2 = CL.status()
        ok("密钥脱敏", "…" in st2["masked_key"] and "1234567890" not in st2["masked_key"],
           st2["masked_key"])
        ok("空 key 沿用已存密钥", CL.save_config({"model": "glm-4-flash"})["api_key"]
           == "sk-abcdef1234567890xyz")

        # 【新增 v2.4】按预设分别保存密钥：现场反馈「填了 GLM 再填 DeepSeek 存不上」
        CL.save_config({"enabled": True, "preset": "deepseek-v4-flash",
                        "base_url": "https://api.deepseek.com",
                        "model": "deepseek-v4-flash",
                        "api_key": "sk-deepseek-9876543210abc",
                        "price_in": 0.14, "price_out": 0.28, "currency": "USD"})
        ok("切换模型用新 Key", CL.load_config()["api_key"] == "sk-deepseek-9876543210abc")
        back = CL.save_config({"preset": "glm-4-flash",
                               "base_url": "https://open.bigmodel.cn/api/paas/v4",
                               "model": "glm-4-flash",
                               "price_in": 0, "price_out": 0, "currency": "CNY"})
        ok("切回自动带出原 Key", back["api_key"] == "sk-abcdef1234567890xyz", back["api_key"])
        ok("两个 Key 互不覆盖",
           set(CL.status()["keys_saved"]) == {"glm-4-flash", "deepseek-v4-flash"},
           CL.status()["keys_saved"])
        dj = CL.apply_preset("deepseek-v4-flash")
        ok("切换预设报该模型状态", dj["has_key"] and "…" in dj["masked_key"], dj)
        ok("未存 Key 的预设不串用", CL.apply_preset("custom")["api_key"] == "")
        # 测试连接走「当前预设自己的 Key」，不再是上一个模型的
        ok("配置可重复读取", isinstance(CL.load_config(), dict))
        # 恢复成 GLM，避免影响后面的计费/HTTP 用例
        CL.save_config({"preset": "glm-4-flash",
                        "base_url": "https://open.bigmodel.cn/api/paas/v4",
                        "model": "glm-4-flash", "enabled": True,
                        "price_in": 0, "price_out": 0, "currency": "CNY"})
        try:
            CL.save_config({"enabled": True, "base_url": "ftp://x", "model": "m"})
            ok("非法 URL 被拒", False)
        except ValueError:
            ok("非法 URL 被拒", True)

        # 计费：2 万输入 + 1 万输出，DeepSeek $0.14 / $0.28 每百万
        usage = {"prompt_tokens": 20000, "completion_tokens": 10000}
        cny = CL.cost_cny(usage, {"price_in": 0.14, "price_out": 0.28, "currency": "USD"})
        expect = round((20000 * 0.14 + 10000 * 0.28) * 7.1 / 1_000_000, 6)
        ok("美元折算人民币", abs(cny - expect) < 1e-6, (cny, expect))
        ok("免费模型零费用", CL.cost_cny(usage, p) == 0.0)

        # 用假 HTTP 响应验证请求格式与 usage 累计
        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        seen = {}

        class FakeOpener:
            def open(self, req, timeout=None):
                seen["url"] = req.full_url
                seen["body"] = json.loads(req.data.decode("utf-8"))
                seen["auth"] = req.headers.get("Authorization")
                return FakeResp(json.dumps({
                    "choices": [{"message": {"content": "整理后的正文"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40,
                              "total_tokens": 140},
                }).encode("utf-8"))

        CL._opener = lambda: FakeOpener()
        chat = CL.CloudChat(CL.load_config())
        r = chat.create_chat_completion(
            messages=[{"role": "user", "content": "原文\n/no_think"}],
            temperature=0.15, max_tokens=800)
        ok("URL 拼 /chat/completions", seen["url"].endswith("/chat/completions"), seen["url"])
        ok("鉴权头正确", str(seen["auth"]).startswith("Bearer sk-"), seen["auth"])
        ok("去掉 /no_think", "/no_think" not in seen["body"]["messages"][0]["content"])
        ok("max_tokens 透传", seen["body"]["max_tokens"] == 800, seen["body"])
        ok("返回 OpenAI 结构", r["choices"][0]["message"]["content"] == "整理后的正文")
        chat.create_chat_completion(messages=[{"role": "user", "content": "x"}])
        ok("usage 累计", chat.usage["prompt_tokens"] == 200, chat.usage)
        ok("估算含次数", chat.estimate()["calls"] == 2, chat.estimate())

        okc, msg, _ = CL.test_connection({"model": "glm-4-flash"})
        ok("测试连接可用假通道", okc, msg)
    finally:
        CL.CONFIG = old_cfg
        CL._opener = getattr(CL, "_opener_orig", CL._opener)

    # llm_polish 的后端选择也必须跟着配置走
    import llm_polish as LP
    orig = LP.available()
    ok("默认走本地", "云端" not in orig[1] or not orig[0], orig)


def batch_unit(tmp: Path):
    print("[批量] 入队、跳过重复、队列设置、重启续跑")
    import server as S

    # 【修复 v2.4】api_queue_set 会 QS.save() 落盘；原来没重定向 CONFIG，
    # 跑一次测试就把真实队列写成「暂停 + 定时 + 重试3次」，
    # 用户点「重试」后任务一直排队不跑。测试必须完全隔离。
    old_cfg = QS.CONFIG
    QS.CONFIG = tmp / "queue.json"
    try:
        _batch_unit_body(tmp, S)
    finally:
        QS.CONFIG = old_cfg


def _batch_unit_body(tmp: Path, S):

    jobs = tmp / "jobs"; jobs.mkdir()
    media = tmp / "media"; media.mkdir()
    inbox = media / "inbox"; inbox.mkdir()
    S.JOBS, S.MEDIA, S.OUT, S.INBOX = jobs, media, tmp / "out", inbox
    (tmp / "out").mkdir()
    S.JOB_Q = QS.JobQueue()
    S.QSTATE.clear(); S.QSTATE.update(QS.DEFAULTS)
    S.QSTATE["auto_retry"] = 0
    S.TASKS.clear(); S.UPLOADS.clear()

    (media / "a.mp4").write_bytes(b"aaaa")
    (media / "b.mp4").write_bytes(b"bbbb")
    S.UPLOADS["f1"] = {"name": "a.mp4", "path": media / "a.mp4", "kind": "video"}
    S.UPLOADS["f2"] = {"name": "b.mp4", "path": media / "b.mp4", "kind": "video"}

    r = S.tasks_enqueue({"file_ids": ["f1", "f2"]})
    ok("批量建 2 个任务", len(r["created"]) == 2, r)
    ok("队列顺序 1、2", [x["position"] for x in r["created"]] == [1, 2], r["created"])
    ok("队列两条可见", len(S.JOB_Q.snapshot()) == 2, S.JOB_Q.snapshot())

    r2 = S.tasks_enqueue({"file_ids": ["f1", "f2"]})
    ok("重复入队被跳过", len(r2["skipped"]) == 2 and not r2["created"], r2)
    r3 = S.tasks_enqueue({"file_ids": ["f1"], "skip_done": False})
    ok("关掉跳过可强制重跑", len(r3["created"]) == 1, r3)

    st = S.api_queue_set({"paused": True})
    ok("暂停已生效", st["paused"])
    st = S.api_queue_set({"auto_retry": 3})
    ok("自动重试可配", st["auto_retry"] == 3)
    st = S.api_queue_set({"start_at": time.time() + 600})
    ok("定时开始可设", st["start_at"] is not None)
    # 【新增 v2.4】暂停态下设定时，到点也必须能开跑
    ok("设定时自动解除暂停", not st["paused"], st)

    ids = S.JOB_Q.snapshot()
    ok("顺序调整", S.api_queue_move({"id": ids[0], "delta": 1})["ok"])
    after = S.JOB_Q.snapshot()
    ok("调整后首两条互换", after[:2] == [ids[1], ids[0]] and len(after) == len(ids), after)

    # 文件夹监控：新文件入队，已入队的不再重复
    (inbox / "c.mp4").write_bytes(b"cccc")
    got = S.scan_inbox()
    ok("扫描到 1 个新文件", len(got) == 1, got)
    ok("再扫不重复", len(S.scan_inbox()) == 0)

    # 重启续跑：queued 保留并重新入队，running 标中断
    (jobs / "q1.json").write_text(json.dumps(
        {"id": "q1", "status": "queued", "name": "q.mp4",
         "path": str(media / "a.mp4"), "created": time.time()}), encoding="utf-8")
    (jobs / "r1.json").write_text(json.dumps(
        {"id": "r1", "status": "running", "name": "r.mp4",
         "path": str(media / "b.mp4"), "created": time.time()}), encoding="utf-8")
    S.JOB_Q = QS.JobQueue()
    S.TASKS.clear()
    S._load_jobs()
    ok("queued 重启后仍排队", S.TASKS["q1"]["status"] == "queued", S.TASKS.get("q1"))
    ok("queued 重新入队", "q1" in S.JOB_Q.snapshot(), S.JOB_Q.snapshot())
    ok("running 标中断", S.TASKS["r1"]["status"] == "interrupted", S.TASKS.get("r1"))

    # 【新增 v2.4】源文件禁止删除：旧客户端就算传 purge_media=True 也不生效
    keep = media / "a.mp4"
    ok("删除前源文件在", keep.exists())
    S.TASKS["r1"]["status"] = "done"
    S.tasks_batch({"action": "delete", "ids": ["r1"], "purge_media": True})
    ok("旧参数也删不掉源文件", keep.exists())


def guards():
    print("[护栏] 文件与接线")
    src = (BASE / "server.py").read_text(encoding="utf-8")
    web = (BASE / "web/index.html").read_text(encoding="utf-8")
    for key in ('@app.post("/api/tasks/enqueue")', '@app.get("/api/queue")',
                '@app.post("/api/queue/move")', '@app.post("/api/queue/scan")',
                '@app.get("/api/cloud/status")', '@app.post("/api/cloud/config")',
                '_keep_awake_loop', 'QGATE.allowed()'):
        ok("后端 " + key[:34], key in src)
    for key in ("btnBatchAdd", "btnQueuePause", "queueAt", "queueWatch",
                "queueRetry", "cloudMask", "btnRevLlmCfg", "batchAdd", "initQueuePanel"):
        ok("前端 " + key, key in web)
    ok("批量 CLI 存在", (BASE / "batch_add.py").exists())
    ok("启动器存在", (BASE / "启动批量任务.bat").exists())
    ok("中文说明存在", (BASE / "批量任务说明.txt").exists())
    # 【新增 v2.4】首页说明书 + 源文件禁止删除
    ok("首页有使用说明按钮", 'id="btnManual"' in web)
    ok("说明书静态页存在", (BASE / "web" / "说明书.html").exists())
    ok("说明书生成脚本存在", (BASE / "samples" / "gen_manual.py").exists())
    ok("任务栏不再有连源文件一起删", 'id="purgeMedia"' not in web)
    ok("后端忽略 purge_media", "purge = False" in src)
    ok("Paraformer 有心跳进度", "funasr-beat" in (BASE / "transcribe.py").read_text(encoding="utf-8"))
    gitignore = (BASE / ".gitignore").read_text(encoding="utf-8")
    ok("配置目录已排除入库", "/config/" in gitignore)
    bat = (BASE / "启动批量任务.bat").read_bytes()
    ok("bat 纯 ASCII / 无 BOM / CRLF",
       all(c < 128 for c in bat) and not bat.startswith(b"\xef\xbb\xbf")
       and bat.count(b"\r\n") == bat.count(b"\n"))
    txt = (BASE / "批量任务说明.txt").read_bytes()
    ok("说明 UTF-8 BOM / CRLF",
       txt.startswith(b"\xef\xbb\xbf") and txt.count(b"\r\n") == txt.count(b"\n"))
    ok("并行度默认 2", T.QWEN3_WORKERS == 2, T.QWEN3_WORKERS)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="v21_"))
    q_unit(tmp)
    cloud_unit(tmp)
    batch_unit(tmp)
    guards()
    bad = [n for n, c in PASS if not c]
    print(f"\n{len(PASS) - len(bad)}/{len(PASS)} 通过")
    if bad:
        print("失败：" + "、".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
