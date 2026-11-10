# -*- coding: utf-8 -*-
"""v2.12 专项：润色模型设置新增硅基流动 / 美团 LongCat。

核对结论（2026-11-10 官方文档 + 真机实测）：
  · 硅基流动 Base URL 正确；模型名 qwen3-8b 错，必须 Qwen/Qwen3-8B；
    正确名 HTTP 200，错误名 HTTP 400 Model does not exist。
  · LongCat 官网 / Base URL 正确；模型名 longcat-2.5 错，必须 LongCat-2.5；
    GET /openai/v1/models 列出 LongCat-2.5 / -Preview / 2.0。
  · 关思考字段三家不统一：DeepSeek / LongCat → thinking:disabled；
    硅基流动 Qwen3 → enable_thinking:false；GLM / Ollama 一律不发。

默认纯离线回归；设 ASR_TEST_CLOUD=1 才用本机 config 里的 Key 做一次真实连通测试。
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import cloud_llm as C  # noqa: E402

PASS = FAIL = 0
CAPTURED = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def txt(rel):
    return (BASE / rel).read_text(encoding="utf-8-sig")


def presets():
    print("[PRESETS 两家新预设]")
    p = C.PRESETS
    ok("硅基流动预设已注册", "siliconflow-qwen3-8b" in p)
    ok("LongCat 预设已注册", "longcat-2.5" in p)
    ok("预设总数 5（含自定义）", len(p) == 5, list(p))
    sf, lc = p["siliconflow-qwen3-8b"], p["longcat-2.5"]
    ok("硅基流动 Base URL", sf["base_url"] == "https://api.siliconflow.cn/v1")
    ok("硅基流动模型名是完整 ID", sf["model"] == "Qwen/Qwen3-8B")
    ok("硅基流动不写错名 qwen3-8b", sf["model"] != "qwen3-8b")
    ok("硅基流动标为免费", sf["price_in"] == 0 and sf["price_out"] == 0)
    ok("硅基流动 note 含官网与订正提示",
       "siliconflow.cn" in sf["note"] and "Qwen/Qwen3-8B" in sf["note"])
    ok("LongCat Base URL", lc["base_url"] == "https://api.longcat.chat/openai/v1")
    ok("LongCat 模型名官方大小写", lc["model"] == "LongCat-2.5")
    ok("LongCat 不写错名 longcat-2.5", lc["model"] != "longcat-2.5")
    ok("LongCat note 含官网与大小写提示",
       "longcat.chat/platform" in lc["note"] and "LongCat-2.5" in lc["note"])
    ok("旧三家预设未被破坏", all(k in p for k in
       ("glm-4-flash", "deepseek-v4-flash", "custom")))
    ids = C.status()["presets"]
    ok("status 下发全部预设", {d["id"] for d in ids} == set(p), ids)


def thinking():
    print("[各家关闭思考字段]")
    cases = [
        ("deepseek-v4-flash", {"thinking": {"type": "disabled"}}),
        ("siliconflow-qwen3-8b", {"enable_thinking": False}),
        ("longcat-2.5", {"thinking": {"type": "disabled"}}),
        ("glm-4-flash", {}),
        ("custom", {}),
    ]
    for key, want in cases:
        cfg = C.apply_preset(key)
        got = C.thinking_off_payload(cfg)
        ok(f"{key} 关思考字段", got == want, got)
        ok(f"{key} 开关可见性", C.supports_thinking(cfg) == bool(want))
    ok("三家新预设默认关思考",
       all(C.apply_preset(k)["thinking"] == "off" for k in
           ("deepseek-v4-flash", "siliconflow-qwen3-8b", "longcat-2.5")))
    ok("GLM 默认不强制关", C.apply_preset("glm-4-flash")["thinking"] == "auto")
    ok("apply_preset 下发 note",
       all("note" in C.apply_preset(k) for k in C.PRESETS))
    ok("apply_preset 下发 thinking_supported",
       all("thinking_supported" in C.apply_preset(k) for k in C.PRESETS))
    ok("Ollama 自定义端点不发额外字段",
       C.thinking_off_payload({"base_url": "http://127.0.0.1:11434/v1",
                               "preset": "custom", "model": "qwen3:4b"}) == {})
    ok("硅基流动自定义非 Qwen3 不乱发",
       C.thinking_off_payload({"base_url": "https://api.siliconflow.cn/v1",
                               "preset": "custom", "model": "THUDM/glm-4-9b-chat"}) == {})
    ok("硅基流动自定义 Qwen3 仍发",
       C.thinking_off_payload({"base_url": "https://api.siliconflow.cn/v1",
                               "preset": "custom", "model": "Qwen/Qwen3-14B"})
       == {"enable_thinking": False})


class _Resp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def open(self, req, timeout=None):
        CAPTURED.append(req)
        body = {"choices": [{"message": {"content": "正常"}}],
                "usage": {"prompt_tokens": 18, "completion_tokens": 1,
                          "total_tokens": 19}}
        return _Resp(json.dumps(body).encode("utf-8"))


def payload_for(preset, thinking=None):
    cfg = C.apply_preset(preset)
    cfg["enabled"] = True
    cfg["api_key"] = "test-key"
    if thinking:
        cfg["thinking"] = thinking
    CAPTURED.clear()
    C.CloudChat(cfg).create_chat_completion(
        messages=[{"role": "user", "content": "只回复两个字：正常"}], max_tokens=32)
    return json.loads(CAPTURED[-1].data.decode("utf-8")), CAPTURED[-1].full_url


def requests_offline():
    print("[真实请求体构造（离线 mock）]")
    original = C._opener
    C._opener = lambda: _Opener()
    try:
        p, url = payload_for("siliconflow-qwen3-8b")
        ok("硅基流动 URL 拼 /chat/completions",
           url == "https://api.siliconflow.cn/v1/chat/completions", url)
        ok("硅基流动发 enable_thinking=false",
           p.get("enable_thinking") is False and "thinking" not in p, p)
        p, _ = payload_for("longcat-2.5")
        ok("LongCat 发 thinking=disabled",
           p.get("thinking") == {"type": "disabled"}
           and "enable_thinking" not in p, p)
        p, _ = payload_for("deepseek-v4-flash")
        ok("DeepSeek 行为未回归", p.get("thinking") == {"type": "disabled"}, p)
        p, _ = payload_for("glm-4-flash")
        ok("GLM 不发任何思考字段",
           "thinking" not in p and "enable_thinking" not in p, p)
        p, _ = payload_for("siliconflow-qwen3-8b", thinking="auto")
        ok("取消勾选后不发关思考字段",
           "enable_thinking" not in p and "thinking" not in p, p)
        p, _ = payload_for("siliconflow-qwen3-8b")
        ok("/no_think 本地开关已剥离",
           "/no_think" not in json.dumps(p, ensure_ascii=False))
    finally:
        C._opener = original


def config_and_docs():
    print("[配置 / 文档 / 版本]")
    cfg_path = BASE / "config" / "cloud_llm.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    keys = cfg.get("keys") or {}
    ok("硅基流动 Key 已按预设保存", bool(keys.get("siliconflow-qwen3-8b")))
    ok("LongCat Key 已按预设保存", bool(keys.get("longcat-2.5")))
    ok("原有两家 Key 未被覆盖",
       bool(keys.get("glm-4-flash")) and bool(keys.get("deepseek-v4-flash")))
    ok("当前启用预设未被擅自改动", cfg.get("preset") == "glm-4-flash",
       cfg.get("preset"))
    gitignore = txt(".gitignore")
    ok("config/ 仍在 .gitignore", "/config/" in gitignore)

    # 明文密钥绝不能出现在任何会被 git 跟踪的源码 / 文档里
    scan = ("cloud_llm.py", "server.py", "web/index.html", "web/说明书.html",
            "README.md", "使用说明书.md", "网课转字幕.txt", ".gitignore",
            "samples/test_v212.py")
    for name, key in keys.items():
        leaks = [f for f in scan if key in txt(f)]
        ok(f"{name} 明文密钥未泄漏到源码/文档", not leaks, leaks)

    html = txt("web/index.html")
    for needle in ("siliconflow.cn", "api.siliconflow.cn/v1", "Qwen/Qwen3-8B",
                   "longcat.chat/platform", "api.longcat.chat/openai/v1",
                   "LongCat-2.5", "enable_thinking=false", "本说明不含任何明文密钥"):
        ok(f"设置界面含 {needle}", needle in html)
    ok("切换预设用后端 note", "d.note || ''" in html)
    ok("思考开关用后端 thinking_supported", "d.thinking_supported ? '' : 'none'" in html)
    manual = txt("使用说明书.md")
    ok("说明书版本 2.12", "版本 2.12" in manual)
    ok("说明书 4.2 新章节", "### 4.2 硅基流动" in manual)
    ok("说明书章节已重编号", "### 4.3 成本账" in manual and "### 4.4 再省一点" in manual)
    ok("网页说明书已同步", "硅基流动" in txt("web/说明书.html"))
    ok("README 2.12 核对表", "## 2.12" in txt("README.md")
       and "invalid_api_key" in txt("README.md"))
    ok("速查已加两家", "Qwen/Qwen3-8B（免费）" in txt("网课转字幕.txt"))
    ok("服务端版本 2.12", '"version": "2.12"' in txt("server.py"))
    hub = Path("D:/Tools/服务台/services.json")
    if hub.exists():
        ok("服务台版本 2.12", '"version": "2.12"' in hub.read_text(encoding="utf-8"))


def cloud_smoke():
    if os.environ.get("ASR_TEST_CLOUD") != "1":
        print("[真实 API 冒烟] 跳过（设 ASR_TEST_CLOUD=1 开启）")
        return
    print("[真实 API 冒烟]")
    cfg = json.loads((BASE / "config/cloud_llm.json").read_text(encoding="utf-8"))
    key = (cfg.get("keys") or {}).get("siliconflow-qwen3-8b")
    p = C.apply_preset("siliconflow-qwen3-8b")
    body = json.dumps({"model": p["model"],
                       "messages": [{"role": "user", "content": "只回复两个字：正常"}],
                       "max_tokens": 32, "stream": False,
                       "enable_thinking": False}).encode("utf-8")
    req = urllib.request.Request(
        p["base_url"] + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + str(key)}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as r:
            ans = json.loads(r.read().decode("utf-8", "replace"))
        ok("硅基流动真实连接 200", r.status == 200)
        ok("回复非空", bool((((ans.get("choices") or [{}])[0]
                            .get("message") or {}).get("content") or "").strip()))
    except Exception as exc:
        ok("硅基流动真实连接 200", False, str(exc)[:180])


if __name__ == "__main__":
    presets()
    thinking()
    requests_offline()
    config_and_docs()
    cloud_smoke()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
