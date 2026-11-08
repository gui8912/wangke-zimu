# -*- coding: utf-8 -*-
"""云端润色模型（OpenAI 兼容 /chat/completions）。

只用于**讲稿轨的文字整理**，音频始终不出本机；字幕轨、字级时间戳一个字不碰。
支持国内两家低成本/免费服务，也支持任意 OpenAI 兼容端点（含本机 Ollama）。

设计要点：
  · 只用标准库 urllib，不装 openai SDK，避免给本地环境再加依赖；
  · 对 llm_polish 伪装成 llama.cpp 的 create_chat_completion，
    于是分块、截断自愈、五项硬红线、diff 预览整套逻辑一行都不用改；
  · 绕开环境变量里的失效代理（本机实测环境代理返 502），
    确需代理时用 ASR_LLM_PROXY 显式指定；
  · 累计 token 用量并折算人民币，界面上直接显示「这一稿花了多少钱」。

配置落 config/cloud_llm.json（已加入 .gitignore，API Key 不入库）。
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config" / "cloud_llm.json"

USD_CNY = float(os.environ.get("ASR_USD_CNY") or 7.1)

# 价格单位：每 100 万 token。调研于 2026-11-08，实际以服务商公告为准。
PRESETS = {
    "glm-4-flash": {
        "label": "智谱 GLM-4-Flash（永久免费）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "price_in": 0.0, "price_out": 0.0, "currency": "CNY",
        "note": "官方长期免费、不限 token 总量，限 30 并发 / 约 3 次每秒；"
                "1 小时课件的讲稿整理约 2 万 token，完全够用。国内直连。",
    },
    "deepseek-v4-flash": {
        "label": "DeepSeek V4-Flash（低价）",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "price_in": 0.14, "price_out": 0.28, "currency": "USD",
        "note": "缓存命中输入 $0.0028/M、未命中 $0.14/M、输出 $0.28/M。"
                "北京时段 18:00–次日 09:00 属低谷，约为半价。",
    },
    "custom": {
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "model": "",
        "price_in": 0.0, "price_out": 0.0, "currency": "CNY",
        "note": "可填本机 Ollama（http://127.0.0.1:11434/v1）等任意兼容端点。",
    },
}

DEFAULTS = {
    "enabled": False,
    "preset": "glm-4-flash",
    "base_url": PRESETS["glm-4-flash"]["base_url"],
    "model": PRESETS["glm-4-flash"]["model"],
    "api_key": "",           # 当前预设正在用的 Key（由 keys[preset] 派生）
    # 【新增 v2.4】按预设分别保存密钥。原来整份配置只有一个 api_key：
    # 填了 GLM 的 Key 再填 DeepSeek 会把前者覆盖，切回去时界面还显示
    # 「已保存密钥」却用错 Key，表现为「换个模型就保存不了」。
    "keys": {},              # {"glm-4-flash": "sk-…", "deepseek-v4-flash": "sk-…"}
    "price_in": 0.0,
    "price_out": 0.0,
    "currency": "CNY",
    "timeout": 120,
    "max_tokens": 2400,
}


def preset_info(key):
    return PRESETS.get(key) or PRESETS["custom"]


def _blank():
    """DEFAULTS 含可变字典，浅拷贝会让所有调用共用同一个 keys，必须深拷贝。"""
    return json.loads(json.dumps(DEFAULTS))


def mask(key):
    key = str(key or "")
    return (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已填" if key else "")


def load_config():
    """读配置；文件不存在/损坏都回落默认值，绝不因为配置把服务带崩。

    【新增 v2.4】旧版配置没有 keys 字段，这里做一次无损迁移：把已有的
    api_key 归到它当时的 preset 名下，用户不必重新录入。
    """
    cfg = _blank()
    try:
        if CONFIG.exists():
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    except Exception:
        cfg = _blank()
    if not isinstance(cfg.get("keys"), dict):
        cfg["keys"] = {}
    if not cfg["keys"] and str(cfg.get("api_key") or "").strip():
        cfg["keys"][str(cfg.get("preset") or "glm-4-flash")] = str(cfg["api_key"]).strip()
    return cfg


def save_config(data):
    """原子写配置；校验 URL / 模型名；空 api_key 表示沿用**该预设**已存的密钥。"""
    if not isinstance(data, dict):
        raise ValueError("配置格式不对")
    cur = load_config()
    cfg = json.loads(json.dumps(cur))          # 深拷贝，别把 keys 改到缓存上
    for k in ("enabled", "preset", "base_url", "model", "price_in",
              "price_out", "currency", "timeout", "max_tokens"):
        if k in data:
            cfg[k] = data[k]
    preset = str(cfg.get("preset") or "glm-4-flash")
    keys = cfg.setdefault("keys", {})
    new_key = str(data.get("api_key") or "").strip()
    if new_key:
        # 只写当前预设自己的槽位，别的模型的 Key 原封不动
        keys[preset] = new_key
        cfg["api_key"] = new_key
    else:
        # 切换预设但未填新 Key：自动带出该预设已存的 Key；没存过就置空，
        # 下面启用校验会明确提示「请填 API Key」，不会悄悄用别的模型的 Key。
        cfg["api_key"] = str(keys.get(preset) or "")
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["base_url"] = str(cfg["base_url"] or "").strip().rstrip("/")
    cfg["model"] = str(cfg["model"] or "").strip()
    if cfg["enabled"]:
        if not cfg["base_url"].startswith(("http://", "https://")):
            raise ValueError("接口地址要以 http:// 或 https:// 开头")
        if not cfg["model"]:
            raise ValueError("请填模型名")
        if not str(cfg["api_key"]).strip() and not cfg["base_url"].startswith("http://127.0.0.1"):
            raise ValueError("请填 API Key")
    try:
        cfg["price_in"] = float(cfg["price_in"] or 0)
        cfg["price_out"] = float(cfg["price_out"] or 0)
        cfg["timeout"] = max(10, min(600, int(cfg["timeout"] or 120)))
        cfg["max_tokens"] = max(256, min(32768, int(cfg["max_tokens"] or 2400)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"数值配置不合法：{exc}") from exc
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_name(f"cloud_llm.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)          # Windows 上尽力而为，Unix 上生效
    except OSError:
        pass
    return cfg


def apply_preset(key):
    """把预设填进配置（不动任何已存密钥），供界面切换下拉框时调用。

    【修改 v2.4】返回该预设自己的 masked_key / has_key，前端切换时就能
    提示「这个模型已存 / 未存密钥」；enabled 沿用当前值，别一换模型就关云端。
    """
    p = preset_info(key)
    saved = load_config()
    cfg = _blank()
    cfg.update({
        "preset": key if key in PRESETS else "custom",
        "base_url": p["base_url"], "model": p["model"],
        "price_in": p["price_in"], "price_out": p["price_out"],
        "currency": p["currency"],
        "enabled": bool(saved.get("enabled")),
    })
    own = str((saved.get("keys") or {}).get(key) or "")
    if not own and saved.get("preset") == key:       # 旧配置无损迁移兜底
        own = str(saved.get("api_key") or "")
    cfg["api_key"] = own
    cfg["masked_key"] = mask(own)
    cfg["has_key"] = bool(own)
    return cfg


def status():
    cfg = load_config()
    keys = cfg.get("keys") or {}
    masked = mask(cfg.get("api_key"))
    p = preset_info(cfg.get("preset"))
    return {
        "enabled": bool(cfg.get("enabled")),
        "configured": bool(cfg.get("api_key") or str(cfg.get("base_url", "")).startswith("http://127.0.0.1")),
        "preset": cfg.get("preset"),
        "label": p["label"], "note": p["note"],
        "base_url": cfg.get("base_url"), "model": cfg.get("model"),
        "masked_key": masked,
        "keys_saved": [k for k in PRESETS if str(keys.get(k) or "").strip()],
        "price_in": cfg.get("price_in"), "price_out": cfg.get("price_out"),
        "currency": cfg.get("currency"), "usd_cny": USD_CNY,
        "presets": [{"id": k, "label": v["label"]} for k, v in PRESETS.items()],
    }


def cost_cny(usage, cfg=None):
    """把 prompt/completion token 折算成人民币，仅用于界面估算。"""
    cfg = cfg or load_config()
    rate = USD_CNY if cfg.get("currency") == "USD" else 1.0
    pin = float(cfg.get("price_in") or 0) * rate
    pout = float(cfg.get("price_out") or 0) * rate
    i = int((usage or {}).get("prompt_tokens") or 0)
    o = int((usage or {}).get("completion_tokens") or 0)
    return round((i * pin + o * pout) / 1_000_000, 6)


def _opener():
    """默认绕过环境变量代理（本机环境代理已失效返 502）；显式 ASR_LLM_PROXY 才启用。"""
    proxy = os.environ.get("ASR_LLM_PROXY")
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CloudChat:
    """对 llm_polish 伪装成 llama-cpp-python 的 Llama 实例。"""

    def __init__(self, cfg=None, log=print):
        self.cfg = cfg or load_config()
        self.log = log
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.calls = 0
        self.model_name = self.cfg.get("model") or ""
        self.timeout = int(self.cfg.get("timeout") or 120)

    def create_chat_completion(self, messages=None, temperature=0.15,
                               top_p=0.9, max_tokens=None, **kw):
        cfg = self.cfg
        url = str(cfg["base_url"]).rstrip("/") + "/chat/completions"
        msgs = []
        for m in messages or []:
            c = str(m.get("content") or "")
            # /no_think 是 Qwen3 本地模型专用的开关，云端模型不认识，去掉
            c = c.replace("\n/no_think", "").rstrip()
            msgs.append({"role": m.get("role") or "user", "content": c})
        payload = {
            "model": cfg["model"],
            "messages": msgs,
            "temperature": float(temperature),
            "top_p": float(top_p),
            "max_tokens": int(max_tokens or cfg.get("max_tokens") or 2400),
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = "Bearer " + str(cfg["api_key"])
        last = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with _opener().open(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                last = RuntimeError(f"HTTP {exc.code}：{detail}")
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise last
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = RuntimeError(f"网络请求失败：{exc}")
                if attempt == 2:
                    raise last
            time.sleep(1.0 * (attempt + 1))
        else:
            raise last or RuntimeError("云端请求失败")
        if not isinstance(body, dict):
            raise RuntimeError("云端返回格式不是对象")
        if body.get("error"):
            raise RuntimeError(f"云端返回错误：{str(body['error'])[:300]}")
        u = body.get("usage") or {}
        for k in self.usage:
            self.usage[k] += int(u.get(k) or 0)
        self.calls += 1
        # 返回结构与 llama-cpp-python 对齐，_polish_chunk 不需要任何改动
        return body

    def estimate(self):
        return {"usage": dict(self.usage), "calls": self.calls,
                "cost_cny": cost_cny(self.usage, self.cfg),
                "currency": "CNY"}


def test_connection(data=None):
    """用一条极短消息试连通，供设置界面的「测试连接」按钮调用。"""
    cfg = load_config()
    if isinstance(data, dict):
        for k in ("preset", "base_url", "model", "api_key", "price_in",
                  "price_out", "currency", "timeout"):
            if k in data and data[k] not in (None, ""):
                cfg[k] = data[k]
        # 【修改 v2.4】没填新 Key 时，用「当前预设自己」已存的 Key 去测，
        # 而不是沿用上一个模型的 Key（否则换模型后测试必定 401）。
        if not str(data.get("api_key") or "").strip():
            preset = str(cfg.get("preset") or "glm-4-flash")
            cfg["api_key"] = str((cfg.get("keys") or {}).get(preset) or "")
    cfg["enabled"] = True
    cfg["base_url"] = str(cfg.get("base_url") or "").rstrip("/")
    if not cfg["base_url"].startswith(("http://", "https://")) or not cfg.get("model"):
        return False, "请先填接口地址和模型名", 0.0
    chat = CloudChat(cfg)
    t0 = time.time()
    try:
        r = chat.create_chat_completion(
            messages=[{"role": "user", "content": "只回复两个字：正常"}],
            temperature=0, max_tokens=16)
        txt = (((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return True, f"连接成功：{cfg['model']} 回复「{txt[:20]}」", round(time.time() - t0, 1)
    except Exception as exc:
        return False, str(exc), round(time.time() - t0, 1)
