# -*- coding: utf-8 -*-
"""v1.7 讲稿层 LLM 顺滑专项回归。

分两段：
  unit() —— 分块 / 末句 / 五项红线 / 句级 diff 的边界，**不加载模型**，秒级完成；
  api()  —— /api/llm/status 与 /api/llm/polish 的参数校验，需要服务在 7870 运行。

真实推理不在本脚本里跑（一次十几秒，且依赖 2.4GB 模型）：
  端到端已用 out/test_lesson.raw.txt 实测 343 → 275 字、五项硬红线全过；
  要复跑用 python -c 直接调 LP.polish()，或点界面上的「LLM 润色」。

运行：python samples/test_v17.py
"""
import sys
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


SRC = ("呃，各位同仁大家好，那么今天我们来讲一下两品一械的这个监管要求。\n"
       "就是说我发现很多企业在冷链药品的温湿度监测上是不到位的。\n"
       "依据药品管理法第一百二十条，合格率达到百分之九十八点五。\n"
       "那今天就到这里，谢谢大家。")
# 合规润色稿：只删口语、调语序，术语/条款号/数字表述原样
# （「百分之九十八点五」不能写成 98.5%——那是 ITN 的职责，提示词里写死了不让模型碰）
GOOD = ("各位同仁大家好，今天我们来讲一下两品一械的监管要求。\n"
        "很多企业在冷链药品的温湿度监测上不到位。\n"
        "依据药品管理法第一百二十条，合格率达到百分之九十八点五。\n"
        "那今天就到这里，谢谢大家。")


def unit():
    import llm_polish as LP

    # 1. 分块不切断句子
    long_text = "\n".join(["这是一句用来测试分块边界的完整句子，长度大约二十多字。" for _ in range(40)])
    chunks = LP.chunk_text(long_text, limit=200)
    assert len(chunks) > 1, chunks
    for c in chunks:
        assert len(c) <= 260, f"块过大：{len(c)}"          # 单句可略超 limit
        assert c.endswith("。"), f"块尾不是句号，句子被切断了：{c[-12:]!r}"
    assert "".join(chunks).replace("\n", "") == long_text.replace("\n", "")
    ok(f"分块按整句切，{len(chunks)} 块且首尾完整")

    # 2. 长行按句末切（整段粘贴没有换行的情况）
    one_line = SRC.replace("\n", "")
    assert len(LP.chunk_text(one_line, limit=30)) > 1
    ok("无换行整段也能按句号切开")

    # 3. 末句带标点
    assert LP.last_sentence(SRC) == "那今天就到这里，谢谢大家。", LP.last_sentence(SRC)
    assert LP.last_sentence("") == ""
    assert LP.last_sentence("没有句号的一句") == "没有句号的一句"
    ok("末句提取带标点")

    # 4. 全部通过：好稿子不该被拦
    a = LP.audit(SRC, GOOD)
    assert not LP.blocked(a), LP.blocked(a)
    assert a["末句保留"]["pass"] and a["长度合理"]["pass"]
    ok("合规润色稿五项硬红线全过")

    # 5. 术语被替换/删掉 → 拦住（"两品一械"→"两品" 就是真丢了）
    bad = GOOD.replace("两品一械", "两品")
    assert any("术语" in x for x in LP.blocked(LP.audit(SRC, bad))), LP.blocked(LP.audit(SRC, bad))
    ok("术语被替换 → 拦住")

    # 6. 条款号被改成阿拉伯数字 → 拦住
    bad = GOOD.replace("第一百二十条", "第120条")
    assert any("条款号" in x for x in LP.blocked(LP.audit(SRC, bad)))
    ok("条款号被改 → 拦住")

    # 7. 数字被改 → 拦住（把汉字写法改成阿拉伯数字也算改，ITN 才该干这个）
    bad = GOOD.replace("百分之九十八点五", "98.5%")
    assert any("数字" in x for x in LP.blocked(LP.audit(SRC, bad)))
    ok("数字表述被改 → 拦住")

    # 8. 末句被删 → 拦住（这是实测最容易发生的失败）
    bad = "\n".join(GOOD.splitlines()[:-1])
    blk = LP.blocked(LP.audit(SRC, bad))
    assert any(x.startswith("末句保留") for x in blk), blk
    ok("末句被删 → 拦住")

    # 9. 模型自由发挥（长度暴涨）→ 拦住
    bad = GOOD + "\n" + "补充：以上内容仅供参考，具体以最新法规为准，各单位要抓好落实。" * 4
    assert any("长度" in x for x in LP.blocked(LP.audit(SRC, bad)))
    ok("长度暴涨 → 拦住")

    # 10. 参考项不参与拦截：残留口语不算硬红线
    a = LP.audit(SRC, GOOD.replace("各位同仁大家好", "各位同仁大家好，这个"))
    assert a["残留口语（参考）"]["pass"] is False
    assert not any("残留口语" in x for x in LP.blocked(a)), LP.blocked(a)
    ok("残留口语只作参考，不阻断采用")

    # 11. 原文没有条款号/数字时，说明写「跳过」而不是「0 处一致」
    a = LP.audit("今天讲质量管理体系的基本要求，大家要记住。",
                 "今天讲质量管理体系的基本要求，大家要记住。")
    assert "跳过" in a["条款号保留"]["note"] and "跳过" in a["数字保留"]["note"]
    ok("原文无条款号/数字时标注跳过")

    # 12. 句级 diff：多行原文被合并成一段时，仍按句对照
    ops = LP.diff_ops(SRC, GOOD)
    ne = [o for o in ops if o["op"] != "equal"]
    assert ne, "应有差异"
    for o in ops:
        assert set(o) == {"op", "before", "after"}, o
    # 关键：把 before 拼回去必须等于原稿——句级切分不能吞掉任何内容
    # （连续的改动会被 SequenceMatcher 并成一个 replace 块，所以不断言段数）
    rebuilt = "".join(o["before"] for o in ops)
    assert rebuilt.replace("\n", "") == SRC.replace("\n", ""), "句级 diff 丢失了内容"
    # 最后一句没改动，应当作为 equal 段原样存在，而不是被并进 replace
    assert any(o["op"] == "equal" and "谢谢大家" in o["before"] for o in ops)
    ok(f"句级 diff 可逆，{len(ne)} 处差异，未改动的句子保持 equal")

    # 13. 术语表驱动：术语来自 terms/，不是硬编码
    terms = LP.must_keep_terms(SRC)
    assert "两品一械" in terms, terms
    assert all(len(t) >= 4 for t in terms)
    assert LP.must_keep_terms("完全不相关的一句话") == []
    ok(f"术语清单来自 terms/（本稿命中 {len(terms)} 条）")

    # 14. 模型缺失时 available() 明确报错而不是抛异常
    old = LP.MODEL_DIR
    LP.MODEL_DIR = Path("Z:/definitely/not/here")
    try:
        okk, msg = LP.available()
        assert okk is False and "未找到" in msg, (okk, msg)
    finally:
        LP.MODEL_DIR = old
    ok("模型缺失时 available() 返回原因，不抛异常")


