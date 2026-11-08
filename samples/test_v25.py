# -*- coding: utf-8 -*-
"""v2.5 专项回归：术语表可视化编辑（错词 / 简称 / 热词）。

全部在临时目录里复制真实表结构后操作，**绝不写真实的 terms/**。
运行：python samples/test_v25.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import glossary as G  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")


def setup(tmp: Path, nl=b"\n"):
    d = tmp / "terms"
    d.mkdir(parents=True, exist_ok=True)
    for n in ("corrections.txt", "aliases.txt", "hotwords.txt"):
        raw = (BASE / "terms" / n).read_bytes()
        raw = raw.replace(b"\r\n", b"\n")
        if nl != b"\n":
            raw = raw.replace(b"\n", nl)
        (d / n).write_bytes(raw)
    G.KINDS = {
        "corrections": {"path": d / "corrections.txt", "mode": "map", "label": "错词校正"},
        "aliases": {"path": d / "aliases.txt", "mode": "map", "label": "简称表"},
        "hotwords": {"path": d / "hotwords.txt", "mode": "list", "label": "识别热词"},
    }
    return d


def read(p: Path) -> str:
    return p.read_bytes().decode("utf-8")


def parse_and_protect(tmp: Path):
    print("[解析] 三张表的条目与注释")
    d = setup(tmp)
    snap = G.snapshot()["files"]
    cor = snap["corrections"]["items"]
    ali = snap["aliases"]["items"]
    hot = snap["hotwords"]["items"]

    ok("错词表有条目", len(cor) > 20, len(cor))
    ok("错词表识别管带", any(x["from"] == "管带" and x["to"] == "管代" for x in cor))
    ok("注释不当条目", all(not x["from"].startswith("#") for x in cor))
    ok("简称表识别 GSP", any(x["from"] == "GSP" for x in ali))
    ok("注释掉的简称不启用", not any(x["from"] == "国家局" for x in ali),
       [x for x in ali if x["from"] == "国家局"])
    ok("热词表识别权重", any(x["word"] == "管代" and x["weight"] == 20 for x in hot))
    ok("热词无权重为 None", any(x["word"] == "管理者代表" and x["weight"] is None for x in hot))
    ok("热词不把注释当词", all(not x["word"].startswith("#") for x in hot))


def edit_flow(tmp: Path):
    print("[编辑] 新增 / 修改 / 删除，注释与换行原样保留")
    d = setup(tmp / "lf")
    p = d / "corrections.txt"
    before = read(p)
    n_before = len(before.splitlines())

    r = G.apply_changes({"kind": "corrections",
                         "adds": [{"from": "测试错词", "to": "测试正确词"}]})
    after = read(p)
    ok("新增计数", r["added"] == 1 and r["total"] >= 1, r)
    ok("新增写入文件", "测试错词=>测试正确词" in after)
    ok("新增后注释仍在", after.count("#") == before.count("#"))
    ok("新增保留 LF", b"\r\n" not in p.read_bytes())
    ok("新增保留末尾换行", after.endswith("\n"))

    # 修改一条已有条目（用当前行号）
    items = G.parse_items(p, "map")
    target = next(x for x in items if x["from"] == "测试错词")
    r2 = G.apply_changes({"kind": "corrections",
                          "updates": [{"line": target["line"],
                                       "from": "测试错词", "to": "测试正确词二"}]})
    mid = read(p)
    ok("修改生效", "测试错词=>测试正确词二" in mid, r2)
    ok("修改计数", r2["updated"] == 1, r2)
    ok("修改不增行", len(mid.splitlines()) == len(after.splitlines()))

    # 删除刚才那条
    items = G.parse_items(p, "map")
    target = next(x for x in items if x["from"] == "测试错词")
    r3 = G.apply_changes({"kind": "corrections", "deletes": [target["line"]]})
    end = read(p)
    ok("删除生效", "测试错词=>" not in end)
    ok("删除计数", r3["deleted"] == 1, r3)
    ok("删除只少一行", len(end.splitlines()) == len(mid.splitlines()) - 1)
    ok("删除不动注释", end.count("#") == before.count("#"))

    # 热词带权重新增
    hp = d / "hotwords.txt"
    hb = read(hp)
    G.apply_changes({"kind": "hotwords", "adds": [{"word": "不良事件", "weight": 25}]})
    ha = read(hp)
    ok("热词带权重写入", "不良事件|25" in ha)
    ok("热词注释保留", ha.count("#") == hb.count("#"))


def crlf_flow(tmp: Path):
    print("[换行] CRLF 文件保存后仍是 CRLF")
    d = setup(tmp / "crlf", nl=b"\r\n")
    p = d / "aliases.txt"
    G.apply_changes({"kind": "aliases", "adds": [{"from": "SOP", "to": "标准操作规程"}]})
    raw = p.read_bytes()
    ok("仍为 CRLF", b"\r\n" in raw and raw.count(b"\r\n") >= raw.count(b"\n") - 2)
    ok("未混入裸 LF", raw.replace(b"\r\n", b"").count(b"\n") == 0)
    ok("内容写入", "SOP=>标准操作规程".encode() in raw)


def guards(tmp: Path):
    print("[护栏] 非法输入一律拒绝")
    d = setup(tmp / "guard")
    p = d / "corrections.txt"
    before = read(p)

    def bad(payload, msg):
        try:
            G.apply_changes(payload)
        except ValueError as exc:
            ok(msg, True)
            return
        ok(msg, False, "未抛错")

    bad({"kind": "corrections", "adds": [{"from": "", "to": "x"}]}, "空错词被拒")
    bad({"kind": "corrections", "adds": [{"from": "x", "to": ""}]}, "空正确词被拒")
    bad({"kind": "corrections", "adds": [{"from": "a=>b", "to": "c"}]}, "错词含 => 被拒")
    bad({"kind": "corrections", "adds": [{"from": "同", "to": "同"}]}, "错词与正确词相同被拒")
    bad({"kind": "corrections", "adds": [{"from": "管带", "to": "另一个"}]}, "重复条目被拒")
    bad({"kind": "hotwords", "adds": [{"word": "测试", "weight": 999}]}, "权重越界被拒")
    bad({"kind": "hotwords", "adds": [{"word": "测试", "weight": "abc"}]}, "权重非数字被拒")
    bad({"kind": "../../etc/passwd", "adds": []}, "未知类型被拒")
    bad({"kind": "corrections", "updates": [{"line": 9999, "from": "a", "to": "b"}]},
        "失效行号被拒")
    ok("被拒后文件未改动", read(p) == before)


def frontend():
    print("[护栏] 前端接线")
    web = (BASE / "web" / "index.html").read_text(encoding="utf-8")
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    ok("顶栏有术语表按钮", 'id="btnGloss"' in web)
    ok("弹窗存在", 'id="glossMask"' in web)
    ok("三个页签", all(k in web for k in ('data-k="corrections"', 'data-k="aliases"',
                                          'data-k="hotwords"')))
    ok("支持新增/删除/保存", all(k in web for k in ("glossAdd", "del.textContent = '删除'",
                                                     "glossSave")))
    ok("服务端两个接口", '@app.get("/api/glossary")' in srv and
       '@app.post("/api/glossary")' in srv)
    ok("保存后刷新缓存", "GLOSS.reload_caches()" in srv)
    # 【新增 v2.5】误关润色弹窗后可重新打开
    ok("上次润色入口", 'id="btnLlmAgain"' in web)
    ok("可重新打开结果", "async function openLlmResult" in web)
    ok("关闭弹窗不清结果", "closeLlmPreview" in web and "结果仍保存在服务端" in web)
    ok("半小时内自动弹回", "age > 1800" in web)
    ok("入口有高亮态", "#btnLlmAgain.has" in web)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="v25_"))
    try:
        parse_and_protect(tmp)
        edit_flow(tmp)
        crlf_flow(tmp)
        guards(tmp)
        frontend()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, c in PASS if not c]
    print(f"\n{len(PASS) - len(bad)}/{len(PASS)} 通过")
    if bad:
        print("失败：" + "、".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
