# -*- coding: utf-8 -*-
"""配置与词库的备份 / 迁移。

只收「用户自己养出来的东西」：
    terms/corrections.txt     错词校正（带分区注释）
    terms/aliases.txt         简称膨胀表
    terms/hotwords.txt        识别热词
    rules/filler_words.json   口语词规则（含用户自己加的词）
    config/cloud_llm.json     云端模型设置（API Key 默认不含，需显式勾选）
    config/queue.json         夜间队列偏好（不含暂停 / 定时等运行态）

刻意**不备份** media/ 和 out/：那是几 GB 的音视频与字幕产物，用户重装后
要的是「配置立刻回来」，不是把整个工作区搬走。

导入前一定先把现有文件快照到 backups/import_YYYYmmdd_HHMMSS/，
导错包也能原样退回去。
"""
import hashlib
import io
import json
import os
import time
import uuid
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / "backups"

TEXT_ITEMS = [
    "terms/corrections.txt",
    "terms/aliases.txt",
    "terms/hotwords.txt",
]
JSON_ITEMS = [
    "rules/filler_words.json",
]
OPTIONAL_ITEMS = [
    "config/cloud_llm.json",     # 含 API Key，必须显式勾选
    "config/queue.json",         # 只回填偏好项
]
ALL_ITEMS = TEXT_ITEMS + JSON_ITEMS + OPTIONAL_ITEMS

# 队列里只迁移「怎么跑」，不迁移「现在停不停」：
# 重新装完机器一启动就是暂停状态，会让人以为工具坏了。
QUEUE_KEYS = ("auto_retry", "watch", "skip_done", "watch_interval")

