#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""文稿对齐：把讲义 / PPT 文字稿对齐到 ASR 的字级时间轴。

思路（不重跑声学模型）：
1. 识别结果已经带「每个有效字」的 [start_ms, end_ms]；
2. 把文稿按句切分，逐句在 ASR 有效字串的一个滑动窗口里做局部模糊匹配
   （rapidfuzz.partial_ratio_alignment），拿到该句在 ASR 里的字区间；
3. 句区间 → 时间戳区间；匹配不上的句子按前后锚点线性插值；
4. 长句复用 transcribe.split_cue 按词边界切成字幕行。

不改音频、不调模型，纯文本对齐，通常一两秒完成。
"""

import math                  # modified(v1.2): 时间戳合法性校验
import re

from rapidfuzz import fuzz

import transcribe as T

# modified(v1.2): 原来在这里又硬编码了一份标点集合，且与识别模块的
# count_effective 口径略有差异（换行/制表符）。统一引用，避免字级时间戳错位。
PUNCT = T.PUNCT


def _clean_ts(rows) -> list:
    """modified(v1.2): 校验并规范化字级时间戳，坏项用上一字末尾占位。

    <stem>.json 是磁盘上的外部文件，可能被手工改坏或来自旧版本；原来直接
    ts[a][0]，遇到 None / 字符串 / 单元素数组会抛 TypeError（HTTP 500），
    或算出 end < start 的倒挂时间轴。占位而不是丢弃，保证「一字一戳」的
    下标关系不被破坏。
    """
    out, last_end = [], 0.0
    for t in rows or []:
        a = b = None
        if isinstance(t, (list, tuple)) and len(t) >= 2:
            try:
                a, b = float(t[0]), float(t[1])
            except (TypeError, ValueError):
                a = b = None
        if (a is None or b is None
                or not (math.isfinite(a) and math.isfinite(b))
                or a < 0 or b < 0):
            a = b = last_end
        if b < a:
            a, b = b, a
        out.append([a, b])
        last_end = b
    return out


def _eff(s: str) -> str:
    """只保留参与时间戳对齐的有效字（去标点与空白）"""
    return "".join(c for c in s if c not in PUNCT)


def split_sentences(text: str) -> list:
    """按句末标点与换行切句；少于 2 个有效字的碎片并回上一句。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = re.split(r"(?<=[。！？；!?;])\s*|\n+", text)
    out = []
    for p in parts:
        s = p.strip()
        if not s:
            continue
        if out and len(_eff(s)) < 2:
            out[-1] += s
        else:
            out.append(s)
    return out


def _fill_missing(rows: list, total_dur: float) -> list:
    """给没有匹配上的句子按前后锚点按字数插值。"""
    anchors = [i for i, r in enumerate(rows) if r.get("a") is not None]
    if not anchors:
        total_chars = sum(max(1, len(_eff(r["text"]))) for r in rows) or 1
        cur = 0.0
        for r in rows:
            span = total_dur * max(1, len(_eff(r["text"]))) / total_chars
            r["start"], r["end"] = cur, min(total_dur, cur + span)
            cur += span
        return rows

    def spread(group, t0, t1):
        chars = sum(max(1, len(_eff(r["text"]))) for r in group) or 1
        cur = t0
        for r in group:
            span = max(0.0, t1 - t0) * max(1, len(_eff(r["text"]))) / chars
            r["start"], r["end"] = cur, cur + span
            cur += span

    first = anchors[0]
    if first > 0:
        spread(rows[:first], 0.0, rows[first]["start"])

    for x, y in zip(anchors, anchors[1:]):
        if y == x + 1:
            continue
        t0, t1 = rows[x]["end"], rows[y]["start"]
        if t1 <= t0:
            t1 = t0 + 0.2 * (y - x)
        spread(rows[x + 1:y], t0, t1)

    last = anchors[-1]
    if last < len(rows) - 1:
        t0 = rows[last]["end"]
        spread(rows[last + 1:], t0, max(t0 + 0.5, total_dur))
    return rows


