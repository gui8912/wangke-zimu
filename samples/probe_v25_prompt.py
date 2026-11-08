# -*- coding: utf-8 -*-
"""验证润色提示词是否仍会被模型复述（v2.5 回归）。

拿真实课件稿的一整块（700 字）走云端，检查：
  1) 输出里有没有规则回显；
  2) 长度是否合理（不低于原文 45%）；
  3) 条款号 / 数字是否原样保留。
用法：python samples/probe_v25_prompt.py
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import llm_polish as LP  # noqa: E402


def main():
    data = json.loads((BASE / "out" / "_llm" / "last.json").read_text(encoding="utf-8"))
    src = data["source"]
    chunk = LP.chunk_text(src, 700)[0]
    print(f"块长度 {len(chunk)} 字")

    llm = LP._ensure(log=print)
    try:
        out = LP._polish_chunk(llm, chunk)
    except LP.EchoOutput as exc:
        print(f"✗ 仍然复述提示词：{exc}")
        return 1

    ok, why = LP._sanity(chunk, out)
    print(f"输出 {len(out)} 字 | 回显={LP._looks_echoed(out)} | 健康={ok} {why}")
    cn = __import__("re").compile(
        "第[〇零一二三四五六七八九十百千两]+(?:条|款|项|章|节|编)")
    print(f"条款号 原文 {len(cn.findall(chunk))} → 输出 {len(cn.findall(out))}")
    print("--- 前 240 字 ---")
    print(out[:240])
    print("--- 后 160 字 ---")
    print(out[-160:])
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
