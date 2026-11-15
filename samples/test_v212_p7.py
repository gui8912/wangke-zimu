# -*- coding: utf-8 -*-
"""v2.12-p7 专项：GLM-4-Flash 长稿润色「首块概括压缩 → 整稿白跑」回归。

现场（服务日志 asr-pipeline-20261115-205610.log 第 177 / 189 行）：
  [llm] 润色失败：第 1 块输出异常（输出过短（3203 → 1294 字）），已中止以免整稿白跑
  同一篇稿子连试两次，输出 1294 / 1248 字（约 40%），稳定撞 45% 红线。

根因：
  ① GLM-4-Flash 在 3000 字大块下会无视固定提示词第 3/9 条，把整段概括压缩。
     实测同一段切成 1000 字块后保留率 71%（3000 字块仅 53%），
     DeepSeek V4-Flash 94%、硅基流动 Qwen3-8B 69%。
  ② polish() 对**首块**不合格直接 raise，后面跑得再好也全部丢弃 ——
     正是用户反复强调的「花长时间生成后不能采用 = 白跑」。

修复（提示词是用户固定下来的，一字未动）：
  · _polish_with_split() 检测到长度塌陷时自动对半细分重跑（最多 2 层）；
  · 首块最终仍不合格时只保留**这一块**原文，其余块照常润色并返回结果；
  · 返回 first_chunk_fallback，前端给醒目提示。

本脚本纯离线：用假模型精确复现「首次只输出 35%」的场景，
不联网、不碰服务与队列，可在识别任务运行中安全执行。
"""
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import llm_polish as LP  # noqa: E402

PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


class FakeLLM:
    """按「这是第 N 次调用」控制输出保真度，精确复现概括压缩。"""

    def __init__(self, first_ratio=0.35, rest_ratio=0.80):
        self.calls = []
        self.first_ratio = first_ratio
        self.rest_ratio = rest_ratio

    def estimate(self):
        return {"calls": len(self.calls), "cost_cny": 0.0}

    def _resp(self, text):
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}]}

    def create_chat_completion(self, messages, **kw):
        user = messages[-1]["content"]
        m = re.search(r"<transcript>\n(.*)\n</transcript>", user, re.S)
        src = m.group(1) if m else user
        self.calls.append(len(src))
        ratio = self.first_ratio if len(self.calls) == 1 else self.rest_ratio
        return self._resp(src[:max(1, int(len(src) * ratio))])


CN = "药品经营质量管理规范检查要点，生产企业应当按照GMP要求建立质量管理体系，并明确管代职责。"


def make_text(chars=1200):
    return "\n".join(CN for _ in range(max(1, chars // len(CN) + 1)))


def main():
    src = (BASE / "llm_polish.py").read_text(encoding="utf-8")
    web = (BASE / "web/index.html").read_text(encoding="utf-8")

    print("[源码契约]")
    ok("长度塌陷阈值单一来源 SHORT_RATIO", "SHORT_RATIO = 0.45" in src)
    ok("_sanity 使用 SHORT_RATIO", "len(src) * SHORT_RATIO" in src
       or "len(out) < len(src) * SHORT_RATIO" in src)
    ok("自动细分复用同一阈值", "len(out) < len(text) * SHORT_RATIO" in src)
    ok("有按整句对半切的辅助函数", "def _halves(text):" in src)
    ok("细分有深度上限", "depth < 2 and len(text) >= 480" in src)
    ok("不再有「已中止以免整稿白跑」", "已中止以免整稿白跑" not in src)
    ok("首块原因会返回给前端", '"first_chunk_fallback": first_fallback' in src)
    ok("前端有醒目提示", "d.first_chunk_fallback" in web)
    ok("提示点名更忠实的替代模型", "DeepSeek V4-Flash" in web and "Qwen3-8B" in web)
    ok("用户固定的提示词第 3 条未改", "原文每一句的信息都必须保留" in src)
    ok("用户固定的提示词第 9 条未改", "不因篇幅长而跳过、概括或压缩任何部分" in src)
    ok("正文仍单独包在 transcript 标签里",
       'PROMPT = "<transcript>\\n{text}\\n</transcript>"' in src)

    print("[逻辑：输出过短会自动细分重跑]")
    fake = FakeLLM(first_ratio=0.35, rest_ratio=0.80)
    text = make_text(1200)
    out = LP._polish_with_split(fake, text)
    ok("首次输出确实过短（模拟成功）", len(fake.calls) >= 1 and len(out) > 0)
    ok("触发了对半细分（至少 3 次调用）", len(fake.calls) >= 3, fake.calls)
    good, why = LP._sanity(text, out)
    ok("细分后通过健康检查", good, f"ratio={len(out)/len(text):.2f} {why}")
    ok("细分后内容明显变多", len(out) > len(text) * 0.45, len(out))

    print("[逻辑：首块最终不合格也不整稿中止]")
    old_ensure, old_chunk = LP._ensure, LP.CLOUD_CHUNK_CHARS
    try:
        LP.CLOUD_CHUNK_CHARS = 500          # 强制分成多块
        always_short = FakeLLM(first_ratio=0.30, rest_ratio=0.30)
        LP._ensure = lambda log=None: always_short
        long_text = make_text(1200)
        r = LP.polish(long_text, log=lambda m: None)
        ok("polish() 没有抛异常（不再白跑）", isinstance(r, dict))
        ok("确实分成了多块", r.get("chunks", 0) >= 2, r.get("chunks"))
        ok("首块原因被带回", bool(r.get("first_chunk_fallback")), r.get("first_chunk_fallback"))
        ok("回退块数被统计", r.get("fallback_chunks", 0) >= 1, r.get("fallback_chunks"))
        ok("结果非空且含原文", len(r.get("polished") or "") > 0
           and CN[:12] in r["polished"])
        ok("审计结构仍完整", isinstance(r.get("audit"), dict)
           and isinstance(r.get("diff"), list))

        # 首块正常、仅后续块失败时，不该误报首块问题
        class Mixed(FakeLLM):
            def create_chat_completion(self, messages, **kw):
                user = messages[-1]["content"]
                m = re.search(r"<transcript>\n(.*)\n</transcript>", user, re.S)
                s = m.group(1) if m else user
                self.calls.append(len(s))
                ratio = 0.90 if len(self.calls) == 1 else 0.20
                return self._resp(s[:max(1, int(len(s) * ratio))])
        mixed = Mixed()
        LP._ensure = lambda log=None: mixed
        r2 = LP.polish(make_text(1200), log=lambda m: None)
        ok("首块正常时不误报", r2.get("first_chunk_fallback") == "",
           r2.get("first_chunk_fallback"))
        ok("后续块失败仍只回退该块", r2.get("fallback_chunks", 0) >= 1,
           r2.get("fallback_chunks"))
    finally:
        LP._ensure, LP.CLOUD_CHUNK_CHARS = old_ensure, old_chunk

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
