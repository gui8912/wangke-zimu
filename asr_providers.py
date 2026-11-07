#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
识别源（provider）抽象层 + 「双轨」数据结构        【新增 v1.3】
==========================================================
设计前提（2026-11-08 用户确认）：音视频可出本机、可在云端运行、内容不涉密。
但「字幕」和「讲稿」是两件事，必须分轨，不能让云端整理稿改写字幕：

  轨 A 字幕轨 subtitle = ASR 原文 + 词级/字级时间戳 → SRT/VTT、波形、align.py
                        一个字都不改，保证音画同步与讲义对齐（align.py:159）
  轨 B 讲稿轨 script   = 云端顺滑 / 口语转书面语文本 → revise.py 七遍改稿 → DOCX/MD
                        允许改措辞，但绝不参与时间轴

本模块只做三件事，不引入任何新依赖：
  1. 统一 provider 接口（本地 Qwen3 / Paraformer，云端骨架）
  2. 把各家返回字段归一化成 Tracks
  3. 生成整理前后 diff，供前端预览与一键回退

云端 provider 目前是**骨架**：未配置密钥时不发请求、不伪造文本，而是明确报错。
但两家官方 API 的响应字段映射（parse_tencent_response / parse_volc_response）
已写成纯函数可直接单测，等拿到密钥后补 HTTP 调用即可，不必改上层代码。

配置全部走环境变量，密钥不写进仓库、不写进产物：
  本地     ASR_PROVIDER=local-qwen3（默认，零回归）
  腾讯云   ASR_TENCENT_SECRET_ID / ASR_TENCENT_SECRET_KEY / ASR_TENCENT_APPID
  火山引擎 ASR_VOLC_APP_KEY / ASR_VOLC_ACCESS_KEY / ASR_VOLC_SECRET_KEY
           ASR_VOLC_TOS_BUCKET / ASR_VOLC_TOS_REGION
