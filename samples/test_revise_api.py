# -*- coding: utf-8 -*-
"""七遍改稿 API 端到端测试"""
import json
import urllib.parse
import urllib.request

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
import apibase                    # 【新增 v2.9】端口可能不是 7870
BASE = apibase.base()

SAMPLE = "\n".join([
    "呃，各位同事啊，大家好。",
    "那么今天下午呢，我们主要是讲一下这个，就是药品零售企业，那个，日常检查的一些要点。",
    "首先呢，第一个，就是证照是不是齐全，对吧？",
    "就是说我们到现场，第一件事就是要看证照，就是说第一件事就是看证照。",
    "然后呢，有个很重要的一条，就是执业药师是不是在岗。",
    "然后这个时候，我们还要看药品陈列，那个，处方药和非处方药有没有分开。",
    "由于企业没有按规定储存，造成的药品质量问题，这个要重点看。",
    "那个地方要特别注意。",
    "我们高度重视这项工作，稳步推进日常检查。",
    "根据《药品管理法》第一百一十六条规定，2025年3月15日起施行，企业应当在30个工作日内整改。",
    "基本上就是这些，发现问题呢，我们就要依法依规地进行处理，对吧？",
])


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    r = OP.open(req, timeout=60)
    return json.loads(r.read().decode())


def get(path):
    return json.loads(OP.open(BASE + path, timeout=30).read().decode())


cur = SAMPLE
print(f"原稿 {len(SAMPLE)} 字符\n")
for n in range(1, 8):
    d = post("/api/revise", {"pass": n, "text": cur, "opts": {}})
    assert "error" not in d, d
    cur = d["text"]
    post("/api/revise/save", {"stem": "接口测试", "pass": n, "text": cur})
    extra = ""
    if d.get("issues"):
        extra = f"，{len(d['issues'])} 处待确认：" + "、".join(
            sorted({i["title"] for i in d["issues"]}))
    if d["notes"].get("items"):
        extra = f"，事实核对 {d['notes']['count']} 项"
    print(f"P{n} {d['name']:<6} {d['stats']['chars']:>4} 字 段{d['stats']['paras']}{extra}")

post("/api/revise/save", {"stem": "接口测试", "pass": 0, "text": SAMPLE})
vs = get("/api/revise/versions?stem=" + urllib.parse.quote("接口测试"))["versions"]
print(f"\n版本文件：v0–v{max(int(k) for k in vs)}，共 {len(vs)} 个")
print("v0 是否原稿：", vs.get("0") == SAMPLE)

for fmt in ("md", "docx"):
    req = urllib.request.Request(
        BASE + "/api/revise/export",
        data=json.dumps({"fmt": fmt, "stem": "接口测试", "text": cur}).encode(),
        headers={"Content-Type": "application/json"})
    r = OP.open(req, timeout=30)
    body = r.read()
    print(f"导出 {fmt}: {len(body)} 字节  {r.headers.get('Content-Disposition')}")

d = post("/api/revise/tts", {"text": cur[:200]})
print("朗读：", d.get("url") or d.get("error"))
if d.get("url"):
    r = OP.open(BASE + d["url"], timeout=30)
    print("  音频字节数：", len(r.read()), r.headers.get("Content-Type"))
