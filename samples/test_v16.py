# -*- coding: utf-8 -*-
"""v1.6 导出格式选项与清理明细专项回归。

分两段：
  unit() —— build_plain_text / norm_punct 的规则边界；
  api()  —— /api/export 的参数、/api/reclean 的 diffs，需要服务在 7870 运行。

运行：python samples/test_v16.py
"""
import json
import sys
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


CUES = [
    {"start": 0, "end": 2, "text": "依据《药品管理法》第一百二十条和第五条"},
    {"start": 2, "end": 4, "text": "第二款的有关规定，"},
    {"start": 4, "end": 7, "text": "二零二六年共检查三百五十家零售药店。"},
    {"start": 7, "end": 9, "text": "抽检一千二百三十四批次。"},
    {"start": 9, "end": 11, "text": "合格率达到98.5%，比去年提高"},
    {"start": 11, "end": 13, "text": "三个百分点。"},
]


def unit():
    import server as S

    print("\n[unit] 文本组装与标点规整")

    # 1. para：字幕按屏断行，讲稿要按句末收段 —— 第一条不含句号，应与第二条合并
    para = S.build_plain_text(CUES, mode="para", blank=True)
    assert para.split("\n\n")[0].endswith("零售药店。"), para
    assert "第二款的有关规定" in para.split("\n\n")[0], "断行处应合并"
    assert len(para.split("\n\n")) == 3, para
    ok("para：按句末收段，屏断行自动合并（3 段）")

    # 2. 空行开关
    no_blank = S.build_plain_text(CUES, mode="para", blank=False)
    assert "\n\n" not in no_blank and no_blank.count("\n") == 2, repr(no_blank)
    ok("blank=False：段间不空行")

    # 3. line：每句一行，且不受 blank 影响（否则「每句一行」变成句句空行）
    line = S.build_plain_text(CUES, mode="line", blank=True)
    assert "\n\n" not in line
    assert line.count("\n") == len(CUES) - 1, repr(line[:80])
    ok("line：每句一行，不受空行设置影响")

    # 4. raw：连成一段
    raw = S.build_plain_text(CUES, mode="raw")
    assert "\n" not in raw and raw.startswith("依据") and raw.endswith("。"), raw[-20:]
    ok("raw：连成一段")

    # 5. 标点全角化：只在中文字符后转，98.5% 的小数点绝不能动
    t = S.norm_punct("合格率达到98.5%,比去年提高,约3.14倍.", "full")
    assert "98.5%" in t, t
    assert "3.14" in t, t
    assert "，" in t, t
    ok("full：中文后半角转全角，98.5% / 3.14 小数不受影响")

    # 6. 半角化
    h = S.norm_punct("合格率达到98.5%，比去年提高。", "half")
    assert "," in h and "." in h and "98.5" in h, h
    ok("half：全角转半角，数字原样")

    # 7. keep 原样
    src = "达到98.5%,提高."
    assert S.norm_punct(src, "keep") == src
    assert S.norm_punct(src, "乱写") == src, "未知模式应原样返回"
    ok("keep / 未知模式：一个字都不改")

    # 8. 空输入不炸
    assert S.build_plain_text([]) == ""
    assert S.build_plain_text([{"text": ""}]) == ""
    ok("空输入安全")


def api():
    print("\n[api] /api/export 参数与 /api/reclean diffs")
    try:
        h = json.loads(opener.open(f"{API}/api/health", timeout=6).read())
    except Exception as exc:
        print("  SKIP 服务未运行:", exc)
        return
    ver = tuple(int(x) for x in str(h.get("version", "0")).split("."))
    assert ver >= (1, 6), h
    ok(f"/api/health v{h['version']}")

    def export(**kw):
        body = {"stem": "v16_probe", "cues": CUES, "kind": "txt", **kw}
        req = urllib.request.Request(f"{API}/api/export",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return opener.open(req, timeout=20).read().decode("utf-8")

    assert "\n\n" in export(mode="para", blank=True)
    assert "\n\n" not in export(mode="para", blank=False)
    assert "\n\n" not in export(mode="line")
    assert "\n" not in export(mode="raw")
    ok("/api/export：para/blank/line/raw 四种组合生效")

    # 非法参数必须回落，不能 500
    got = export(mode="乱写", blank="x", punct="乱写")
    assert got and isinstance(got, str)
    ok("/api/export：非法参数回落默认值，不报错")

    # 标点选项经 HTTP 生效
    assert "，" in export(mode="para", punct="full")
    ok("/api/export：punct=full 经接口生效")

    # reclean 返回逐条 diffs
    cues = json.dumps([{"idx": 1, "time": "00:00:01,000 --> 00:00:03,000",
                        "text": "嗯，依据药品经营质量规范的要求，货值金额五十万元。"}],
                      ensure_ascii=False)
    data = urllib.parse.urlencode({"file_id": "v16_probe", "cues_json": cues,
                                   "itn": "true", "termfix": "true"}).encode()
    r = json.loads(opener.open(urllib.request.Request(f"{API}/api/reclean", data=data),
                               timeout=20).read())
    assert r.get("diffs"), r
    kinds = " ".join(r["diffs"][0]["changes"])
    assert "语气词" in kinds and "术语补全" in kinds and "数字规整" in kinds, kinds
    ok("reclean：diffs 带逐条变更类型（语气词/术语补全/数字规整）")

    # 无改动的句子不应出现在 diffs 里
    clean_cues = json.dumps([{"idx": 1, "time": "00:00:01,000 --> 00:00:02,000",
                              "text": "坚持四不两直。"}], ensure_ascii=False)
    data2 = urllib.parse.urlencode({"file_id": "v16_probe",
                                    "cues_json": clean_cues}).encode()
    r2 = json.loads(opener.open(urllib.request.Request(f"{API}/api/reclean", data=data2),
                                timeout=20).read())
    assert r2["diffs"] == [], r2["diffs"]
    ok("reclean：无改动的句子不进 diffs")


if __name__ == "__main__":
    unit()
    api()
    print(f"\n合计通过 {len(PASS)} 项")