def align(script: str, asr_text: str, timestamps: list) -> dict:
    """把文稿对齐到识别时间轴。

    参数
    ----
    script      讲义 / PPT 文字稿全文
    asr_text    ASR 识别全文
    timestamps  [[start_ms, end_ms], ...]，每个有效字一个

    返回
    ----
    {cues, sentences, similarity, matched, total}
    cues 里每条同时带 text（文稿）与 asr_text（识别原句），
    前端可以切换「按文稿 / 按识别」而不用重跑。
    """
    asr_eff = _eff(asr_text or "")
    ts_all = _clean_ts(timestamps)        # modified(v1.2): 先净化外部时间戳
    n = min(len(asr_eff), len(ts_all))
    if n <= 0:
        raise ValueError(
            "识别结果没有字级时间戳，无法对齐；请用 Qwen3-ASR 或带时间戳的模型重跑一次")
    asr_eff = asr_eff[:n]
    ts = ts_all[:n]

    # 有效字下标 → 原文位置，后面切 asr_text 用
    eff_to_orig = [i for i, c in enumerate(asr_text) if c not in PUNCT][:n]

    sents = split_sentences(script)
    if not sents:
        raise ValueError("文稿为空")

    total_dur = max(0.5, float(ts[-1][1]) / 1000.0)
    rows, cursor = [], 0

    for s in sents:
        se = _eff(s)
        if not se:
            continue
        # 窗口取本句长度的 3 倍 + 240 字，至少 480 字。
        # 太小容易错过插入语，太大容易被重复短语带偏。
        win_len = min(len(asr_eff) - cursor, max(len(se) * 3 + 240, 480))
        if win_len <= 0:
            rows.append({"text": s, "a": None, "b": None, "score": 0.0})
            continue
        window = asr_eff[cursor:cursor + win_len]
        al = fuzz.partial_ratio_alignment(se, window)
        if al and al.score >= 55 and al.dest_end > al.dest_start:
            a = cursor + al.dest_start
            b = cursor + al.dest_end
            rows.append({"text": s, "a": a, "b": b, "score": al.score / 100.0})
            # 允许回退两个字：讲课时常有「这个 / 那么」被 ASR 吞掉
            cursor = max(cursor, b - 2)
        else:
            rows.append({"text": s, "a": None, "b": None, "score": 0.0})

    matched = sum(1 for r in rows if r.get("a") is not None)
    if matched == 0:
        raise ValueError("文稿与识别内容差异过大，找不到可对齐的句子；请确认文稿与音频是同一门课")

    for r in rows:
        if r.get("a") is not None:
            a = r["a"]
            b = min(r["b"], n)
            r["start"] = ts[a][0] / 1000.0
            r["end"] = ts[b - 1][1] / 1000.0
            if r["end"] <= r["start"]:
                r["end"] = r["start"] + 0.2
            oa, ob = eff_to_orig[a], eff_to_orig[b - 1] + 1
            r["asr_text"] = asr_text[oa:ob].strip()
        else:
            r["start"] = r["end"] = 0.0
            r["asr_text"] = ""

    rows = _fill_missing(rows, total_dur)

    cues, prev_end = [], 0.0
    for r in rows:
        st = max(prev_end, float(r.get("start") or 0.0))
        et = max(st + 0.2, float(r.get("end") or st + 0.2))
        # modified(v1.2): 插值尾段可能越过音频总时长，夹回有依据的边界
        et = min(et, max(st + 0.2, total_dur))
        # modified(v1.2): 纯标点句原来会切出空字幕条
        pieces = [p for p in T.split_cue(r["text"]) if p.strip()] or [r["text"]]
        total = sum(len(p) for p in pieces) or 1
        cur = st
        for p in pieces:
            span = (et - st) * len(p) / total
            cues.append({
                "start": round(cur, 3),
                "end": round(cur + span, 3),
                "text": p,
                "asr_text": r.get("asr_text") or "",
                "similarity": round(float(r.get("score") or 0.0), 3),
                "warn": bool(r.get("a") is None or (r.get("score") or 0) < 0.75),
            })
            cur += span
        prev_end = et

    overall = sum(float(r.get("score") or 0.0)
                  for r in rows if r.get("a") is not None) / max(1, len(rows))
    return {
        "cues": cues,
        "sentences": [
            {"text": r["text"], "start": round(r["start"], 3), "end": round(r["end"], 3),
             "score": round(float(r.get("score") or 0.0), 3),
             "asr_text": r.get("asr_text") or ""}
            for r in rows
        ],
        "similarity": round(overall, 3),
        "matched": matched,
        "total": len(rows),
    }
