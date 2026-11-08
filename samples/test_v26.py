# -*- coding: utf-8 -*-
"""v2.6 专项回归：配置与词库的备份 / 迁移。

全程在临时目录里模拟一套工作区，不读写真实 terms / config / backups。
运行：python samples/test_v26.py
"""
import hashlib
import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import backup as BK  # noqa: E402
import queue_state as QS  # noqa: E402
import cloud_llm as CL  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")


def make_ws(tmp: Path):
    """搭一个最小工作区：三张词表 + 口语词规则 + config。"""
    (tmp / "terms").mkdir(parents=True)
    (tmp / "rules").mkdir(parents=True)
    (tmp / "config").mkdir(parents=True)
    (tmp / "terms" / "corrections.txt").write_text(
        "# 注释保留\n管带=>管代\n", encoding="utf-8")
    (tmp / "terms" / "aliases.txt").write_text(
        "# 简称\nGSP=>药品经营质量管理规范\n", encoding="utf-8")
    (tmp / "terms" / "hotwords.txt").write_text(
        "# 热词\n管代|20\n", encoding="utf-8")
    (tmp / "rules" / "filler_words.json").write_text(
        json.dumps({"version": 1, "groups": {"filler": {"words": ["嗯"]}}},
                   ensure_ascii=False), encoding="utf-8")
    (tmp / "config" / "cloud_llm.json").write_text(
        json.dumps({"enabled": True, "preset": "deepseek-v4-flash",
                    "model": "deepseek-v4-flash",
                    "keys": {"deepseek-v4-flash": "sk-test-123456"}},
                   ensure_ascii=False), encoding="utf-8")
    (tmp / "config" / "queue.json").write_text(
        json.dumps({"paused": True, "start_at": 1234567890.0,
                    "auto_retry": 2, "watch": True, "skip_done": True},
                   ensure_ascii=False), encoding="utf-8")
    return tmp


def export_cases(tmp: Path):
    print("[导出] 包内容与清单")
    data, name, man = BK.export_zip(include_key=False, include_queue=False)
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    ok("含 manifest", "manifest.json" in names)
    ok("含三张词表", all(p in names for p in
                        ("terms/corrections.txt", "terms/aliases.txt",
                         "terms/hotwords.txt")))
    ok("含口语词规则", "rules/filler_words.json" in names)
    ok("默认不含 Key", "config/cloud_llm.json" not in names)
    ok("默认不含队列", "config/queue.json" not in names)
    ok("文件名带日期", ".zip" in name and "备份" in name, name)
    ok("manifest 标记不含 Key", man["contains_key"] is False)
    raw = z.read("terms/corrections.txt")
    entry = next(x for x in man["items"] if x["path"] == "terms/corrections.txt")
    ok("manifest 记录大小", entry["size"] == len(raw))
    ok("manifest 记录 sha256", entry["sha256"] == hashlib.sha256(raw).hexdigest())

    data2, _, man2 = BK.export_zip(include_key=True, include_queue=True)
    z2 = zipfile.ZipFile(io.BytesIO(data2))
    n2 = set(z2.namelist())
    ok("可选含 Key", "config/cloud_llm.json" in n2)
    ok("可选含队列", "config/queue.json" in n2)
    ok("manifest 标记含 Key", man2["contains_key"] is True)
    ok("manifest 不泄露 Key", "sk-test-123456" not in json.dumps(man2))


def import_cases(tmp: Path):
    print("[导入] 往返、快照与队列复位")
    data, _, _ = BK.export_zip(include_key=True, include_queue=True)

    # 先改坏现有文件，导入后应恢复
    (tmp / "terms" / "corrections.txt").write_text("被改坏了\n", encoding="utf-8")
    r = BK.import_zip(data)
    ok("导入成功", r.get("ok") and len(r["imported"]) >= 4, r)
    back = (tmp / "terms" / "corrections.txt").read_text(encoding="utf-8")
    ok("词表已恢复", "管带=>管代" in back)
    ok("注释也恢复", "# 注释保留" in back)
    ok("标记含 Key", r.get("contains_key") is True)
    snap = Path(r["snapshot"])
    ok("导入前快照存在", snap.exists() and any(snap.iterdir()), snap)
    ok("快照保住被改坏版本", "被改坏了" in
       (snap / "terms__corrections.txt").read_text(encoding="utf-8"))

    # 队列：偏好回填，但 paused / start_at 必须被清掉
    st = QS.load()
    ok("队列偏好导入", st.get("auto_retry") == 2 and st.get("watch") is True, st)
    ok("队列强制解除暂停", st.get("paused") is False, st)
    ok("队列清掉定时", st.get("start_at") is None, st)


