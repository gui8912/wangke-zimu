# -*- coding: utf-8 -*-
"""v1.5 术语修复（简称膨胀 + 漏字补全）与修订学习专项回归。

分三段：
  termfix() —— 简称/补字的规则边界；
  learn()   —— 差异提炼、重复提交去重、候选阈值、采纳写入（全部走临时目录）；
  api()     —— HTTP 接口，需要服务已在 127.0.0.1:7870 运行。

红线：任何一段都不允许写真实词表。涉及写入的一律把路径改到临时目录。

运行：python samples/test_v15.py
"""
import json
import sys
import tempfile
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


# ─────────────────────── 术语修复 ───────────────────────

def termfix():
    import termfix as TF
    import itn_zh as I
    import clean_transcript as CT

    print("\n[termfix] 简称膨胀 + 漏字补全")
    terms = TF.load_terms(BASE / "terms" / "hotwords.txt")
    aliases = TF.load_aliases(BASE / "terms" / "aliases.txt")
    assert len(terms) > 50, len(terms)
    assert any(a[0] == "GSP" for a in aliases), aliases
    ok(f"加载术语 {len(terms)} 条 / 简称 {len(aliases)} 条")

    # 1. 简称膨胀：英文缩写支持紧跟汉字（GSP认证 这种最常见的写法）
    out, _, _ = TF.fix("GSP要求企业建立质量管理体系。", terms, aliases)
    assert out.startswith("药品经营质量管理规范"), out
    out2, _, _ = TF.fix("按GSP认证要求执行。", terms, aliases)
    assert "药品经营质量管理规范认证" in out2, out2
    ok("简称膨胀：GSP / GSP认证 均能展开")

    # 2. 英文缩写不得误伤其他英文串
    out3, ch3, _ = TF.fix("这个 GDP 指标和 GSPX 都不是术语。", terms, aliases)
    assert "GDP" in out3 and "GSPX" in out3, out3
    ok("英文缩写按整词边界，不误伤 GDP / GSPX")

    # 3. 漏字补全：强上下文自动改
    cases = [
        ("依据药品经营质量规范的要求，企业应当建立体系。", "药品经营质量管理规范"),
        ("医疗器械监管条例第二十条规定。", "医疗器械监督管理条例"),
        ("根据药品生产质量规范的要求执行。", "药品生产质量管理规范"),
        ("《药品经营质量规范》第三十条", "药品经营质量管理规范"),
    ]
    for src, want in cases:
        out, ch, _ = TF.fix(src, terms, aliases)
        assert want in out, f"{src} -> {out}（缺 {want}）"
        assert any(c[0] == "术语补全" for c in ch), ch
    ok("漏字补全：4 组强上下文全部自动改正")

    # 4. 负样本：正常表述零改动（这是本功能最重要的性质）
    safe = [
        "药品管理方面的问题要重视",
        "依据药品管理的规定执行",
        "药品经营企业应当建立质量管理体系",
        "我们要求质量管理到位",
        "无菌操作是基本要求",
        "我们按照药品管理法实施条例执行",
    ]
    for s in safe:
        out, ch, sg = TF.fix(s, terms, aliases)
        assert out == s, f"误改：{s} -> {out}"
        assert not ch, ch
    ok("负样本 6 例零改动（含「药品管理」不误补成「药品管理法」）")

    # 5. 弱上下文只出建议、不改原文
    out, ch, sg = TF.fix("这个药品经营质量规范挺重要。", terms, aliases, auto=False)
    assert out == "这个药品经营质量规范挺重要。", out
    assert not ch and sg, (ch, sg)
    ok("auto=False 时只出建议，一个字都不动")

    # 6. 条款号与补全互不干扰（v1.5 修过的 P0：第二十条被拆成第二10条）
    for s in ["第二十条", "第一百二十条", "第五条第二款", "第三章第四节"]:
        assert I.normalize(s)[0] == s, s
    cfg = CT.load_rules(BASE / "rules" / "filler_words.json")
    corr = CT.load_corrections(BASE / "terms" / "corrections.txt")
    out, _, _ = CT.Cleaner(cfg, corr).clean(
        "医疗器械监管条例第二十条规定，货值金额五十万元。")
    assert "第二十条" in out and "50万元" in out and "监督管理条例" in out, out
    ok("补字 + 条款号保护 + 数字规整 三者协同无冲突")


# ─────────────────────── 修订学习 ───────────────────────

