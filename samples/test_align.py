# -*- coding: utf-8 -*-
"""align.py 的合成用例：不需要模型，几秒跑完。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import align  # noqa: E402


def test_basic():
    asr = "今天我们讲药品经营质量管理规范的基本框架要注意冷链药品的温湿度监测"
    ts = [[i * 200, (i + 1) * 200] for i in range(len(align._eff(asr)))]
    script = "今天我们讲药品经营质量管理规范的基本框架。\n要注意冷链药品的温湿度监测。"
    r = align.align(script, asr, ts)
    assert len(r["cues"]) >= 2, r
    assert r["similarity"] > 0.8, r["similarity"]
    assert all(c["end"] > c["start"] for c in r["cues"])
    assert r["cues"][0]["start"] >= 0
    print("basic OK:", len(r["cues"]), "cues, similarity", r["similarity"])


def test_extra_sentence():
    asr = "今天我们讲药品经营质量管理规范"
    ts = [[i * 200, (i + 1) * 200] for i in range(len(align._eff(asr)))]
    script = "今天我们讲药品经营质量管理规范。\n这句话是讲义里有、录音里没有的。"
    r = align.align(script, asr, ts)
    assert len(r["cues"]) >= 2, r
    print("extra OK:", len(r["cues"]), "cues, similarity", r["similarity"])


def test_fuzzy():
    asr = "药品经营质量管理规范的基本框架"
    ts = [[i * 200, (i + 1) * 200] for i in range(len(align._eff(asr)))]
    script = "药品经营质量管理办法的基本框架。"
    r = align.align(script, asr, ts)
    assert r["cues"], r
    assert 0 < r["similarity"] < 1
    print("fuzzy OK:", len(r["cues"]), "cues, similarity", r["similarity"])


def test_no_ts():
    try:
        align.align("文稿内容在这里", "识别内容在这里", [])
    except ValueError:
        print("no_ts OK: 空时间戳被拒绝")
        return
    raise AssertionError("空时间戳应当报错")


if __name__ == "__main__":
    test_basic()
    test_extra_sentence()
    test_fuzzy()
    test_no_ts()
    print("ALL ALIGN TESTS PASSED")