def api():
    # 15. status 结构与真实可用性
    st = __import__("json").loads(opener.open(API + "/api/llm/status", timeout=10).read())
    assert set(("available", "detail", "max_input", "chunk_chars")) <= set(st), st
    assert st["max_input"] == 8000
    ok(f"/api/llm/status（可用={st['available']}，{st['detail']}）")

    # 16. 空文本 400
    req = urllib.request.Request(API + "/api/llm/polish",
                                 data=b'{"text":"   "}',
                                 headers={"Content-Type": "application/json"})
    try:
        opener.open(req, timeout=10)
        raise AssertionError("应当 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    ok("/api/llm/polish 空文本 400")

    # 17. 超长 400（不能等模型跑完才报错）
    body = ('{"text":"' + "字" * 8001 + '"}').encode()
    req = urllib.request.Request(API + "/api/llm/polish", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        opener.open(req, timeout=20)
        raise AssertionError("应当 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    ok("/api/llm/polish 超长文本 400")

    # 18. health 版本与 llm 字段
    h = __import__("json").loads(opener.open(API + "/api/health", timeout=10).read())
    assert h["version"].split(".") >= ["1", "7"], h["version"]
    assert isinstance(h.get("llm"), bool)
    ok(f"/api/health v{h['version']}，llm={h['llm']}")


def main():
    print("=== unit：分块 / 红线 / diff（不加载模型）===")
    unit()
    print("\n=== api：接口参数校验（需服务在 7870）===")
    try:
        api()
    except Exception as exc:
        print(f"  SKIP 服务未运行或版本不符：{exc}")
    print("\n" + "=" * 60)
    print(f"共 {len(PASS)} 项通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