def learn():
    import learn as L

    print("\n[learn] 差异提炼 / 去重 / 阈值 / 采纳（全程临时目录）")
    # 整段重定向到临时目录：绝不允许碰真实词表
    tmp = Path(tempfile.mkdtemp(prefix="learn_test_"))
    saved = (L.LEARN_DIR, L.STATS, L.CORRECTIONS, L.ALIASES)
    L.LEARN_DIR = tmp
    L.STATS = tmp / "stats.json"
    L.CORRECTIONS = tmp / "corrections.txt"
    L.ALIASES = tmp / "aliases.txt"
    L._LAST_SIG.clear()
    try:
        # 1. 词级差异提炼：片段要干净，不能带「营」「的」这种边界字
        e = L.extract_edits("依据药品经营质量规范的要求", "依据药品经营质量管理规范的要求")
        assert ("质量规范", "质量管理规范") in e, e
        e2 = L.extract_edits("要求建立质量管里体系", "要求建立质量管理体系")
        assert any("管里" in a and "管理" in b for a, b in e2), e2
        e3 = L.extract_edits("完全相同的一句", "完全相同的一句")
        assert e3 == [], e3
        ok("词级差异提炼：补字/改字能提取，无差异返回空")

        sys_c = [{"idx": 1, "text": "依据药品经营质量规范的要求"},
                 {"idx": 2, "text": "货值金额五十万元"}]
        usr_c = [{"idx": 1, "text": "依据药品经营质量管理规范的要求"},
                 {"idx": 2, "text": "货值金额五十万元"}]

        # 2. 第一次记 1 处；同内容重复提交不重复记（自动保存每 1.2s 一次）
        assert L.record("s1", sys_c, usr_c) == 1
        assert L.record("s1", sys_c, usr_c) == 0, "重复提交不应重复累计"
        ok("重复提交去重（自动保存不会把计数刷爆）")

        # 3. 未达阈值不列候选
        assert L.pending() == [], L.pending()
        # 4. 第二课同样改法 → 达阈值
        L.record("s2", sys_c, usr_c)
        items = L.pending()
        assert len(items) == 1 and items[0]["n"] == 2, items
        assert items[0]["before"] == "质量规范", items[0]
        ok("阈值：出现 2 次才列为候选")

        # 5. 换源（句子数差很多）不学习
        assert L.record("s3", sys_c, [{"idx": i, "text": "完全不同"} for i in range(9)]) == 0
        ok("句子数差异过大判为换源，不学习")

        # 6. 采纳写入临时词表，重复采纳不重复写
        key = items[0]["key"]
        wrote, msg = L.accept(key, "correction")
        assert wrote and "corrections.txt" in msg, msg
        assert (tmp / "corrections.txt").read_text(encoding="utf-8").strip() == "质量规范=>质量管理规范"
        wrote2, _ = L.accept(key, "correction")
        assert wrote2 is False, "已存在的条目不应重复写入"
        ok("采纳写入对应词表；重复采纳自动跳过")

        # 7. 采纳后不再提示：规则已进词表，下次不会再出现同一个错，
        #    重复提示只会变成噪声
        L._LAST_SIG.clear()
        L.record("s4", sys_c, usr_c)
        L._LAST_SIG.clear()
        L.record("s5", sys_c, usr_c)
        assert L.pending() == [], "采纳过的条目不应再提示"
        ok("采纳后不再提示（规则已生效，重复提示是噪声）")

        # 8. 忽略：另造一条候选，ignore 后消失
        sys_x = [{"idx": 1, "text": "这个批次已经放行"}]
        usr_x = [{"idx": 1, "text": "这个批次已经放行上市"}]
        L._LAST_SIG.clear()
        L.record("s6", sys_x, usr_x)
        L._LAST_SIG.clear()
        L.record("s7", sys_x, usr_x)
        assert L.pending(), "新候选应出现"
        L.ignore(L.pending()[0]["key"])
        assert L.pending() == [], "忽略后不应再列出"
        ok("忽略后不再列出")
    finally:
        L.LEARN_DIR, L.STATS, L.CORRECTIONS, L.ALIASES = saved
        L._LAST_SIG.clear()


# ─────────────────────── HTTP ───────────────────────

def api():
    print("\n[api] termfix 开关 / learn 接口")
    try:
        h = json.loads(opener.open(f"{API}/api/health", timeout=6).read())
    except Exception as exc:
        print("  SKIP 服务未运行:", exc)
        return
    ver = tuple(int(x) for x in str(h.get("version", "0")).split("."))
    assert ver >= (1, 5), h
    ok(f"/api/health v{h['version']}")

    cues = json.dumps([{"idx": 1, "time": "00:00:01,000 --> 00:00:04,000",
                        "text": "GSP要求企业建立质量管理体系，依据药品经营质量规范执行。"}],
                      ensure_ascii=False)
    texts = {}
    for flag in ("true", "false"):
        data = urllib.parse.urlencode({"file_id": "v15_probe", "cues_json": cues,
                                       "itn": "true", "termfix": flag,
                                       "termfix_auto": "true"}).encode()
        d = json.loads(opener.open(urllib.request.Request(f"{API}/api/reclean", data=data),
                                   timeout=20).read())
        texts[flag] = d["cues"][0]["text"]
    assert "药品经营质量管理规范" in texts["true"] and "GSP" not in texts["true"], texts["true"]
    assert "GSP" in texts["false"], texts["false"]
    ok("/api/reclean：termfix 开则展开补全，关则原样")

    d = json.loads(opener.open(f"{API}/api/learn/pending", timeout=10).read())
    assert d["ok"] and isinstance(d["items"], list) and "stats" in d
    ok(f"/api/learn/pending 可读（当前可采纳 {d['stats']['pending']} 条）")

    # 参数校验：非法 key 必须 400，不能 500
    req = urllib.request.Request(f"{API}/api/learn/accept",
                                 data=json.dumps({"key": "没有箭头"}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        opener.open(req, timeout=10)
        raise AssertionError("应返回 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400, e.code
    ok("/api/learn/accept 非法 key → 400")


if __name__ == "__main__":
    termfix()
    learn()
    api()
    print(f"\n合计通过 {len(PASS)} 项")