MAX_ZIP = 12 * 1024 * 1024
MAX_ONE = 4 * 1024 * 1024
MAX_TOTAL = 24 * 1024 * 1024      # 【新增 v2.7】解压总量上限，防 zip 炸弹


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def export_zip(include_key: bool = False, include_queue: bool = True):
    """生成备份包，返回 (zip 字节, 建议文件名, manifest)。"""
    buf = io.BytesIO()
    manifest = {
        "app": "网课转字幕",
        "kind": "config-backup",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "contains_key": False,
        "items": [],
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in TEXT_ITEMS + JSON_ITEMS:
            p = BASE / rel
            if not p.exists():
                continue
            data = p.read_bytes()
            z.writestr(rel, data)
            manifest["items"].append(
                {"path": rel, "size": len(data), "sha256": _digest(data)})

        if include_key:
            import cloud_llm as CL
            cfg = CL.load_config()
            data = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
            z.writestr("config/cloud_llm.json", data)
            manifest["items"].append(
                {"path": "config/cloud_llm.json", "size": len(data),
                 "sha256": _digest(data)})
            # 只报「有没有存 Key」，不把 Key 本身写进 manifest
            manifest["contains_key"] = bool(cfg.get("keys") or cfg.get("api_key"))

        if include_queue:
            import queue_state as QS
            st = QS.load()
            slim = {k: st.get(k) for k in QUEUE_KEYS if k in st}
            data = json.dumps(slim, ensure_ascii=False, indent=2).encode("utf-8")
            z.writestr("config/queue.json", data)
            manifest["items"].append(
                {"path": "config/queue.json", "size": len(data),
                 "sha256": _digest(data)})

        z.writestr("manifest.json",
                   json.dumps(manifest, ensure_ascii=False, indent=2))

    name = f"网课转字幕-配置备份-{time.strftime('%Y%m%d-%H%M')}.zip"
    return buf.getvalue(), name, manifest


def import_zip(data: bytes) -> dict:
    """校验并导入备份包。任何一项不合法都整体拒绝，绝不写半个文件。"""
    if not data:
        raise ValueError("备份包是空的")
    if len(data) > MAX_ZIP:
        raise ValueError(f"备份包太大（{len(data) / 1048576:.1f} MB，上限 12 MB）")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("这不是有效的 zip 文件")

    names = set(z.namelist())
    if "manifest.json" not in names:
        raise ValueError("备份包里缺少 manifest.json，可能不是本工具导出的")
    try:
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
    except Exception:
        raise ValueError("manifest.json 已损坏，无法识别备份内容")
    if manifest.get("app") != "网课转字幕":
        raise ValueError("这不是「网课转字幕」的备份包")

    # 白名单：包里的其它路径（含 ../ 之类的穿越）一律忽略
    todo = sorted(n for n in names if n in ALL_ITEMS)
    if not todo:
        raise ValueError("备份包里没有可导入的配置或词表")

    payload = {}
    declared = {it.get("path"): it.get("sha256")
                for it in (manifest.get("items") or []) if isinstance(it, dict)}
    total_size = 0
    for rel in todo:
        info = z.getinfo(rel)
        if info.file_size > MAX_ONE:
            raise ValueError(f"{rel} 太大（{info.file_size / 1048576:.1f} MB）")
        total_size += info.file_size
        if total_size > MAX_TOTAL:
            raise ValueError("备份包解压后总量过大，已拒绝")
        raw = z.read(rel)
        if rel.endswith(".json"):
            try:
                json.loads(raw.decode("utf-8"))
            except Exception:
                raise ValueError(f"{rel} 不是合法 JSON")
        else:
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError(f"{rel} 不是 UTF-8 文本，可能是别的工具的文件")
        # 【新增 v2.7】校验 manifest 里的 sha256：下载不完整 / 被改过的包
        # 在写入前就拒绝，避免把半份词表导进去覆盖掉现有配置。
        want = declared.get(rel)
        if want and _digest(raw) != want:
            raise ValueError(f"{rel} 校验失败（sha256 不一致），备份包可能已损坏")
        payload[rel] = raw

    # 写前快照：导错了还能原样退回。同一秒内连续导入时目录加序号，避免互相覆盖。
    base = BACKUP_DIR / f"import_{time.strftime('%Y%m%d_%H%M%S')}"
    snap, i = base, 1
    while snap.exists():
        i += 1
        snap = base.with_name(f"{base.name}_{i}")
    snap.mkdir(parents=True, exist_ok=True)
    for rel in todo:
        p = BASE / rel
        if p.exists():
            (snap / rel.replace("/", "__")).write_bytes(p.read_bytes())

    written = []
    for rel, raw in payload.items():
        p = BASE / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_bytes(raw)
            os.replace(tmp, p)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        written.append(rel)

    # 队列只回填偏好项，且强制解除暂停 / 清掉定时，避免「导入后一动不动」
    if "config/queue.json" in payload:
        import queue_state as QS
        st = QS.load()
        slim = json.loads(payload["config/queue.json"].decode("utf-8"))
        for k in QUEUE_KEYS:
            if k in slim:
                st[k] = slim[k]
        st["paused"] = False
        st["start_at"] = None
        QS.save(st)

    # 词表缓存立即刷新，不用重启
    try:
        import glossary as G
        G.reload_caches()
    except Exception:
        pass

    return {"ok": True, "imported": written,
            "snapshot": str(snap),
            "contains_key": "config/cloud_llm.json" in payload,
            "note": "词表已即时生效；云端 Key 若一并导入，刷新页面即可使用"}


def info() -> dict:
    """前端展示用：哪些项存在、多大、上次导入快照。"""
    items = []
    for rel in ALL_ITEMS:
        p = BASE / rel
        items.append({"path": rel, "exists": p.exists(),
                      "size": p.stat().st_size if p.exists() else 0})
    last = ""
    if BACKUP_DIR.exists():
        dirs = sorted((d.name for d in BACKUP_DIR.iterdir() if d.is_dir()), reverse=True)
        last = dirs[0] if dirs else ""
    try:
        import cloud_llm as CL
        cfg = CL.load_config()
        keys = sorted((cfg.get("keys") or {}).keys())
    except Exception:
        keys = []
    return {"items": items, "last_snapshot": last,
            "key_presets": keys, "max_mb": MAX_ZIP // 1048576}
