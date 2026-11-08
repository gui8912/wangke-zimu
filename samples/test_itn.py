# -*- coding: utf-8 -*-
"""v1.4 受限 ITN（中文数字规整）专项回归测试。

分两段：
  unit() —— 规则边界：该转的必须转，不该动的必须一字不动；
  api()  —— /api/reclean 的 itn 开关，需要服务已在 127.0.0.1:7870 运行。

核心风险是「错转」而不是「少转」：法规条款号（第一百二十条）与
监管固定表达（四不两直、双随机一公开）一旦被改，材料就不能用了。

运行：python samples/test_itn.py
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:7870"
sys.path.insert(0, str(BASE))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS = []


def ok(name):
    PASS.append(name)
    print("  PASS", name)


# ─────────────────────── 单元 ───────────────────────

def unit():
    import itn_zh as I
    import clean_transcript as CT

    print("\n[unit] 受限 ITN 规则边界")

    # 1. 法规条款号：条 / 款 / 项 / 章 / 节 / 编 一律不动
    #    「第二十条」是必测项：只用 (?<!第) 前缀断言时，「十」前面是「二」不是「第」，
    #    会被当成数量词转成「第二10条」（v1.5 实测复现过的 P0），故改为整体遮蔽。
    for s in ["第一百二十条", "第五条第二款", "第十八条第一款", "第三章第四节",
              "第二编", "第十条第三项", "第七百三十九条", "第二十条", "第十部分",
              "依据第一百二十条和第二十条处罚", "医疗器械监管条例第二十条规定"]:
        out, log = I.normalize(s)
        assert out == s, f"条款号被改：{s} -> {out}"
        assert log == [], f"条款号产生变更记录：{log}"
    ok("条款号（条/款/项/章/节/编/部分，含「第二十条」）零改动")

    # 2. 监管固定表达：含中文数字但不是数量，一律不动
    terms = ["四不两直", "双随机一公开", "四个最严", "三品一械", "十四五",
             "两品一械", "一把手", "一刀切", "一揽子", "第一时间", "三令五申",
             "五花八门", "十全十美", "千方百计", "万无一失"]
    for s in terms:
        out, _ = I.normalize(s)
        assert out == s, f"监管/成语表达被改：{s} -> {out}"
    ok("监管术语与成语零改动（15 例）")

    # 3. 年份：逐位转换 + 范围保护
    assert I.normalize("二零二六年")[0] == "2026年"
    assert I.normalize("一九九八年")[0] == "1998年"
    assert I.normalize("二〇二五年")[0] == "2025年"       # 〇 U+3007
    assert I.normalize("三三三三年")[0] == "三三三三年"    # 超出 1900–2100，不转
    assert I.normalize("一八九九年")[0] == "一八九九年"    # 超下界，不转
    ok("四位年份：正常转换 + 越界保护")

    # 4. 月日：合法转换 + 非法值保护
    assert I.normalize("六月一日")[0] == "6月1日"
    assert I.normalize("十二月三十一号")[0] == "12月31号"
    assert I.normalize("十三月一日")[0] == "十三月一日"     # 月份越界
    assert I.normalize("二月三十二日")[0] == "二月三十二日"  # 日期越界
    ok("月日：合法转换 + 越界保护")

    # 5. 百分比（含小数）
    assert I.normalize("百分之九十八点五")[0] == "98.5%"
    assert I.normalize("百分之五十")[0] == "50%"
    assert I.normalize("百分之一百")[0] == "100%"
    ok("百分比：整数与小数")

    # 6. 数量词：≥10 转，<10 保留汉字
    assert I.normalize("三百五十家")[0] == "350家"
    assert I.normalize("十二批次")[0] == "12批次"
    assert I.normalize("一千二百三十四件")[0] == "1234件"
    assert I.normalize("三个人")[0] == "三个人"      # <10 保留
    assert I.normalize("五个品种")[0] == "五个品种"    # <10 保留
    ok("数量词：≥10 转 / 个位数保留汉字")

    # 7. 万 / 亿 不摊平
    assert I.normalize("五十万元")[0] == "50万元"
    assert I.normalize("三亿元")[0] == "3亿元"
    ok("万/亿：保留单位字不摊成裸数字")

    # 8. 文号转阿拉伯
    assert I.normalize("第七百三十九号")[0] == "第739号"
    ok("文号（第X号）转阿拉伯")

    # 9. 组合场景：同一句里该转的转、该保护的保
    src = "依据《药品管理法》第一百二十条，二零二六年共检查三百五十家，合格率百分之九十八点五。"
    out, log = I.normalize(src)
    assert "第一百二十条" in out, out
    assert "2026年" in out and "350家" in out and "98.5%" in out, out
    ok("组合句：四种规则并行且互不干扰")

    # 10. 幂等：已规整的文本再跑一遍不变
    once = I.normalize(src)[0]
    twice, log2 = I.normalize(once)[0], I.normalize(once)[1]
    assert twice == once and log2 == []
    ok("幂等：重复规整无二次改动")

    # 11. 空文本与无数字文本
    assert I.normalize("") == ("", [])
    assert I.normalize("飞行检查依据法规开展")[0] == "飞行检查依据法规开展"
    ok("空文本 / 无数字文本安全")

    # 12. cn2an 缺失时降级：返回原文 + 一条警告，不抛异常
    saved = I._cn2an
    try:
        I._cn2an = None
        I.AVAILABLE = False
        out, log = I.normalize("百分之五十")
        assert out == "百分之五十", out
        assert log and "cn2an" in log[0], log
    finally:
        I._cn2an = saved
        I.AVAILABLE = True
    ok("缺 cn2an 时优雅降级（不抛异常）")

    # 13. Cleaner 开关双向生效
    cfg = CT.load_rules(BASE / "rules" / "filler_words.json")
    corr = CT.load_corrections(BASE / "terms" / "corrections.txt")
    src2 = "嗯，合格率百分之九十八点五，依据第一百二十条处罚。"
    on_text, on_ch, _ = CT.Cleaner(cfg, corr, itn=True).clean(src2)
    off_text, off_ch, _ = CT.Cleaner(cfg, corr, itn=False).clean(src2)
    assert "98.5%" in on_text and "第一百二十条" in on_text, on_text
    assert "百分之九十八点五" in off_text and "98.5%" not in off_text, off_text
    assert any(c[0] == "数字规整" for c in on_ch), on_ch
    assert not any(c[0] == "数字规整" for c in off_ch), off_ch
    ok("Cleaner 开关：开则转、关则原样，条款号两态均保护")

    # 14. ITN 不应触发「保护词丢失」告警
    src3 = "严格执行四不两直，二零二六年检查三百五十家。"
    _, _, warns = CT.Cleaner(cfg, corr, itn=True).clean(src3)
    assert not any("保护词丢失" in w for w in warns), warns
    ok("ITN 不产生保护词丢失告警")


# ─────────────────────── HTTP ───────────────────────

def api():
    print("\n[api] /api/reclean 的 itn 开关")

    try:
        opener.open(f"{API}/api/health", timeout=6).read()
    except Exception as exc:
        print("  SKIP 服务未运行:", exc)
        return

    cues = json.dumps([{"idx": 1, "time": "00:00:01,000 --> 00:00:04,000",
                        "text": "合格率百分之九十八点五，检查三百五十家，依据第一百二十条。"}],
                      ensure_ascii=False)

    texts = {}
    for flag in ("true", "false"):
        data = urllib.parse.urlencode({"file_id": "itn_probe", "cues_json": cues,
                                       "itn": flag}).encode()
        req = urllib.request.Request(f"{API}/api/reclean", data=data)
        d = json.loads(opener.open(req, timeout=20).read())
        texts[flag] = d["cues"][0]["text"]

    assert "98.5%" in texts["true"] and "350家" in texts["true"], texts["true"]
    assert "第一百二十条" in texts["true"], texts["true"]
    ok("itn=true：数字规整且条款号保留")

    assert "百分之九十八点五" in texts["false"], texts["false"]
    assert "98.5%" not in texts["false"], texts["false"]
    ok("itn=false：完全保留汉字数字写法")

    # 省略 itn 时应默认开启（向后兼容旧前端）
    data = urllib.parse.urlencode({"file_id": "itn_probe", "cues_json": cues}).encode()
    req = urllib.request.Request(f"{API}/api/reclean", data=data)
    d = json.loads(opener.open(req, timeout=20).read())
    assert "98.5%" in d["cues"][0]["text"], d["cues"][0]["text"]
    ok("省略 itn 时默认开启（旧前端兼容）")


if __name__ == "__main__":
    unit()
    api()
    print(f"\n合计通过 {len(PASS)} 项")
