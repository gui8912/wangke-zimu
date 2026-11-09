# -*- coding: utf-8 -*-
"""v2.7 专项回归：术语表扩充 / 字幕清理生效 / 4 路并行 / DeepSeek 不思考 / 审计修复。

全程使用临时目录与内存假对象，不改真实词表与配置。
运行：python samples/test_v27.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import clean_transcript as CT   # noqa: E402
import cloud_llm as CL          # noqa: E402
import glossary as G            # noqa: E402
import llm_polish as LP         # noqa: E402
import termfix as TF            # noqa: E402
import transcribe as T          # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}"
          f"{(' — ' + str(extra)) if extra and not cond else ''}")


# ────────────────────────── 术语表内容 ──────────────────────────

def terms_content():
    print("[术语表] 规模、去重、危险条目")
    lines = [ln.strip() for ln in
             (BASE / "terms" / "hotwords.txt").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    words = [ln.split("|")[0].strip() for ln in lines]
    # 【修改 v2.8】第二轮按 GSP/GMP 附录与规章补遗后总量到 450 左右；
    # 上限放到 520：再往上（>500）FunASR 解码会明显变慢，届时请分表。
    ok("热词 400–520 条", 400 <= len(words) <= 520, len(words))
    ok("热词无重复", len(words) == len(set(words)),
       [w for w in words if words.count(w) > 1][:4])
    for must in ("医疗器械生产质量管理规范检查指导原则",
                 "医疗器械生产质量管理规范无菌医疗器械检查指导原则",
                 "关键项目", "主要项目", "一般项目",
                 "化妆品生产质量管理规范", "首营企业", "药品追溯码"):
        ok(f"含「{must}」", must in words)

    corr = dict(CT.load_corrections(BASE / "terms" / "corrections.txt"))
    # 【修改 v2.8】判据改为「域内出现概率」：这几个错写只在非监管语境成立，
    # 已恢复启用（用户明确要求，见 corrections.txt 顶部说明）。
    for bad, good in (("影片", "饮片"), ("印片", "饮片"), ("驻藏", "贮藏"),
                      ("抽氧", "抽样"), ("去雾剂", "气雾剂")):
        ok(f"域内安全替换已启用：{bad}=>{good}", corr.get(bad) == good)
    # 这些在监管语料里仍有正常含义，继续保持停用
    for bad in ("备安", "处于药品", "泡制", "炮炙", "列要", "储备"):
        ok(f"仍有歧义的替换未启用：{bad}", bad not in corr)
    ok("管带→管代仍启用", corr.get("管带") == "管代")

    aliases = TF.load_aliases(BASE / "terms" / "aliases.txt")
    amap = dict(aliases)
    ok("管代→管理者代表", amap.get("管代") == "管理者代表")
    ok("UDI→医疗器械唯一标识", amap.get("UDI") == "医疗器械唯一标识")
    ok("长简称先于裸 GMP", "医疗器械GMP" in amap)

    changes, _ = TF.expand_aliases("这个医疗器械GMP的要求", aliases)
    ok("器械 GMP 不展开成药监 GMP", "医疗器械生产质量管理规范" in changes
       and "药品生产质量管理规范" not in changes, changes)


def subtitle_glossary():
    print("[字幕] 术语表参与字幕清理，且外部编辑即时生效")
    tmp = Path(tempfile.mkdtemp(prefix="v27g_"))
    orig_base = CT.BASE
    try:
        (tmp / "terms").mkdir(parents=True)
        (tmp / "terms" / "hotwords.txt").write_text(
            "药品经营质量管理规范\n", encoding="utf-8")
        (tmp / "terms" / "aliases.txt").write_text(
            "GSP=>药品经营质量管理规范\n管代=>管理者代表\n", encoding="utf-8")
        CT.BASE = tmp
        c = CT.Cleaner({"groups": {}, "guards": {}}, [])
        out, ch, _ = c.clean("依据药品经营质量规范，管代负责GSP。")
        ok("字幕句应用术语补全", "药品经营质量管理规范" in out, out)
        ok("字幕句应用简称膨胀", "管理者代表" in out, out)

        # 外部改词表后，不重建进程、下一次 Cleaner 就生效（mtime 缓存）
        (tmp / "terms" / "hotwords.txt").write_text(
            "药品经营质量管理规范\n药物警戒质量管理规范\n", encoding="utf-8")
        os.utime(tmp / "terms" / "hotwords.txt", None)
        c2 = CT.Cleaner({"groups": {}, "guards": {}}, [])
        out2, _, _ = c2.clean("依据药物警戒质量规范执行。")
        ok("外部编辑词表即时生效", "药物警戒质量管理规范" in out2, out2)
    finally:
        CT.BASE = orig_base
        CT.load_glossary(force=True)
        shutil.rmtree(tmp, ignore_errors=True)

    p = G.preview("依据药品经营质量规范，管代负责质量管理体系。")
    names = [s["name"] for s in p["steps"]]
    ok("试一句显示三步链路", "简称膨胀" in names and "术语补全" in names, names)
    ok("试一句给出改后文本", "药品经营质量管理规范" in p["after"], p["after"])


def glossary_bom(tmp: Path):
    print("[术语表] BOM / GBK 往返不破坏原文件")
    orig = G.KINDS
    try:
        for enc, bom in (("utf-8", True), ("gbk", False)):
            f = tmp / f"c_{enc}.txt"
            f.write_bytes((b"\xef\xbb\xbf" if bom else b"")
                          + "错词=>正确词\n".encode(enc))
            G.KINDS = {"corrections": {"path": f, "mode": "map", "label": "错词校正"}}
            items = G.parse_items(f, "map")
            ok(f"{enc} 解析首条", items and items[0]["from"] == "错词", items)
            G.apply_changes({"kind": "corrections",
                             "adds": [{"from": "管带", "to": "管代"}]})
            raw = f.read_bytes()
            ok(f"{enc} BOM 保持", raw.startswith(b"\xef\xbb\xbf") == bom)
            ok(f"{enc} 编码未变", raw.decode("utf-8-sig" if bom else enc)
               .strip().endswith("管带=>管代"))
    finally:
        G.KINDS = orig


# ────────────────────────── 识别与并行 ──────────────────────────

def transcribe_cases():
    print("[识别] 热词权重、切片、并行度")
    tmp = Path(tempfile.mkdtemp(prefix="v27t_"))
    try:
        p = tmp / "h.txt"
        p.write_text("管代|20\n错词|abc\n正常|88\n", encoding="utf-8")
        hw = T.load_hotwords(p)
        ok("坏权重按整行当词，不拼坏热词串", "错词|abc 20" in hw and "错词 20" not in hw, hw)
        ok("合法权重保留", "正常 88" in hw, hw)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    import numpy as np
    pcm = (np.sin(np.arange(16000 * 12) / 20.0) * 0.2).astype("float32")
    parts = T._split_pcm(pcm, 16000, 4.0, 0.5)
    ok("12s 按 4s 切片 ≥3 片", len(parts) >= 3, len(parts))
    offs = [o for _, o in parts]
    ok("偏移单调", all(offs[i] < offs[i + 1] for i in range(len(offs) - 1)), offs)
    covered = sum(len(c) for c, _ in parts)
    ok("不丢采样", covered == len(pcm), (covered, len(pcm)))
    ok("末片到结尾", abs((offs[-1] + len(parts[-1][0]) / 16000) - 12) < 0.05)

    ok("默认并行 1–4 路", 1 <= T.FUNASR_WORKERS <= 4, T.FUNASR_WORKERS)
    ok("默认等于自适应值",
       T.FUNASR_WORKERS == T._default_funasr_workers(), T.FUNASR_WORKERS)


# ────────────────────────── 润色 / 云端 ──────────────────────────

def llm_cases():
    print("[润色] 术语组、回显检测、块尾判断、块数估算")
    groups = LP.must_keep_terms("这个医疗器械GMP的要求，管代负责。")
    flat = [g for g in groups]
    ok("简称/全称作为一组", any("医疗器械GMP" in g and
                          "医疗器械生产质量管理规范" in g for g in flat), flat)
    ok("不误加药监 GMP", not any("药品生产质量管理规范" in g for g in flat), flat)

    ok("单说「技术约定」不算回显", not LP._looks_echoed("我们技术约定一下，这个条款怎么理解。"))
    ok("复述规则仍能识别",
       LP._looks_echoed("你是一名熟悉药品、医疗器械、化妆品监管工作的文字编辑。"))

    ok("块尾只差标点算已保留", LP._tail_present("……谢谢大家", "谢谢大家。"))
    ok("块尾真的删了要补回", not LP._tail_present("这是第98句的内容。", "这是第99句的内容。"))

    orig = CL.load_config
    try:
        CL.load_config = lambda: {"enabled": True}
        cloud = LP.estimate_chunks("字。" * 15000)
        CL.load_config = lambda: {"enabled": False}
        local = LP.estimate_chunks("字。" * 15000)
        ok("云端块数明显更少", cloud * 3 < local, (cloud, local))
    finally:
        CL.load_config = orig


def cloud_cases():
    print("[云端] DeepSeek 关闭思考 + 默认额度")
    cfg = CL.load_config()
    cfg.update(enabled=True, preset="deepseek-v4-flash",
               base_url="https://api.deepseek.com", model="deepseek-flash",
               api_key="sk-test", thinking="off", max_tokens=8192)
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "正常"},
                                            "finish_reason": "stop"}],
                               "usage": {}}).encode()

    class Op:
        def open(self, req, timeout=0):
            captured["body"] = json.loads(req.data.decode())
            return Resp()

    orig = CL._opener
    CL._opener = lambda: Op()
    try:
        CL.CloudChat(cfg).create_chat_completion(
            messages=[{"role": "user", "content": "hi"}], max_tokens=32)
        ok("请求体带 thinking=disabled",
           captured["body"].get("thinking") == {"type": "disabled"}, captured)
        cfg["thinking"] = "auto"
        captured.clear()
        CL.CloudChat(cfg).create_chat_completion(
            messages=[{"role": "user", "content": "hi"}], max_tokens=32)
        ok("auto 时不发 thinking", "thinking" not in captured["body"])
    finally:
        CL._opener = orig

    ok("默认关闭思考", CL.DEFAULTS["thinking"] == "off")
    ok("默认输出额度 8192", CL.DEFAULTS["max_tokens"] == 8192)
    ok("现行模型名", CL.PRESETS["deepseek-v4-flash"]["model"] == "deepseek-flash")


# ────────────────────────── 队列 / 备份 ──────────────────────────

def server_cases():
    print("[服务] 结果载荷回收、队列幽灵条目、重启落盘")
    import server as S

    orig_tasks = dict(S.TASKS)
    try:
        S.TASKS.clear()
        for i in range(25):
            S.TASKS[f"t{i}"] = {"status": "done", "result": {"cues": [i]}}
        S._prune_tasks(300)
        kept = sum(1 for v in S.TASKS.values() if v.get("result"))
        ok("只保留最近 20 份结果", kept == 20, kept)

        S.TASKS["cancel1"] = {"status": "cancelled", "result": None}
        S.JOB_Q.put("cancel1")
        ok("删除前在队列里", "cancel1" in S.JOB_Q.snapshot())
        S._remove_task("cancel1")
        ok("删除后队列无幽灵", "cancel1" not in S.JOB_Q.snapshot(),
           S.JOB_Q.snapshot())
    finally:
        S.TASKS.clear()
        S.TASKS.update(orig_tasks)

    tmp = Path(tempfile.mkdtemp(prefix="v27j_"))
    orig_jobs, orig_tasks = S.JOBS, dict(S.TASKS)
    try:
        S.JOBS = tmp
        (tmp / "run1.json").write_text(json.dumps(
            {"id": "run1", "status": "running", "logs": []}), encoding="utf-8")
        S.TASKS.clear()
        S._load_jobs()
        disk = json.loads((tmp / "run1.json").read_text(encoding="utf-8"))
        ok("running 重启后落盘为 interrupted", disk["status"] == "interrupted",
           disk.get("status"))
    finally:
        S.JOBS = orig_jobs
        S.TASKS.clear()
        S.TASKS.update(orig_tasks)
        shutil.rmtree(tmp, ignore_errors=True)


def backup_cases():
    print("[备份] 篡改包按 sha256 拒绝")
    import backup as BK
    tmp = Path(tempfile.mkdtemp(prefix="v27b_"))
    orig_base, orig_dir = BK.BASE, BK.BACKUP_DIR
    try:
        (tmp / "terms").mkdir(parents=True)
        (tmp / "rules").mkdir(parents=True)
        (tmp / "terms" / "corrections.txt").write_text("管带=>管代\n", encoding="utf-8")
        (tmp / "terms" / "aliases.txt").write_text("GSP=>药品经营质量管理规范\n",
                                                   encoding="utf-8")
        (tmp / "terms" / "hotwords.txt").write_text("药品管理法\n", encoding="utf-8")
        (tmp / "rules" / "filler_words.json").write_text("{}", encoding="utf-8")
        BK.BASE, BK.BACKUP_DIR = tmp, tmp / "backups"

        data, _, _ = BK.export_zip(False, False)
        src = zipfile.ZipFile(io.BytesIO(data))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in src.namelist():
                raw = src.read(name)
                if name == "terms/corrections.txt":
                    raw = "管带=>管道\n".encode("utf-8")   # 篡改内容，manifest 不变
                z.writestr(name, raw)
        try:
            BK.import_zip(buf.getvalue())
            ok("篡改包被拒绝", False)
        except ValueError as exc:
            ok("篡改包被拒绝", "sha256" in str(exc), str(exc))
    finally:
        BK.BASE, BK.BACKUP_DIR = orig_base, orig_dir
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="v27_"))
    try:
        terms_content()
        subtitle_glossary()
        glossary_bom(tmp)
        transcribe_cases()
        llm_cases()
        cloud_cases()
        server_cases()
        backup_cases()
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