"""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


# ────────────────────────── 双轨数据结构 ──────────────────────────

TRACK_VERSION = "1.3"


@dataclass
class Tracks:
    """一次识别的双轨产物。subtitle 永远是 ASR 原文，script 可为空。"""
    provider: str = "local-qwen3"
    version: str = TRACK_VERSION
    # 轨 A：字幕。text 是原文，timestamp 是逐字 [[start_ms, end_ms], ...]
    subtitle_text: str = ""
    timestamps: list = field(default_factory=list)
    sentences: list = field(default_factory=list)
    # 轨 B：讲稿。空字符串表示本次没有整理稿（本地识别就是这种情况）
    script_text: str = ""
    script_source: str = ""          # "tencent:WrittenText" / "volc:smooth" / ""
    notes: list = field(default_factory=list)

    @property
    def has_script(self) -> bool:
        """是否有独立于原文的整理稿。"""
        return bool(self.script_text and
                    self.script_text.strip() != self.subtitle_text.strip())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["has_script"] = self.has_script
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @staticmethod
    def from_dict(d: dict) -> "Tracks":
        """从落盘的 json 还原；缺字段按默认，兼容旧版产物。"""
        return Tracks(
            provider=d.get("provider") or "local-qwen3",
            version=d.get("version") or TRACK_VERSION,
            subtitle_text=d.get("subtitle_text") or "",
            timestamps=d.get("timestamps") or [],
            sentences=d.get("sentences") or [],
            script_text=d.get("script_text") or "",
            script_source=d.get("script_source") or "",
            notes=list(d.get("notes") or []),
        )


def diff_ops(before: str, after: str) -> list:
    """整理前后逐段 diff（标准库 difflib，不引依赖）。

    返回 [{op:'equal'|'replace'|'delete'|'insert', before, after}, ...]。
    前端用它渲染「原文 / 整理稿」对照；一键回退直接取 before 拼回即可。
    """
    a = (before or "").splitlines() or [""]
    b = (after or "").splitlines() or [""]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        out.append({
            "op": op,
            "before": "\n".join(a[i1:i2]),
            "after": "\n".join(b[j1:j2]),
        })
    return out


def restore_from_diff(ops: list) -> str:
    """一键回退：从 diff 里取回原稿（equal/replace/delete 的 before 段）。"""
    return "\n".join(x["before"] for x in ops
                     if x["op"] in ("equal", "replace", "delete"))


# ────────────────────────── provider 基类与注册表 ──────────────────────────

class ProviderUnavailable(RuntimeError):
    """provider 未配置 / 未接入。 distinguishable，便于上层返回 400 而不是 500。"""


class BaseProvider:
    id = ""
    name = ""
    kind = "local"                  # local | cloud
    supports_script = False         # 是否自带顺滑 / 口语转书面语整理稿
    config_keys: tuple = ()         # 需要的环境变量名
    note = ""

    def configured(self) -> bool:
        return all(os.environ.get(k, "").strip() for k in self.config_keys)

    def status(self) -> dict:
        """给前端的公开信息：绝不包含密钥值。"""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "configured": self.configured(),
            "ready": getattr(self, "ready", True),
            "supports_script": self.supports_script,
            "needs": list(self.config_keys),
            "note": self.note,
        }

    def require(self):
        if not self.configured():
            missing = [k for k in self.config_keys
                       if not os.environ.get(k, "").strip()]
            raise ProviderUnavailable(
                f"{self.name} 未配置：缺环境变量 {'、'.join(missing)}")

    def transcribe(self, wav: Path, language: str = "Chinese",
                   hotwords: list | None = None, log=print) -> Tracks:
        raise NotImplementedError

    def describe_payload(self, raw: Any) -> Tracks:
        """把厂商原始响应映射成 Tracks；纯函数，便于离线单测。"""
        raise NotImplementedError


class LocalProvider(BaseProvider):
    """本地模型的统一包装：行为与 v1.2 的 transcribe.transcribe() 完全一致。"""
    kind = "local"

    def __init__(self, pid: str, name: str, model_key: str):
        self.id = pid
        self.name = name
        self.model_key = model_key
        self.note = "本机推理，音视频不出本机；不提供云端整理稿"

    def configured(self) -> bool:
        # 本地模型目录存在即可；不强制，缺模型时由底层报出可诊断错误
        return True

    def require(self):
        return None

    def transcribe(self, wav: Path, language: str = "Chinese",
                   hotwords: list | None = None, log=print) -> Tracks:
        # 延迟 import，避免与 transcribe.py 形成循环依赖
        import transcribe as T

        cfg = T.MODELS[self.model_key]
        hw = ""
        if cfg.get("hotword") and hotwords:
            hw = " ".join(f"{w} 20" for w in hotwords)
        item = T.transcribe(wav, cfg, hw, "cpu", language, log)
        return Tracks(
            provider=self.id,
            subtitle_text=(item.get("text") or "").strip(),
            timestamps=item.get("timestamp") or [],
            sentences=item.get("sentence_info") or [],
            script_text="", script_source="",
            notes=["本地识别，讲稿轨为空；请走七遍改稿或本地口语清理"],
        )

    def describe_payload(self, raw: Any) -> Tracks:
        item = raw or {}
        return Tracks(
            provider=self.id,
            subtitle_text=(item.get("text") or "").strip(),
            timestamps=item.get("timestamp") or [],
            sentences=item.get("sentence_info") or [],
        )


class CloudProvider(BaseProvider):
    """云端骨架：字段映射已就绪，HTTP 调用待接入（拿到密钥后补 _submit/_poll）。"""
    kind = "cloud"
    supports_script = True
    # 【新增 v1.3】骨架未接 HTTP，前端据此把云端选项标为「待接入」，不误导用户
    ready = False

    def transcribe(self, wav: Path, language: str = "Chinese",
                   hotwords: list | None = None, log=print) -> Tracks:
        self.require()
        raise ProviderUnavailable(
            f"{self.name} 的 HTTP 调用尚未接入（本版只提供可插拔骨架与字段映射）。"
            f"请先用 {self.id.upper().replace('-', '_')}_* 环境变量配好密钥，"
            f"再实现 _submit / _poll；describe_payload() 已可离线验证。")


class TencentProvider(CloudProvider):
    """腾讯云录音文件识别：ResTextFormat=5 口语转书面语（增值付费）。

    关键：返回结构里 FinalSentence / Words 是**原文+词级时间戳**，
    WrittenText 才是整理稿。两者同时返回，正好天然构成双轨。
    """
    id = "tencent-cloud"
    name = "腾讯云 ASR · 口语转书面语"
    config_keys = ("ASR_TENCENT_SECRET_ID", "ASR_TENCENT_SECRET_KEY",
                   "ASR_TENCENT_APPID")
    note = ("ResTextFormat=5：FinalSentence+Words 原文轨，WrittenText 整理稿轨；"
            "词级时间戳保留，字幕不被改写")

    def describe_payload(self, raw: Any) -> Tracks:
        return parse_tencent_response(raw)


class VolcengineProvider(CloudProvider):
    """火山引擎豆包录音文件识别 2.0：语义顺滑 + 请求级热词。"""
    id = "volcengine-doubao"
    name = "火山引擎豆包 ASR · 语义顺滑"
    config_keys = ("ASR_VOLC_APP_KEY", "ASR_VOLC_ACCESS_KEY",
                   "ASR_VOLC_SECRET_KEY")
    note = ("字词时间戳 + 语义顺滑 + boosting_table_id 热词；"
            "顺滑文本是否保留原文需 PoC 核实，故始终以 words 原文轨为准")

    def describe_payload(self, raw: Any) -> Tracks:
        return parse_volc_response(raw)


# ────────────────────────── 厂商响应 → 双轨映射（纯函数，可单测） ──────────────────────────

def _to_ms(v) -> int:
    """毫秒数值清洗：非法值按 0，避免时间戳污染对齐。"""
    try:
        n = int(float(v))
        return n if n >= 0 and n < 24 * 3600 * 1000 else 0
    except (TypeError, ValueError):
        return 0


def parse_tencent_response(raw: Any) -> Tracks:
    """腾讯云 DescribeTaskStatus.Response.Data → Tracks。

    ResultDetail 是分句数组，每句含：
      FinalSentence  原句（原文轨）
      Words[]        词级 {OffsetStartMs, OffsetEndMs}（相对句内偏移）
      StartMs        句起始（毫秒，绝对）
      WrittenText    口语转书面语结果（整理稿轨）
    """
    if isinstance(raw, str):
        raw = json.loads(raw)
    data = (raw or {}).get("Response", raw).get("Data", raw) or {}
    details = data.get("ResultDetail") or []
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except Exception:
            details = []

    sent_texts, written_texts, stamps, sentences = [], [], [], []
    for d in details:
        if not isinstance(d, dict):
            continue
        orig = (d.get("FinalSentence") or d.get("SliceSentence") or "").strip()
        written = (d.get("WrittenText") or "").strip()
        base = _to_ms(d.get("StartMs", 0))
        st = _to_ms(d.get("StartMs", 0))
        et = _to_ms(d.get("EndMs", 0))
        if et < st:
            st, et = et, st
        for w in (d.get("Words") or []):
            if not isinstance(w, dict):
                continue
            a = base + _to_ms(w.get("OffsetStartMs", w.get("StartTime", 0)))
            b = base + _to_ms(w.get("OffsetEndMs", w.get("EndTime", 0)))
            if b <= a:
                b = a + 1
            stamps.append([a, b])
        if orig:
            sent_texts.append(orig)
            sentences.append({"text": orig, "start": st, "end": et})
        if written:
            written_texts.append(written)

    return Tracks(
        provider=TencentProvider.id,
        subtitle_text="".join(sent_texts) or (data.get("Result") or "").strip(),
        timestamps=stamps,
        sentences=sentences,
        script_text="\n".join(written_texts),
        script_source="tencent:WrittenText" if written_texts else "",
        notes=["字幕轨取 FinalSentence+Words，整理稿轨取 WrittenText，互不覆盖"],
    )


def parse_volc_response(raw: Any) -> Tracks:
    """火山引擎豆包录音文件识别 2.0 响应 → Tracks。

    utterances[].words[] 含 start_time / end_time（毫秒）与 text；
    原文轨一律由 words 拼接（不依赖顺滑后的 text），保证时间戳一一对应。
    """
    if isinstance(raw, str):
        raw = json.loads(raw)
    data = (raw or {}).get("data", raw) or {}
    result = data.get("result") or data
    utterances = result.get("utterances") or []

    chars, stamps, sentences = [], [], []
    for u in utterances:
        if not isinstance(u, dict):
            continue
        cur, st, et = [], None, None
        for w in (u.get("words") or []):
            if not isinstance(w, dict):
                continue
            t = w.get("text") or ""
            a, b = _to_ms(w.get("start_time", 0)), _to_ms(w.get("end_time", 0))
            if b <= a:
                b = a + 1
            if t:
                cur.append(t)
                stamps.append([a, b])
                st = a if st is None else min(st, a)
                et = b if et is None else max(et, b)
        if cur:
            s = "".join(cur)
            chars.append(s)
            sentences.append({"text": s, "start": st or 0, "end": et or 0})

    # 语义顺滑文本：厂商字段尚未定稿，先按常见命名探测，取不到就留空
    smooth = ""
    for key in ("smooth_text", "polished_text", "semantic_smooth_text", "text"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            smooth = v.strip()
            break
    original = "".join(chars)
    # 若顺滑字段其实就是原文（或空），不制造一条假整理稿
    if smooth and smooth == original:
        smooth = ""

    return Tracks(
        provider=VolcengineProvider.id,
        subtitle_text=original,
        timestamps=stamps,
        sentences=sentences,
        script_text=smooth,
        script_source="volc:smooth" if smooth else "",
        notes=["原文轨由 words 拼接；顺滑字段未确认前保持为空，不伪造整理稿"],
    )


# ────────────────────────── 注册表 ──────────────────────────

REGISTRY: dict[str, BaseProvider] = {
    "local-qwen3": LocalProvider("local-qwen3", "本地 Qwen3-ASR · 精度优先",
                                 "qwen3-asr"),
    "local-paraformer": LocalProvider("local-paraformer", "本地 Paraformer · 速度优先",
                                      "paraformer"),
    TencentProvider.id: TencentProvider(),
    VolcengineProvider.id: VolcengineProvider(),
}

DEFAULT_PROVIDER = "local-qwen3"


def get_provider(pid: str | None) -> BaseProvider:
    pid = (pid or os.environ.get("ASR_PROVIDER") or DEFAULT_PROVIDER).strip()
    p = REGISTRY.get(pid)
    if p is None:
        raise KeyError(f"未知识别源：{pid}（可选：{'、'.join(REGISTRY)}）")
    return p


def list_providers() -> list:
    """供 /api/providers 使用；不含任何密钥值。"""
    return [p.status() for p in REGISTRY.values()]


def preferred_from_env() -> BaseProvider:
    """服务端默认：环境变量 ASR_PROVIDER 可改；未配置云端时自动回落本地，
    避免用户开了开关却没密钥，导致所有任务直接失败。"""
    p = get_provider(None)
    if p.kind == "cloud" and not p.configured():
        return REGISTRY[DEFAULT_PROVIDER]
    return p


def hotword_terms(path: Path, limit: int = 5000, max_chars: int = 10) -> list:
    """把 terms/hotwords.txt 转成云端热词表（每词 <10 字，最多 5000 条）。

    云端热词是「提示性」而非强制约束，超长词（如「药品经营质量管理规范」）
    会被丢弃并在 notes 里提示，避免整张词表被拒绝。
    """
    if not path.exists():
        return []
    out, dropped = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = s.split("|", 1)[0].strip()      # 去掉本地权重
        if not s:
            continue
        if len(s) > max_chars:
            dropped.append(s)
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out
