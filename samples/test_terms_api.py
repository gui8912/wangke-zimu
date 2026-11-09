# -*- coding: utf-8 -*-
"""五类口语词 + 七组判别的接口端到端测试"""
import json
import shutil
from pathlib import Path

import requests

import apibase                    # 【新增 v2.9】端口可能不是 7870
BASE = apibase.base()

# 本机有代理环境变量，requests 走代理会丢 POST body，直接绕过
S = requests.Session()
S.trust_env = False
RULES = Path(__file__).resolve().parent.parent / "rules" / "filler_words.json"
BAK = RULES.with_suffix(".json.bak")


def get(path):
    return S.get(BASE + path, timeout=30).json()


def post(path, body):
    return S.post(BASE + path, json=body, timeout=60).json()


print("=== 1. GET /api/terms 结构 ===")
d = get("/api/terms")
groups = d.get("groups", {})
assert groups, "缺少 groups"
for name, spec in groups.items():
    print(f"  {name:6s} mode={spec.get('mode'):14s} words={len(spec.get('words', []))}"
          f" tail={len(spec.get('tail_only', []))}")
assert len(groups) == 5, f"应有 5 组，实际 {len(groups)}"
print("  guards:", "、".join(d.get("guards", {}).keys()))

print("\n=== 2. 保存时「嘛/哈」要分流回 tail_only ===")
shutil.copy(RULES, BAK)
try:
    saved = post("/api/terms", {
        "groups": {
            "语气词": ["呃", "嗯", "啊", "嘛", "哈", "呀"],   # 嘛/哈 混在 words 里提交
            "句首填充": ["这个", "那个", "就是说"],
            "句尾确认": ["对吧", "是吧"],
            "口头冗余": ["怎么说呢"],
            "组合填充": ["这个就是说呢"],
        },
        "tail_only": {"语气词": ["嘛", "哈"]},
    })
    g = saved["terms"]["groups"]["语气词"]
    print("  words     :", g["words"])
    print("  tail_only :", g["tail_only"])
    assert "嘛" not in g["words"] and "哈" not in g["words"], "嘛/哈 不应留在 words"
    assert set(g["tail_only"]) == {"嘛", "哈"}, "tail_only 应保留 嘛/哈"
    # 规则字段不能被前端写坏
    assert saved["terms"]["groups"]["句首填充"]["mode"] == "sentence_start"
    assert "conditional" in saved["terms"]["groups"]["口头冗余"], "conditional 规则被写丢了"
    print("  规则字段完整：mode / conditional 均保留")
finally:
    shutil.copy(BAK, RULES)
    BAK.unlink()
    print("  已恢复原词表")

print("\n=== 3. 重新清理并返回建议 ===")
cues = [
    {"start": 0, "end": 3, "text": "嗯，这个，各位同事啊，那么今天下午呢，我们主要是讲一下这个，就是药品零售企业。"},
    {"start": 3, "end": 6, "text": "我们的监管要求是这样的，对吧？"},
    {"start": 6, "end": 9, "text": "大概三十个工作日能办完，基本上就是这样。"},
    {"start": 9, "end": 12, "text": "这个文件要备案，那个条款要看，就是说很重要。"},
    {"start": 12, "end": 15, "text": "然后的话，还要看处方药和非处方药有没有分开。"},
]
r = S.post(BASE + "/api/reclean",
                  data={"file_id": "test", "cues_json": json.dumps(cues, ensure_ascii=False)},
                  timeout=90)
d = r.json()
assert r.status_code == 200, d
print(f"  有变化：{d['changed']} 句")
for c in d["cues"]:
    print(f"    [{c['start']:>2}-{c['end']:<2}] {c['text']}")
print(f"  待人工判断 {len(d.get('suggestions', []))} 条：")
for s in d.get("suggestions", []):
    print("    -", s)

assert len(d.get("suggestions", [])) >= 2, "应至少返回 2 条建议"
assert any("我局" in s for s in d["suggestions"]), "单位自称应给出「我局」建议"
assert any("回听" in s for s in d["suggestions"]), "含数字的约量应提示回听"
print("\n全部通过")