def guard_cases(tmp: Path):
    print("[护栏] 坏包、越权路径、损坏 JSON 一律拒绝")
    before = (tmp / "terms" / "corrections.txt").read_bytes()

    def bad(data, msg):
        try:
            BK.import_zip(data)
        except ValueError:
            ok(msg, True)
            return
        ok(msg, False, "未拒绝")

    bad(b"not a zip", "非 zip 被拒")

    b1 = io.BytesIO()
    with zipfile.ZipFile(b1, "w") as z:
        z.writestr("terms/corrections.txt", "x")
    bad(b1.getvalue(), "缺 manifest 被拒")

    b2 = io.BytesIO()
    with zipfile.ZipFile(b2, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "别的工具"}))
        z.writestr("terms/corrections.txt", "x")
    bad(b2.getvalue(), "非本工具备份被拒")

    b3 = io.BytesIO()
    with zipfile.ZipFile(b3, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "网课转字幕"}))
        z.writestr("rules/filler_words.json", "{坏 JSON")
    bad(b3.getvalue(), "损坏 JSON 被拒")

    b4 = io.BytesIO()
    with zipfile.ZipFile(b4, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "网课转字幕"}))
        z.writestr("../../evil.txt", "逃逸")
    bad(b4.getvalue(), "只有越权路径时被拒")

    # 混合包：白名单文件导入，越权路径必须被忽略且不落盘
    b5 = io.BytesIO()
    with zipfile.ZipFile(b5, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "网课转字幕"}))
        z.writestr("terms/aliases.txt", "# 新简称\nGSP=>药品经营质量管理规范\n")
        z.writestr("../../evil.txt", "逃逸")
    r = BK.import_zip(b5.getvalue())
    ok("越权路径被忽略", not (tmp.parent / "evil.txt").exists()
       and not Path("evil.txt").exists())
    ok("合法文件照常导入", "terms/aliases.txt" in r["imported"])

    ok("失败时不动现有文件", before ==
       (tmp / "terms" / "corrections.txt").read_bytes())


def frontend():
    print("[护栏] 前端与接口接线")
    web = (BASE / "web" / "index.html").read_text(encoding="utf-8")
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    gi = (BASE / ".gitignore").read_text(encoding="utf-8")
    ok("顶栏有备份按钮", 'id="btnBackup"' in web)
    ok("弹窗有导出/导入", 'id="backupExport"' in web and 'id="backupImport"' in web)
    ok("可勾选包含 Key", 'id="backupKey"' in web)
    ok("三个接口", all(p in srv for p in
                     ('/api/backup/info', '/api/backup/export', '/api/backup/import')))
    ok("备份目录不入库", "/backups/" in gi)
    ok("导出用 RFC5987 文件名", "filename*=UTF-8''" in srv)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="v26_"))
    orig_base, orig_dir = BK.BASE, BK.BACKUP_DIR
    orig_q = QS.CONFIG
    orig_c = CL.CONFIG
    try:
        make_ws(tmp)
        BK.BASE = tmp
        BK.BACKUP_DIR = tmp / "backups"
        QS.CONFIG = tmp / "config" / "queue.json"
        CL.CONFIG = tmp / "config" / "cloud_llm.json"
        export_cases(tmp)
        import_cases(tmp)
        guard_cases(tmp)
        frontend()
    finally:
        BK.BASE, BK.BACKUP_DIR = orig_base, orig_dir
        QS.CONFIG = orig_q
        CL.CONFIG = orig_c
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, c in PASS if not c]
    print(f"\n{len(PASS) - len(bad)}/{len(PASS)} 通过")
    if bad:
        print("失败：" + "、".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
