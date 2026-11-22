# -*- coding: utf-8 -*-
r"""v2.12-p8 专项：环境自检 + 模型清单的回归。

背景：换机 / 重装 / 清理缓存后，模型缺失的报错一直很含糊
（「未找到 gguf 模型（D:\AI\models\llm）」），只能靠人逐目录翻。
这里借鉴 AudioScript 的 model-catalog 做法：清单声明「必需文件 + 体积下限」，
自检逐项核对并给出一句话结论。

本脚本验证：
  ① assets/model-catalog.json 结构合法、id 唯一、路径安全；
  ② tools/selfcheck.py 在本机跑通、退出码 0、报告结构完整；
  ③ 自检只读：不改业务文件、不打印任何 API Key；
  ④ 自检.bat 是纯 ASCII + CRLF + 无 BOM；说明文件是 UTF-8 BOM；
  ⑤ 标点模型不再误套 Paraformer 的 am.mvn（上一版清单的真实错项）。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def main():
    cat_path = BASE / "assets/model-catalog.json"
    selfcheck = BASE / "tools/selfcheck.py"
    bat = BASE / "自检.bat"
    manual = BASE / "tools/自检与模型清单.txt"

    print("[模型清单结构]")
    ok("catalog 存在", cat_path.is_file())
    cat = json.loads(cat_path.read_text(encoding="utf-8-sig"))
    ok("version = 1", cat.get("version") == 1, cat.get("version"))
    items = cat.get("items") or []
    ok("至少 6 个条目", len(items) >= 6, len(items))
    ids = [it.get("id") for it in items]
    ok("id 唯一", len(ids) == len(set(ids)), ids)
    bad = []
    for it in items:
        p = str(it.get("path") or "")
        if not (p.startswith("D:/") or p.startswith("/")):
            bad.append((it.get("id"), "path 不是绝对路径"))
        if ".." in p.replace("\\", "/").split("/"):
            bad.append((it.get("id"), "path 含 .."))
        if not (it.get("files") or []):
            bad.append((it.get("id"), "没有 files"))
        for f in it.get("files") or []:
            if not f.get("path") or not isinstance(f.get("minBytes"), int):
                bad.append((it.get("id"), f"文件项不完整 {f}"))
    ok("每项路径安全、files 完整", not bad, bad[:4])
    required = [it for it in items if it.get("required")]
    ok("必需模型至少 4 个", len(required) >= 4, len(required))
    ok("find 兜底字段存在（缓存目录名会转义）",
       all(it.get("find") for it in required))
    punc = next((it for it in items if "punc" in str(it.get("id"))), None)
    ok("标点模型清单不含 am.mvn（上一版误报的根因）",
       punc is not None and not any(f["path"] == "am.mvn" for f in punc["files"]))

    print("[自检脚本本身]")
    src = selfcheck.read_text(encoding="utf-8")
    ok("用 find_spec 探测依赖，不 import 重包",
       "importlib.util.find_spec" in src and "\nimport torch" not in src
       and "\nimport funasr" not in src)
    ok("支持 --hash / --no-service / --quiet",
       all(k in src for k in ("--hash", "--no-service", "--quiet")))
    ok("报告原子写（先 .tmp 再 os.replace）",
       "os.replace(tmp, path)" in src)
    ok("检查「我们 / 咱们」硬规则", "「我们 / 咱们」" in src)
    ok("不打印 Key 明文", 'str(v or "").strip()' in src and "masked" not in src)

    print("[实跑：本机全绿]")
    before = {p: p.stat().st_mtime_ns for p in
              (BASE / "config/cloud_llm.json", BASE / "server.py",
               BASE / "web/index.html")}
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "report.json"
        p = subprocess.run(
            [sys.executable, str(selfcheck), "--quiet", "--no-service",
             "--json", str(out)],
            cwd=str(BASE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=180)
        blob = (p.stdout or "") + (p.stderr or "")
        ok("退出码 0", p.returncode == 0, f"rc={p.returncode} {blob[-300:]}")
        ok("报告已生成", out.is_file())
        rep = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
        ok("报告 ok=true", rep.get("ok") is True)
        ok("summary.fail = 0", (rep.get("summary") or {}).get("fail") == 0,
           rep.get("summary"))
        ok("models 数量与清单一致", len(rep.get("models") or []) == len(items),
           f'{len(rep.get("models") or [])} vs {len(items)}')
        st = {m["id"]: m.get("state") for m in rep.get("models") or []}
        ok("全部必需模型 state=ok",
           all(st.get(it["id"]) == "ok" for it in required),
           {k: v for k, v in st.items() if v != "ok"})
        ok("checks 里有词表与规则检查",
           any(r.get("section") == "词表" for r in rep.get("checks") or [])
           and any("我们 / 咱们" in r.get("name", "") for r in rep.get("checks") or []))
        text = out.read_text(encoding="utf-8")
        ok("报告不含 API Key", "sk-" not in text and "ak_" not in text)

    after = {p: p.stat().st_mtime_ns for p in before}
    ok("自检没有改动业务文件", before == after,
       [str(k) for k in before if before[k] != after[k]])

    print("[交付文件编码]")
    raw = bat.read_bytes()
    try:
        raw.decode("ascii")
        ascii_ok = True
    except UnicodeDecodeError:
        ascii_ok = False
    ok("自检.bat 是纯 ASCII", ascii_ok)
    ok("自检.bat 无 BOM", raw[:3] != b"\xef\xbb\xbf")
    ok("自检.bat 用 CRLF", b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""))
    mraw = manual.read_bytes()
    ok("说明文件 UTF-8 BOM", mraw[:3] == b"\xef\xbb\xbf")
    mtext = mraw.decode("utf-8-sig")
    ok("说明写了 --hash 与报告路径",
       "--hash" in mtext and "_selfcheck.json" in mtext)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
