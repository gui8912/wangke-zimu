#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
药品监管网课转字幕 · 后台服务（FastAPI）
========================================
提供：上传、转写（后台任务）、媒体流、口语词维护、重新清理、保存校对稿。

启动：python server.py      → http://127.0.0.1:7870
"""

import json
import math                     # modified(v1.2): 时间码合法性校验需要 isfinite
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
MEDIA = BASE / "media"
OUT = BASE / "out"
WEB = BASE / "web"
RULES = BASE / "rules" / "filler_words.json"
JOBS = OUT / "_jobs"
PEAKS = OUT / "_peaks"

for d in (MEDIA, OUT, WEB, JOBS, PEAKS):
    d.mkdir(parents=True, exist_ok=True)

# 【新增 v1.9】原生层崩溃（段错误 / abort）在 Python 侧完全无声：进程直接消失，
# 任务状态永远停在 running，日志也断在半句。开启 faulthandler 后，崩溃瞬间会把
# 各线程的 Python 栈追加写到 out/_crash.log，下次出事能直接看出卡在哪一步。
try:
    import faulthandler
    _CRASH_LOG = open(OUT / "_crash.log", "a", encoding="utf-8", buffering=1)
    faulthandler.enable(file=_CRASH_LOG, all_threads=True)
except Exception as _exc:      # 打不开就静默降级，不能因为诊断功能拖垮启动
    print(f"[crash] faulthandler 未启用：{_exc}", flush=True)

import transcribe as T          # noqa: E402
import clean_transcript as CT   # noqa: E402
import revise as RV             # noqa: E402
import align as AL              # noqa: E402
import asr_providers as P       # noqa: E402  【新增 v1.3】识别源抽象层
import learn as _LEARN          # noqa: E402  【新增 v1.5】修订学习（只记录，不自动应用）
import llm_polish as LP        # noqa: E402  【新增 v1.7】讲稿层 LLM 顺滑

REV = OUT / "rev"

# 串行队列：本机只有一颗 CPU，多任务并行只会互相拖慢。
JOB_Q = queue.Queue()
PERSIST_LOCK = threading.Lock()

app = FastAPI(title="药品监管网课转字幕")

# 任务表：task_id -> {status, logs, result, error}
TASKS = {}
UPLOADS = {}   # file_id -> {name, path, media_type}

MAX_UPLOADS = 200      # modified: 上限，避免长时间运行后字典无界增长
MAX_TASKS = 300        # modified

# modified(v1.2): 上传/解码硬边界。参考 faster-whisper 异步转写实践的常见坑：
# 损坏音频会让解码器无限期挂住，未校验大小会把同步盘撑爆。
MAX_UPLOAD_BYTES = int(os.environ.get("ASR_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
PEAKS_TIMEOUT = int(os.environ.get("ASR_PEAKS_TIMEOUT", "300"))
ALLOWED_MEDIA_EXT = set(T.MEDIA_EXT)
DOWNLOAD_EXT = {".srt", ".vtt", ".txt", ".md", ".json", ".mp3", ".wav"}


# ─────────────────────── 工具函数 ───────────────────────

def _inside(p: Path, root: Path) -> bool:
    """判断 p 是否真的在 root 目录之内。

    modified: 原来用 str(p).startswith(str(OUT))，
    'out' 与 'output_evil' 前缀相同会被误放行，属于路径穿越漏洞。
    """
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _safe(name: str) -> str:
    """文件名安全化：去掉路径分隔符与非法字符"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(name)).strip("_") or "draft"


def _prune(d: dict, limit: int):
    """超出上限时丢掉最早的条目（dict 保插入序）。

    modified: 新增。任务与上传记录原来只增不减，是内存泄漏。
    磁盘上的媒体文件不动——用户可能还在播放。
    """
    while len(d) > limit:
        d.pop(next(iter(d)), None)


def _atomic_write(p: Path, text: str):
    """modified(v1.2): 文本原子写——同目录临时文件 + os.replace。

    来源场景：Subtitle Edit / mp3-to-text 等本地转写工具对词表、字幕草稿
    普遍采用「临时文件再替换」；用途：避免自动保存或规则编辑写到一半时
    进程退出，留下半截 JSON / SRT 把下次启动直接带崩。

    【修改 v1.8】临时文件名加随机后缀。原来固定叫 `<名字>.tmp`，
    两个并发保存同一目标（自动保存 1.2s 一次 + 手动点保存）会互相覆盖
    临时文件，后一个 os.replace 时源文件已被前一个搬走 → FileNotFoundError。
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        # 【新增 v1.9】Windows 上 os.replace 偶尔会撞「目标正被占用」（杀软扫描、
        # 索引服务瞬时打开文件），表现为 PermissionError。瞬时冲突重试两次即可，
        # 别把一次偶发当成保存失败丢给用户——实测高负载下并发压测能撞到。
        for attempt in range(3):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
    except OSError:
        tmp.unlink(missing_ok=True)     # 失败别在 out/ 里堆垃圾
        raise


def _normalize_cues(cues) -> list:
    """modified(v1.2): 统一校验前端传来的字幕数组。

    原 /api/save、/api/export 直接取 c['start'] / c['end']，客户端只要传
    字符串、缺字段或 NaN，就会在 cues_to_srt 里抛 TypeError/KeyError → HTTP 500
    （已实测复现）。这里统一拦成 400，顺带把倒挂时间轴与零时长修正掉。
    """
    if not isinstance(cues, list) or not cues:
        raise ValueError("没有内容")
    out = []
    for i, c in enumerate(cues):
        if not isinstance(c, dict):
            raise ValueError(f"第 {i + 1} 条字幕不是对象")
        try:
            st, et = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"第 {i + 1} 条字幕时间码缺失或无效")
        if not (math.isfinite(st) and math.isfinite(et)) or st < 0 or et < 0:
            raise ValueError(f"第 {i + 1} 条字幕时间码非法")
        if et < st:                          # 倒挂：交换，不粗暴拒绝
            st, et = et, st
        if et - st < 0.001:                  # 零时长会让播放器/体检除零
            et = st + 0.001
        out.append({**c, "start": round(st, 3), "end": round(et, 3),
                    "text": str(c.get("text") or "")})
    return out


def srt_ts_to_sec(ts: str) -> float:
    """'00:01:02,300' → 62.3"""
    h, m, rest = ts.strip().split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_cues(srt_text: str) -> list:
    """SRT → [{index, start, end, text}]，时间统一成秒"""
    out = []
    for c in CT.parse_srt(srt_text):
        tm = c["time"].split("-->")
        if len(tm) != 2:
            continue
        try:
            st, et = srt_ts_to_sec(tm[0]), srt_ts_to_sec(tm[1])
        except Exception:
            continue
        out.append({
            "index": c["idx"],
            "start": round(st, 3),
            "end": round(et, 3),
            "text": " ".join(c["lines"]),
        })
    return out


# 【新增 v1.6】导出纯文本：分段方式 + 段间空行 + 标点风格
_CJK = re.compile(r"[\u4e00-\u9fa5]")
_FULL_MAP = {",": "，", ";": "；", ":": "：", "?": "？", "!": "！"}
_HALF_MAP = {"，": ",", "；": ";", "：": ":", "？": "?", "！": "!", "。": "."}
_SENT_END = ("。", "！", "？", "…", "!", "?", ".")


def norm_punct(text: str, mode: str = "keep") -> str:
    """标点风格规整。

    keep —— 原样（默认；识别结果本身就是全角，通常不需要动）
    full —— 半角转全角，但**只在中文字符后面转**：
            "98.5%" 的小数点、英文缩写里的点都不会被动到
    half —— 全角转半角（用于要贴进英文排版或代码块的场景）

    这里刻意不用无脑 replace：`98.5%`、`A.B`、`3.14` 被改成全角句号是不可逆的破坏。
    """
    if not text or mode == "keep":
        return text
    out = []
    for i, ch in enumerate(text):
        if mode == "full":
            if ch in _FULL_MAP:
                prev = text[i - 1] if i else ""
                if _CJK.match(prev or ""):
                    out.append(_FULL_MAP[ch])
                    continue
            if ch == ".":
                # modified(v1.8):原来只排除了「数字.数字」，英文缩写照样被改坏：
                # GMP.GSP → GMP。GSP、No.5 → No。5、e.g. → e。g。（已实测）。
                # 文档一直承诺"英文缩写里的点不会被动到"，实现却相反。
                # 现在改成「前一个字符是中文、且后面不是数字」才转句号：
                # 中文语境里的句点该转（第一章.总则），英文与数字里的一律不动。
                prev = text[i - 1] if i else ""
                nxt = text[i + 1] if i + 1 < len(text) else ""
                if _CJK.match(prev or "") and not nxt.isdigit():
                    out.append("。")
                    continue
        elif mode == "half":
            if ch in _HALF_MAP:
                out.append(_HALF_MAP[ch])
                continue
        out.append(ch)
    return "".join(out)


def build_plain_text(cues: list, mode: str = "para", blank: bool = True,
                     punct: str = "keep") -> str:
    """把 cues 组装成纯文本。

    mode:
      line —— 每条字幕一行（原来 TXT 的行为，适合逐句核对）
      para —— 按句末标点合成段落（**默认**，适合当讲稿读）
      raw  —— 全部连成一段，不断行
    blank: 段落之间是否空一行（Markdown 必须空行才是段落，TXT 可选）
    """
    # modified(v1.8) 新增:非数组直接报错。原来传字符串会在 c.get 处抛
    # AttributeError（'str' object has no attribute 'get'），是 500 而不是 400。
    if not isinstance(cues, list):
        raise ValueError("cues 必须是数组")
    texts = [str(c.get("text") or "").strip() for c in cues if isinstance(c, dict)]
    texts = [t for t in texts if t]

    if mode == "line":
        paras = texts
    elif mode == "raw":
        paras = ["".join(texts)] if texts else []
    else:                                   # para
        paras, cur = [], []
        for t in texts:
            cur.append(t)
            # 字幕本身是按屏断行的，一句可能拆成两三条；
            # 只有遇到句末标点才收段，拼出来才是完整句子
            if t.endswith(_SENT_END):
                paras.append("".join(cur))
                cur = []
        if cur:
            paras.append("".join(cur))

    paras = [norm_punct(p, punct) for p in paras]
    # 「每句一行」与「连成一段」本来就没有段落概念，blank 只对 para 生效
    sep = ("\n\n" if blank else "\n") if mode == "para" else "\n"
    return sep.join(paras)


def cues_to_srt(cues: list) -> str:
    def fmt(t):
        t = max(0.0, float(t))
        h = int(t // 3600)
        m = int(t % 3600 // 60)
        s = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        if ms == 1000:                 # modified: 四舍五入可能到 1000，统一用 divmod 进位
            s, ms = s + 1, 0
        if s == 60:
            m, s = m + 1, 0
        if m == 60:
            h, m = h + 1, 0
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, c in enumerate(cues, 1):
        lines.append(f"{i}\n{fmt(c['start'])} --> {fmt(c['end'])}\n{c['text']}\n")
    return "\n".join(lines)


def cues_to_vtt(cues: list) -> str:
    """WebVTT：网页 <track> 与多数视频平台通用，时间码用「.」分隔毫秒。

    modified: 新增。调研同类字幕工具（Subtitle Edit / Buzz / whisper-webui）
    都把 SRT↔VTT 互转作为标配；用户要发 B 站或嵌网页时 VTT 更直接，实现成本极低。
    """
    def fmt(t):
        t = max(0.0, float(t))
        h = int(t // 3600)
        m = int(t % 3600 // 60)
        s = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        if ms == 1000:
            s, ms = s + 1, 0
        if s == 60:
            m, s = m + 1, 0
        if m == 60:
            h, m = h + 1, 0
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    out = ["WEBVTT", ""]
    for c in cues:
        out.append(f"{fmt(c['start'])} --> {fmt(c['end'])}")
        out.append(str(c.get("text") or ""))
        out.append("")
    return "\n".join(out)


def media_type_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv"}:
        return "video"
    return "audio"


# ─────────────────────── 任务：持久化 + 串行队列 ───────────────────────

def _job_path(tid: str) -> Path:
    return JOBS / f"{tid}.json"


def _persist(tid: str):
    """把任务状态原子写盘；result 不落盘（cues 可能很大），重启后按文件重建。"""
    task = TASKS.get(tid)
    if not task:
        return
    data = {k: v for k, v in task.items() if k != "result"}
    data["logs"] = (task.get("logs") or [])[-120:]
    p = _job_path(tid)
    # modified(v1.9): 临时名加随机后缀。原来固定 <id>.tmp，同时跑起两个实例
    # （桌面启动器 + 命令行）时两边会互相覆盖，后写入的那个 os.replace 直接抛错。
    tmp = p.with_name(f"{p.stem}.{uuid.uuid4().hex[:8]}.tmp")
    with PERSIST_LOCK:
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass


def _load_jobs():
    """服务启动时接回历史任务；中断的标 interrupted，等用户点重试。"""
    # modified(v1.2): 只加载最近 MAX_TASKS 个。原来全量读回，磁盘 job 文件
    # 却从不清，_prune_tasks 的内存上限在重启后立刻失效（越积越多）。
    # 磁盘文件仍保留作归档，不删用户数据。

    # modified(v1.8):两处健壮性：
    # ① stat 放在 try 里——文件可能在 glob 与排序之间被删（清理脚本/同步盘），
    #    原来会抛 FileNotFoundError 直接让服务起不来；
    # ② JSON 顶层不是对象（数组/字符串/数字）时 d.get 会 AttributeError，
    #    原来这句在 try 外面，同样会带崩启动。
    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    try:
        candidates = list(JOBS.glob("*.json"))
    except OSError as exc:
        print(f"[jobs] 无法枚举任务目录：{exc}", flush=True)
        return
    files = sorted(candidates, key=_mtime, reverse=True)[:MAX_TASKS]

    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("任务文件顶层不是 JSON 对象")
            if not d.get("id"):
                d["id"] = p.stem
            tid = d["id"]
        except Exception as exc:
            print(f"[jobs] 跳过损坏的任务文件 {p.name}: {exc}", flush=True)
            continue
        d["result"] = None
        stale = d.get("status") in ("queued", "running")
        if stale:
            d["status"] = "interrupted"
            d["error"] = "服务重启，任务中断；可点「重试」重新排队"
        TASKS[tid] = d
        # modified(v1.9): 状态改了要立刻落盘。原来只在内存里改，磁盘上还写着
        # running——进程崩过之后打开任务文件看到的是假的「正在运行」，排查时误导人。
        if stale:
            _persist(tid)


def _locate(p, *dirs):
    """modified: 以「当前工程目录」为准重定位文件。

    工程目录搬迁后，job.json 里存的绝对路径可能指向旧副本（旧会话目录还在时尤其明显）。
    这里优先在当前工程目录下找同名文件，找到就用它；找不到才退回原路径。
    这样旧副本残留不会造成「读旧、写新」的双份漂移，旧副本删掉也不影响。
    """
    if not p:
        return None
    q = Path(p)
    for d in dirs:
        cand = d / q.name
        if cand.exists():
            return cand
    # modified(v1.2): 回落路径必须仍在当前工程内，否则迁移前的旧副本
    # 会被当成有效源文件，造成「读旧、写新」的双份漂移。
    return q if q.exists() and _inside(q, BASE) else None


def _rehydrate(task: dict):
    """从磁盘产物重建 result，让重启后的「已完成」任务还能打开。"""
    if task.get("result") or task.get("status") != "done":
        return
    # modified: 先按文件名重定位产物，迁移后历史任务仍可打开
    files = {k: str(_locate(v, OUT) or v)
             for k, v in (task.get("files") or {}).items() if v}
    task["files"] = files
    raw = files.get("raw_srt")
    if not raw or not Path(raw).exists():
        return
    try:
        raw_cues = parse_cues(Path(raw).read_text(encoding="utf-8"))
        clean = files.get("clean_srt")
        clean_cues = (parse_cues(Path(clean).read_text(encoding="utf-8"))
                      if clean and Path(clean).exists() else None)

        def read(p):
            return Path(p).read_text(encoding="utf-8") if p and Path(p).exists() else ""

        stem = task.get("stem") or Path(raw).stem.replace(".raw", "")
        # 【新增 v1.3】历史任务也能恢复双轨整理稿；没有 tracks 文件时按本地源处理，
        # 老任务（v1.2 及以前）不会因此打不开。
        script_text, script_source, provider_id = "", "", task.get("provider") or "local-qwen3"
        tp = OUT / f"{stem}.tracks.json"
        if tp.exists():
            try:
                tr = P.Tracks.from_dict(json.loads(tp.read_text(encoding="utf-8")))
                script_text, script_source = tr.script_text, tr.script_source
                provider_id = tr.provider or provider_id
            except Exception:
                pass
        task["result"] = {
            "stem": stem,
            "cues": raw_cues,
            "clean_cues": clean_cues,
            "doc_text": read(files.get("raw_txt")),
            "clean_doc_text": read(files.get("clean_txt")),
            "report_text": read(files.get("report")),
            "elapsed": task.get("elapsed") or 0,
            "chars": task.get("chars") or 0,
            "model": task.get("model") or "",
            "files": files,
            # 【新增 v1.3】
            "provider": provider_id,
            "script_text": script_text,
            "has_script": bool(script_text and script_source),
            "script_source": script_source,
            "tracks_file": str(tp) if tp.exists() else "",
            "script_file": str(OUT / f"{stem}.script.txt"),
            # 【新增 v1.6】历史任务没有逐条变更；重新清理一次即可生成
            "cue_changes": [],
        }
    except Exception as exc:
        # modified(v1.2): 原来静默吞掉，历史任务打不开却毫无线索
        print(f"[rehydrate] 重建 {task.get('id')} 失败：{exc}", flush=True)


def _prune_tasks(limit: int):
    """只淘汰已结束的任务；排队中和识别中的不能被清掉。"""
    terminal = {"done", "error", "cancelled", "interrupted"}
    while len(TASKS) > limit:
        victim = next((k for k, v in TASKS.items()
                       if v.get("status") in terminal), None)
        if victim is None:
            break
        TASKS.pop(victim, None)


def _queue_pos(tid: str) -> int:
    # modified(v1.2): 已取消 / 已中断的 tid 仍留在队列里（worker 会跳过），
    # 原来把它们算进位次，前端显示「第 3 位」其实前面根本没人。
    waiting = [x for x in list(JOB_Q.queue)
               if (TASKS.get(x) or {}).get("status") == "queued"]
    try:
        return waiting.index(tid) + 1
    except ValueError:
        return 0


def _worker(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        return
    # modified: 迁移/清理后按文件名回落，找到就把 task["path"] 自愈成新路径
    src = _locate(task.get("path"), MEDIA)
    if src is None:
        task["status"] = "error"
        task["error"] = "源文件已清理，请重新上传"
        task["finished"] = time.time()
        _persist(task_id)
        return
    if str(src) != (task.get("path") or ""):
        task["path"] = str(src)
    task["status"] = "running"
    task["started"] = time.time()
    _persist(task_id)

    log_last = [0.0]        # 上一次日志落盘的时间（单调时钟）
    def log(msg):
        task["logs"].append(str(msg))
        if len(task["logs"]) > 200:
            task["logs"] = task["logs"][-200:]
        # modified(v1.8):日志落盘节流到 1 秒一次。原来每来一行日志就整份
        # JSON 序列化 + 写盘，一小时课程会产生上千次写入；日志本身只是
        # 给人看的进度条，没必要逐行持久化。任务状态变更处仍立即 _persist。
        now = time.monotonic()
        if now - log_last[0] >= 1.0:
            log_last[0] = now
            _persist(task_id)

    try:
        r = T.run_pipeline(src, task.get("model") or "qwen3-asr",
                           task.get("clean", True), outdir=OUT,
                           language=task.get("language") or "Chinese", log=log,
                           # 【新增 v1.3】识别源与双轨开关；未指定时走本地，零回归
                           provider_key=task.get("provider"),
                           use_script_track=bool(task.get("use_script_track")),
                           # 【新增 v1.4】数字规整；历史任务无此字段时默认开
                           itn=bool(task.get("itn", True)),
                           # 【新增 v1.5】术语修复
                           termfix=bool(task.get("termfix", True)),
                           termfix_auto=bool(task.get("termfix_auto", True)))
        raw_cues = parse_cues(r["raw_srt_text"])
        clean_cues = parse_cues(r["clean_srt_text"]) if r["clean_srt_text"] else None

        task["stem"] = r["stem"]
        task["chars"] = r["chars"]
        task["elapsed"] = r["elapsed"]
        task["files"] = {
            "raw_srt": str(r["srt_path"]),
            "raw_txt": str(r["txt_path"]),
            "clean_srt": str(r["clean_srt_path"]) if r["clean_srt_path"] else None,
            "clean_txt": str(r["clean_txt_path"]) if r["clean_txt_path"] else None,
            "report": str(r["report_path"]) if r["report_path"] else None,
        }
        task["result"] = {
            "stem": r["stem"],
            "cues": raw_cues,
            "clean_cues": clean_cues,
            "doc_text": r["doc_text"],
            "clean_doc_text": r["clean_doc_text"],
            "report_text": r["report_text"],
            "elapsed": r["elapsed"],
            "chars": r["chars"],
            "model": task["model"],
            "files": task["files"],
            # 【新增 v1.3】双轨信息给前端：有整理稿才开放 diff / 采用 / 回退
            "provider": r.get("provider") or "local-qwen3",
            "script_text": r.get("script_text") or "",
            "has_script": bool(r.get("has_script")),
            "script_source": r.get("script_source") or "",
            "tracks_file": str(r.get("tracks_path") or ""),
            "script_file": str(r.get("script_txt_path") or ""),
            # 【新增 v1.6】逐条变更（原文/清理后/变更类型），界面据此做 diff 预览。
            # 注意 result 不落盘，重启后这里为空，前端会提示「重新清理一次生成」。
            "cue_changes": r.get("cue_changes") or [],
        }
        task["status"] = "done"
        task["finished"] = time.time()
        _persist(task_id)
    except Exception as exc:
        task["status"] = "error"
        task["error"] = str(exc)
        task["logs"].append(traceback.format_exc())
        task["finished"] = time.time()
        _persist(task_id)


def _run_queued(tid: str):
    """modified(v1.2): 从循环体抽出来，让「坏任务不拖死 worker」可单测。"""
    try:
        task = TASKS.get(tid)
        if task and task.get("status") == "queued":
            _worker(tid)
    except BaseException as exc:
        # modified(v1.2): 必须是 BaseException。transcribe.ensure_wav 原来
        # 用 sys.exit() 报错，抛的是 SystemExit（不是 Exception 子类），
        # 旧写法 except Exception: pass 既抓不到也不记日志，整个队列线程
        # 会静默死掉，此后所有任务永远停在 queued。这里兜住并落盘状态。
        print(f"[queue] 任务 {tid} 异常终止：{type(exc).__name__}: {exc}\n"
              f"{traceback.format_exc()}", flush=True)
        t = TASKS.get(tid)
        if t is not None:
            t["status"] = "error"
            t["error"] = f"任务异常终止：{type(exc).__name__}: {exc}"
            t["finished"] = time.time()
            _persist(tid)


def _queue_loop():
    while True:
        tid = JOB_Q.get()
        try:
            _run_queued(tid)
        finally:
            JOB_Q.task_done()


_load_jobs()
threading.Thread(target=_queue_loop, daemon=True, name="asr-queue").start()


# ─────────────────────── API ───────────────────────

@app.post("/api/upload")
def upload(file: UploadFile = File(...)):
    """modified(v1.2): 同步 def（FastAPI 自动放线程池），大视频上传不再
    阻塞事件循环；并补齐类型 / 大小 / 空文件 / wav 头校验。

    来源场景：faster-whisper 异步转写实践里的已知坑——损坏音频头会让
    解码器无限期挂住，未限制大小的上传会把磁盘撑满。
    """
    fid = uuid.uuid4().hex[:12]
    safe = _safe(Path(file.filename or "upload.bin").name)[:120] or "upload"
    ext = Path(safe).suffix.lower()
    if ext not in ALLOWED_MEDIA_EXT:
        return JSONResponse(
            {"error": f"不支持的文件类型：{ext or '无扩展名'}"}, status_code=400)
    dst = MEDIA / f"{fid}_{safe}"
    size = 0
    try:
        with open(dst, "wb") as f:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError(f"文件超过 {MAX_UPLOAD_BYTES // 1048576} MB 上限")
                f.write(chunk)
    except (OSError, ValueError) as exc:
        dst.unlink(missing_ok=True)
        return JSONResponse({"error": f"上传失败：{exc}"}, status_code=400)
    if size == 0:
        dst.unlink(missing_ok=True)
        return JSONResponse({"error": "文件为空，请重新选择"}, status_code=400)
    if ext == ".wav":
        # modified(v1.2): 只验 wav 文件头（标准库即可），无需额外依赖
        import wave
        try:
            with wave.open(str(dst), "rb") as w:
                if w.getnframes() <= 0:
                    raise ValueError("没有音频帧")
        except Exception as exc:
            dst.unlink(missing_ok=True)
            return JSONResponse({"error": f"wav 文件损坏：{exc}"}, status_code=400)
    UPLOADS[fid] = {"name": safe, "path": dst, "kind": media_type_of(safe)}
    _prune(UPLOADS, MAX_UPLOADS)       # modified: 控制内存
    return {"file_id": fid, "name": safe, "kind": UPLOADS[fid]["kind"],
            "size": size, "path": str(dst), "media_url": f"/api/media/{fid}"}


@app.get("/api/media/{fid}")
async def media(fid: str):
    info = UPLOADS.get(fid)
    if not info:
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    if not Path(info["path"]).exists():          # modified: 记录还在但文件已被删
        return JSONResponse({"error": "文件已清理，请重新上传"}, status_code=404)
    # FileResponse 原生支持 Range，视频可以拖动
    return FileResponse(info["path"], filename=info["name"])


@app.post("/api/transcribe")
async def transcribe(file_id: str = Form(...), model: str = Form("qwen3-asr"),
                     clean: bool = Form(True), language: str = Form("Chinese"),
                     provider: str = Form(""), use_script_track: bool = Form(False),
                     itn: bool = Form(True), termfix: bool = Form(True),
                     termfix_auto: bool = Form(True)):
    info = UPLOADS.get(file_id)
    if not info:
        return JSONResponse({"error": "请先上传文件"}, status_code=400)
    # modified: 原来不校验 model，非法值只会在子线程里抛 KeyError，用户只看到一屏 traceback
    if model not in T.MODELS:
        return JSONResponse(
            {"error": f"未知模型：{model}（可选：{'、'.join(T.MODELS)}）"},
            status_code=400)
    # 【新增 v1.3】识别源校验：非法 id / 未配置 / 骨架未接 HTTP 都在这里拦成 400，
    # 不让任务排进队列再失败（沿用 model 校验的同一思路）。
    raw_pid = (provider or "").strip()
    try:
        # 【新增 v1.3】未指定时用带「自动回落本地」的选源逻辑，保证默认零回归；
        # 显式指定则严格校验，缺配置/未接入都在这里拦成 400，不排进队列再失败。
        prov = (P.preferred_from_env() if not raw_pid
                else P.get_provider(raw_pid))
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if prov.kind == "cloud":
        if not prov.configured():
            return JSONResponse(
                {"error": f"{prov.name} 未配置：需环境变量 "
                          f"{'、'.join(prov.config_keys)}"}, status_code=400)
        if not getattr(prov, "ready", False):
            return JSONResponse(
                {"error": f"{prov.name} 的 HTTP 调用尚未接入（本版只提供可插拔骨架，"
                          f"字段映射已可离线验证）"}, status_code=400)
    pid = prov.id
    tid = uuid.uuid4().hex[:12]
    TASKS[tid] = {"id": tid, "status": "queued", "logs": [], "result": None,
                  "error": None, "file_id": file_id, "name": info["name"],
                  "path": str(info["path"]), "model": model, "clean": clean,
                  "language": language, "created": time.time(),
                  # 【新增 v1.3】
                  "provider": pid, "use_script_track": bool(use_script_track),
                  # 【新增 v1.4】数字规整开关
                  "itn": bool(itn),
                  # 【新增 v1.5】术语修复开关与力度
                  "termfix": bool(termfix), "termfix_auto": bool(termfix_auto)}
    _persist(tid)
    JOB_Q.put(tid)
    _prune_tasks(MAX_TASKS)
    return {"task_id": tid, "position": _queue_pos(tid)}


@app.get("/api/task/{tid}")
async def task_status(tid: str):
    t = TASKS.get(tid)
    if not t:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    _rehydrate(t)
    src = _locate(t.get("path"), MEDIA)   # modified: 迁移后按文件名找回上传件
    return {"status": t.get("status"), "logs": (t.get("logs") or [])[-80:],
            "error": t.get("error"), "result": t.get("result"),
            "name": t.get("name"), "file_id": t.get("file_id"),
            "stem": t.get("stem"), "elapsed": t.get("elapsed"),
            "path": str(src) if src else t.get("path"),
            "kind": media_type_of(t.get("name") or ""),
            "size": src.stat().st_size if src else 0,
            "created": t.get("created"), "position": _queue_pos(tid)}


@app.get("/api/task/{tid}/media")
async def task_media(tid: str):
    """历史任务的媒体流：服务重启后 UPLOADS 丢了，仍能按任务里存的路径播放。"""
    t = TASKS.get(tid)
    if not t:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    p = _locate(t.get("path"), MEDIA)   # modified: 迁移后按文件名找回上传件
    if p is None or not _inside(p, MEDIA):
        return JSONResponse({"error": "源文件已清理，请重新上传"}, status_code=404)
    return FileResponse(p, filename=t.get("name") or p.name)


@app.get("/api/tasks")
async def task_list():
    items = []
    for tid, t in list(TASKS.items()):
        items.append({
            "id": tid, "status": t.get("status"), "name": t.get("name"),
            "stem": t.get("stem"), "error": t.get("error"),
            "created": t.get("created"), "started": t.get("started"),
            "finished": t.get("finished"), "elapsed": t.get("elapsed"),
            "chars": t.get("chars"), "model": t.get("model"),
        })
    items.sort(key=lambda x: x.get("created") or 0, reverse=True)
    return {"tasks": items[:60]}


@app.post("/api/task/{tid}/retry")
async def task_retry(tid: str):
    t = TASKS.get(tid)
    if not t:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    if t.get("status") not in ("error", "interrupted", "cancelled"):
        return JSONResponse({"error": "当前状态不能重试"}, status_code=400)
    # modified(v1.2): 原来用裸 Path.exists()，与 _worker 的 _locate 不一致；
    # 工程搬迁后旧 job.json 明明能在 media/ 里找到文件，却被误判「已清理」。
    src = _locate(t.get("path"), MEDIA)
    if src is None:
        return JSONResponse({"error": "源文件已清理，请重新上传"}, status_code=400)
    t["path"] = str(src)                     # 顺手自愈成当前工程路径
    t.update(status="queued", logs=[], error=None, result=None,
             started=None, finished=None)
    _persist(tid)
    JOB_Q.put(tid)
    return {"ok": True, "position": _queue_pos(tid)}


@app.post("/api/task/{tid}/cancel")
async def task_cancel(tid: str):
    t = TASKS.get(tid)
    if not t:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    if t.get("status") != "queued":
        return JSONResponse(
            {"error": "识别中无法立即中断；等它跑完，或重启服务台"}, status_code=409)
    t["status"] = "cancelled"
    t["error"] = "已取消"
    _persist(tid)
    return {"ok": True}


@app.get("/api/terms")
def get_terms():
    # modified(v1.2): 词表损坏时给明确错误，而不是让 json.load 抛成 500 白屏
    try:
        with open(RULES, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": f"词表读取失败：{exc}"}, status_code=500)


@app.post("/api/terms")
def save_terms(payload: dict):
    """保存口语词表。

    只改各组的 words，mode / desc / conditional 等规则字段原样保留——
    这些是引擎的行为约定，不能让前端误写。
    """
    # modified(v1.8):原来直接 open + json.load，词表被外部编辑坏（或同步盘
    # 同步到一半）时这里抛 JSONDecodeError，界面上表现为 500 且无法保存任何词。
    try:
        with open(RULES, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        return JSONResponse(
            {"error": f"词表读取失败，请检查 rules/filler_words.json：{exc}"},
            status_code=500)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("groups"), dict):
        return JSONResponse({"error": "词表结构异常：缺少 groups"}, status_code=500)

    groups = payload.get("groups")
    if isinstance(groups, dict):
        for name, words in groups.items():
            if name in cfg.get("groups", {}) and isinstance(words, list):
                cfg["groups"][name]["words"] = [
                    str(x).strip() for x in words if str(x).strip()]

    # 「嘛/哈」这类只在句尾删的词要分流，不能混进 words 变成无条件删
    tails = payload.get("tail_only")
    if isinstance(tails, dict):
        for name, words in tails.items():
            if name in cfg.get("groups", {}) and isinstance(words, list):
                cfg["groups"][name]["tail_only"] = [
                    str(x).strip() for x in words if str(x).strip()]

    # 兜底：同一组里 tail_only 的词不能同时留在 words，否则会变成无条件删
    for spec in cfg.get("groups", {}).values():
        tail_set = set(spec.get("tail_only", []))
        if tail_set:
            spec["words"] = [w for w in spec.get("words", []) if w not in tail_set]

    # 兼容旧客户端提交的三层键
    legacy = {
        "level1_pure_filler": "语气词",
        "level2_pause_word": "句首填充",
        "level3_filler_phrase": "组合填充",
    }
    for key, gname in legacy.items():
        if key in payload and isinstance(payload[key], list) and gname in cfg.get("groups", {}):
            cfg["groups"][gname]["words"] = [
                str(x).strip() for x in payload[key] if str(x).strip()]

    # modified(v1.2): 原子写 + 一次性备份。词表是 101 词的核心资产，
    # 原来直接 open(..., "w")，写到一半进程退出就会留下半截 JSON，
    # 下次启动连界面都打不开。
    bak = RULES.with_suffix(".json.bak")
    if not bak.exists():
        try:
            shutil.copy2(RULES, bak)
        except OSError:
            pass
    try:
        _atomic_write(RULES, json.dumps(cfg, ensure_ascii=False, indent=2))
    except OSError as exc:
        return JSONResponse({"error": f"词表写入失败：{exc}"}, status_code=500)
    return {"ok": True, "terms": cfg}


@app.post("/api/reclean")
def reclean(file_id: str = Form(...), cues_json: str = Form(...),
            itn: bool = Form(True), termfix: bool = Form(True),
            termfix_auto: bool = Form(True)):
    """用当前口语词表重新清理一份字幕（输入是前端当前的原始 cues）。

    【修改 v1.8】由 async def 改成 def：清理是纯 CPU 计算，几千条字幕要
    跑一两秒。放在 async def 里会占住唯一的事件循环，这期间首页、任务轮询、
    截图请求全部排队——FastAPI 会把 def 端点丢进线程池，不挡事件循环。
    """
    # modified: 原来直接 json.loads，非法 JSON 会 500；这里统一兜成 400
    try:
        cues = json.loads(cues_json)
        if not isinstance(cues, list):
            raise ValueError("应为数组")
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": f"字幕数据格式错误：{exc}"}, status_code=400)

    cleaner = CT.Cleaner(CT.load_rules(RULES),
                         CT.load_corrections(BASE / "terms" / "corrections.txt"),
                         itn=itn,                # 【新增 v1.4】受限数字规整
                         termfix=termfix,        # 【新增 v1.5】简称膨胀 + 漏字补全
                         termfix_auto=termfix_auto)
    out, hit, tips, seen = [], 0, [], set()      # modified: 去重从 O(n²) 列表查改成 set
    diffs = []                                   # 【新增 v1.6】逐条变更，供 diff 预览
    for c in cues:
        if not isinstance(c, dict):
            continue
        before = str(c.get("text") or "")
        new, changes, _ = cleaner.clean(before)
        if changes:
            hit += 1
            diffs.append({"idx": c.get("idx"), "before": before, "after": new,
                          "changes": [f"{a}·{b}" for a, b, _ in changes]})
        for s in cleaner.last_suggestions:
            if s not in seen:
                seen.add(s)
                tips.append(s)
        out.append({**c, "text": new})
    return {"cues": out, "changed": hit, "suggestions": tips[:60],
            "diffs": diffs}      # 【新增 v1.6】


@app.post("/api/save")
def save(payload: dict):
    """保存校对后的字幕与文稿"""
    # modified: stem 原来直接拼路径，传 "../../evil" 能写到 out 目录之外
    stem = _safe(payload.get("stem") or "transcript")
    # modified(v1.2): 畸形 cues 原来直接 500（已复现），统一 400
    try:
        cues = _normalize_cues(payload.get("cues"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    srt_path = OUT / f"{stem}.edited.srt"
    txt_path = OUT / f"{stem}.edited.txt"
    # modified(v1.2): 原子写 + 保留上一版。自动保存 1.2s 一次，写盘中途
    # 退出会留下半截 SRT；有 .bak 也便于回退误覆盖的校对稿。
    if srt_path.exists():
        try:
            shutil.copy2(srt_path, OUT / f"{stem}.edited.srt.bak")
        except OSError:
            pass
    try:
        _atomic_write(srt_path, cues_to_srt(cues))
        _atomic_write(txt_path, "\n".join(c["text"] for c in cues))
    except OSError as exc:
        return JSONResponse({"error": f"保存失败：{exc}"}, status_code=500)

    # 【新增 v1.5】修订学习：拿系统清理稿与用户提交稿对比，把差异累计成候选。
    # 只记录、不应用，采纳与否由人在界面上逐条决定。
    # 整段包在 try 里：学习出任何问题都不能影响保存本身。
    learned = 0
    try:
        csrt = OUT / f"{stem}.clean.srt"
        if csrt.exists():
            learned = _LEARN.record(stem, parse_cues(csrt.read_text(encoding="utf-8")), cues)
    except Exception:
        learned = 0

    return {"ok": True, "srt": str(srt_path), "txt": str(txt_path),
            "learned": learned}


@app.get("/api/learn/pending")
def api_learn_pending():
    """【新增 v1.5】修订学习候选：同一处修改出现 ≥2 次才列出。

    只读，不会自动改任何词表 —— 采纳必须由人点。
    """
    return {"ok": True, "items": _LEARN.pending(), "stats": _LEARN.stats()}


@app.post("/api/learn/accept")
def api_learn_accept(payload: dict):
    """【新增 v1.5】采纳一条候选，写入 corrections.txt 或 aliases.txt。"""
    key = str(payload.get("key") or "").strip()
    if not key or "=>" not in key:
        return JSONResponse({"error": "候选参数不合法"}, status_code=400)
    kind = payload.get("kind")
    if kind not in (None, "", "correction", "alias", "unknown"):
        return JSONResponse({"error": "kind 只能是 correction / alias"}, status_code=400)
    try:
        wrote, msg = _LEARN.accept(key, kind or None)
    except OSError as exc:
        return JSONResponse({"error": f"写入词表失败：{exc}"}, status_code=500)
    return {"ok": True, "wrote": wrote, "message": msg,
            "stats": _LEARN.stats()}


@app.post("/api/learn/ignore")
def api_learn_ignore(payload: dict):
    """【新增 v1.5】忽略一条候选（不再列出）。"""
    key = str(payload.get("key") or "").strip()
    if not key:
        return JSONResponse({"error": "缺少 key"}, status_code=400)
    return {"ok": _LEARN.ignore(key), "stats": _LEARN.stats()}


@app.get("/api/draft")
async def get_draft(stem: str):
    """取上次自动保存的校对稿；只有比原始识别稿新才值得恢复。"""
    s = _safe(stem)
    p = OUT / f"{s}.edited.srt"
    raw = OUT / f"{s}.raw.srt"
    if not p.exists():
        return {"exists": False}
    try:
        cues = parse_cues(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return JSONResponse({"error": f"草稿读取失败：{exc}"}, status_code=400)
    return {"exists": True, "cues": cues,
            "draft_mtime": p.stat().st_mtime,
            "raw_mtime": raw.stat().st_mtime if raw.exists() else 0}


@app.post("/api/export")
def export_file(payload: dict):
    """按当前校对内容即时生成下载文件（不落盘，避免覆盖历史稿）"""
    from fastapi import Response
    from urllib.parse import quote

    kind = payload.get("kind", "srt")
    stem = _safe(payload.get("stem") or "transcript")   # modified: 文件名安全化
    cues = payload.get("cues") or []
    report = payload.get("report") or ""
    # 【新增 v1.6】纯文本导出选项；非法值一律回落默认，不让它 500
    mode = str(payload.get("mode") or "para")
    if mode not in ("line", "para", "raw"):
        mode = "para"
    blank = bool(payload.get("blank", True))
    punct = str(payload.get("punct") or "keep")
    if punct not in ("keep", "full", "half"):
        punct = "keep"

    # modified(v1.2): 与 /api/save 共用同一套校验，缺时间码不再 500（已复现）
    if kind != "report":
        try:
            cues = _normalize_cues(cues)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    if kind == "srt":
        content, ext, mime = cues_to_srt(cues), "srt", "text/plain"
    elif kind == "vtt":                                  # modified: 新增 WebVTT
        content, ext, mime = cues_to_vtt(cues), "vtt", "text/vtt"
    elif kind == "txt":
        # 【修改 v1.6】原来固定「每句一行」，现在按导出设置分段
        content = build_plain_text(cues, mode, blank, punct)
        ext, mime = "txt", "text/plain"
    elif kind == "md":
        # Markdown 必须空行才是段落，这里强制 blank=True，不受设置影响
        content = f"# {stem}\n\n" + build_plain_text(cues, mode, True, punct)
        ext, mime = "md", "text/markdown"
    elif kind == "report":
        content, ext, mime = report or "（未启用口语清理，无变更报告）", "md", "text/markdown"
    else:
        return JSONResponse({"error": f"未知格式：{kind}"}, status_code=400)

    fname = f"{stem}.{ext}"
    return Response(
        content.encode("utf-8"),
        media_type=f"{mime}; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@app.get("/api/download")
def download(path: str):
    p = Path(path)
    # modified: 改用 _inside，避免 output_evil 之类的前缀绕过
    if not _inside(p, OUT) or not p.exists() or not p.is_file():
        return JSONResponse({"error": "非法路径"}, status_code=400)
    # modified(v1.2): 原来能下载 OUT 下任意文件，包括 _jobs/*.json
    # （内含本机绝对路径）与 _peaks 缓存。已实测可读到 1470 字节 job 文件。
    # 现在只放行对外产物：白名单扩展名 + 禁止下划线开头的内部目录。
    if p.suffix.lower() not in DOWNLOAD_EXT:
        return JSONResponse({"error": "该文件类型不允许下载"}, status_code=400)
    rel = p.resolve().relative_to(OUT.resolve())
    if any(part.startswith("_") for part in rel.parts):
        return JSONResponse({"error": "内部文件不允许下载"}, status_code=400)
    return FileResponse(p, filename=p.name)


@app.get("/api/health")
def api_health():
    """modified(v1.2): 健康检查。来源场景：本地转写服务的探活实践。

    /api/tasks 之前的接口都能当探活，但都会带业务数据；单独留一个轻量端点，
    便于服务台 / 自动化脚本判断「服务活着、队列没堵」。推理在后台线程，
    这里不会被 CPU 任务阻塞。
    """
    running = [t for t in TASKS.values() if t.get("status") == "running"]
    return {"ok": True, "version": "1.9",       # modified(v1.9): 长音频段错误修复
            "queued": sum(1 for t in TASKS.values() if t.get("status") == "queued"),
            "running": len(running),
            "tasks": len(TASKS), "uploads": len(UPLOADS),
            # 【新增 v1.3】默认识别源与云端配置状态，便于服务台一眼看清
            "provider": P.preferred_from_env().id,
            "cloud_ready": [p["id"] for p in P.list_providers()
                            if p["kind"] == "cloud" and p["configured"]
                            and p.get("ready")],
            # 【新增 v1.7】本地润色模型是否就绪（只查文件与依赖，不加载）
            "llm": LP.available()[0]}


# ─────────────────────── 【新增 v1.3】识别源与双轨整理稿 ───────────────────────

@app.get("/api/providers")
def api_providers():
    """列出识别源；只回配置状态，绝不回密钥值。

    前端据此渲染下拉：本地可用、云端未配置/未接入时标灰并给出原因。
    """
    return {"default": P.DEFAULT_PROVIDER, "providers": P.list_providers()}


@app.get("/api/tracks")
def api_tracks(stem: str = ""):
    """读取一次识别的双轨产物，并算好整理前后 diff。

    字幕轨 subtitle_text 永远是 ASR 原文；讲稿轨 script_text 可为空。
    前端一键回退直接用 subtitle_text，不需要依赖撤销栈。
    """
    name = _safe(stem)
    p = OUT / f"{name}.tracks.json"
    if not p.exists() or not _inside(p, OUT):
        return JSONResponse({"error": "没有找到该任务的双轨产物"}, status_code=404)
    try:
        tracks = P.Tracks.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:
        return JSONResponse({"error": f"双轨产物损坏：{exc}"}, status_code=400)
    return {"ok": True, "tracks": tracks.to_dict(),
            "diff": P.diff_ops(tracks.subtitle_text, tracks.script_text)
            if tracks.has_script else []}


@app.post("/api/diff")
def api_diff(payload: dict):
    """整理前后 diff（纯计算，不落盘）。前端预览与一键回退共用。"""
    before = str(payload.get("before") or "")
    after = str(payload.get("after") or "")
    return {"ok": True, "ops": P.diff_ops(before, after),
            "restored": P.restore_from_diff(P.diff_ops(before, after))}


# ─────────────────────── 七遍改稿 ───────────────────────

@app.get("/api/revise/meta")
async def api_revise_meta():
    """七遍的定义集中放在 revise.py，前端不再重复一份文案。"""
    return {"passes": [
        {"n": n, "name": v[0], "desc": v[1], "level": v[2]}
        for n, v in sorted(RV.PASSES.items())
    ]}


@app.post("/api/revise")
async def api_revise(payload: dict):
    """执行某一遍。无状态：输入是什么稿子，就从什么稿子改。"""
    try:
        n = int(payload.get("pass", 1))
    except (TypeError, ValueError):
        return JSONResponse({"error": "pass 必须是整数"}, status_code=400)
    if not 1 <= n <= 7:
        return JSONResponse({"error": "pass 取值 1–7"}, status_code=400)
    try:
        res = RV.run_pass(n, payload.get("text") or "", payload.get("opts") or {})
        return res
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/revise/save")
async def api_revise_save(payload: dict):
    """每遍结果单独存 v0..v7，原稿 v0 永不覆盖。"""
    stem = _safe(payload.get("stem") or "draft")
    # modified: pass 传非数字会 ValueError 直接 500
    try:
        n = int(payload.get("pass", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "pass 必须是整数"}, status_code=400)
    if not 0 <= n <= 7:
        return JSONResponse({"error": "pass 取值 0–7"}, status_code=400)
    d = REV / stem
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"v{n}.md"
    if n == 0 and p.exists():
        return {"ok": True, "path": str(p), "kept": True}
    _atomic_write(p, payload.get("text") or "")   # modified(v1.2): 原子写
    return {"ok": True, "path": str(p)}


@app.get("/api/revise/versions")
async def api_revise_versions(stem: str):
    d = REV / _safe(stem)
    out = {}
    if d.exists():
        for i in range(0, 8):
            p = d / f"v{i}.md"
            if p.exists():
                out[str(i)] = p.read_text(encoding="utf-8")
    return {"versions": out}


@app.post("/api/revise/export")
async def api_revise_export(payload: dict):
    """导出成稿：md / txt / docx（公文版式）"""
    from fastapi import Response
    from urllib.parse import quote

    fmt = payload.get("fmt", "md")
    stem = _safe(payload.get("stem") or "成稿")      # modified: 用于下载文件名，需安全化
    text = payload.get("text") or ""
    if not text.strip():
        return JSONResponse({"error": "没有内容可导出"}, status_code=400)

    if fmt == "docx":
        data = RV.to_docx(text, stem)
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition":
                     f"attachment; filename*=UTF-8''{quote(stem + '.docx')}"})

    if fmt == "txt":
        body, ext, mime = text, "txt", "text/plain"
    else:
        body, ext, mime = RV.export_markdown(text, stem), "md", "text/markdown"

    return Response(
        body.encode("utf-8"),
        media_type=f"{mime}; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(f'{stem}.{ext}')}"})


@app.post("/api/revise/tts")
async def api_revise_tts(payload: dict):
    """第7遍用：把成稿读出来，听哪里磕绊。"""
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "没有内容"}, status_code=400)
    # modified: limit 原来不设上下限，传超大值会把整篇丢给 edge_tts，慢且容易失败
    try:
        limit = int(payload.get("limit", 1500) or 1500)
    except (TypeError, ValueError):
        limit = 1500
    limit = max(100, min(limit, 5000))
    full_len = len(text)
    text = text[:limit]
    voice = payload.get("voice") or "zh-CN-YunjianNeural"
    # modified(v1.2): 朗读文件原来只增不减，out/ 会无限累积；保留最近 10 个
    try:
        olds = sorted(OUT.glob("_tts_*.mp3"),
                      key=lambda x: x.stat().st_mtime, reverse=True)
        for q in olds[9:]:
            q.unlink(missing_ok=True)
    except OSError:
        pass
    name = f"_tts_{uuid.uuid4().hex[:8]}.mp3"
    p = OUT / name
    try:
        import edge_tts
        await edge_tts.Communicate(text, voice, rate="+8%").save(str(p))
    except Exception as exc:
        return JSONResponse({"error": f"朗读生成失败（需要联网）：{exc}"}, status_code=500)
    return {"url": f"/api/audio/{name}", "chars": len(text),
            "truncated": full_len > limit}


# ─────────────────────── 【新增 v1.7】讲稿层 LLM 顺滑 ───────────────────────
#
# 只作用于讲稿层。字幕（cues + 字级时间戳）一个字都不碰——整段改写会打断
# align.py 的字级对齐，音画就不同步。这是 v1.3 双轨设计里写死的红线。

_LLM_BUSY = threading.Lock()   # 同时只跑一份；并发只会让两份都慢到超时
_LLM_START = [0.0]             # 单调时钟，记录当前这一次开始时间
# modified(v1.8) 新增:单次润色的硬上限。llama.cpp 的推理循环在 C++ 里跑，
# Python 侧无法中断（threading 也没有安全的 kill），所以超时只能做到
# 「不再接受新请求 + 明确告诉你卡住了」，做不到自动回收。
# 8000 字上限本身约 3 分钟，600 秒留了充足余量，正常不会误判。
LLM_MAX_SECONDS = int(os.environ.get("ASR_LLM_TIMEOUT", "600"))


@app.get("/api/llm/status")
def api_llm_status():
    """模型是否就绪。前端据此置灰按钮并写明原因，而不是点了等半天才失败。"""
    ok, detail = LP.available()
    return {"available": ok, "detail": detail,
            "model_dir": str(LP.MODEL_DIR),
            "max_input": LP.MAX_INPUT, "chunk_chars": LP.CHUNK_CHARS}


@app.post("/api/llm/polish")
def api_llm_polish(payload: dict):
    """讲稿润色：只返回预览，不落盘、不碰字幕。

    用同步端点（def 而非 async def）：FastAPI 会把同步端点放进线程池，
    模型在 CPU 上要跑十几秒到几分钟，期间首页、截图、任务轮询照常响应。

    为什么不在这里保存：润色结果必须先过 diff 预览和红线审计才能采用，
    直接落盘就等于绕过了闸门。采用由前端另起 stem 调 /api/revise/save。
    """
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "没有可润色的文本"}, status_code=400)
    if len(text) > LP.MAX_INPUT:
        return JSONResponse(
            {"error": f"文本 {len(text)} 字，超过单次上限 {LP.MAX_INPUT} 字，"
                      f"请分成两三段分别润色"}, status_code=400)
    ok, detail = LP.available()
    if not ok:
        return JSONResponse({"error": f"本地润色模型不可用：{detail}"},
                            status_code=400)
    # modified(v1.8):原来是 acquire 失败就一律 409，但推理卡死时锁永远不会释放，
    # 用户只会看到「正在润色」的假象、反复重试也没用。现在区分两种情形：
    # 正常占用 → 409（等一下就好）；超时占用 → 503（明说卡住了、要重启）。
    if not _LLM_BUSY.acquire(blocking=False):
        stuck = time.monotonic() - (_LLM_START[0] or time.monotonic())
        if stuck > LLM_MAX_SECONDS:
            return JSONResponse(
                {"error": f"上一次润色已持续 {int(stuck)} 秒仍未返回，疑似卡死。"
                          f"推理在 C++ 层执行、无法从 Python 中断，请重启服务后再试"},
                status_code=503)
        return JSONResponse(
            {"error": f"已有一份稿子正在润色（已 {int(stuck)} 秒），请等它完成"},
            status_code=409)
    _LLM_START[0] = time.monotonic()
    try:
        logs, t0 = [], time.time()
        r = LP.polish(text, log=lambda m: logs.append(m))
        r["blocked"] = LP.blocked(r["audit"])   # 硬红线未过的项，前端据此禁用「采用」
        r["logs"] = logs
        r["total_elapsed"] = round(time.time() - t0, 1)
        return r
    except Exception as exc:
        # modified(v1.8):traceback 只进服务日志，不回给前端：里面的绝对路径
        # 属于本机信息，而且对用户也没有可用信息量。
        print(f"[llm] 润色失败：{exc}\n{traceback.format_exc()[-1200:]}", flush=True)
        return JSONResponse({"error": f"润色失败：{exc}"}, status_code=500)
    finally:
        _LLM_BUSY.release()
        _LLM_START[0] = 0.0


@app.get("/api/audio/{name}")
async def api_audio(name: str):
    p = OUT / name
    # modified: 同样改用 _inside；name 里带 ../ 时会被挡住
    if not _inside(p, OUT) or not p.exists():
        return JSONResponse({"error": "不存在"}, status_code=404)
    return FileResponse(p, media_type="audio/mpeg")


# ─────────────────────── 波形 ───────────────────────

def _compute_peaks(src: Path) -> dict:
    """ffmpeg 解码成 16k 单声道，按 100ms 一桶取 min/max，供前端画波形。"""
    import subprocess

    import imageio_ffmpeg
    import numpy as np

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [exe, "-v", "error", "-i", str(src), "-vn", "-ac", "1",
           "-ar", "16000", "-f", "s16le", "-"]
    # modified(v1.2): 原来是 Popen + stdout.read() 且没有超时，损坏音频会让
    # 线程池里的请求永久挂住（faster-whisper 异步转写实践的已知坑）。
    # 改成带 timeout 的一次性解码，超时由 subprocess 负责 kill；顺带删掉
    # 手写分块 / 字节对齐的冗余逻辑，代码少一半，行为一致。
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=PEAKS_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ffmpeg 解码超时（{PEAKS_TIMEOUT}s），文件可能损坏") from exc
    if p.returncode != 0:
        raise RuntimeError("ffmpeg 解码失败，可能是文件损坏或格式不支持")
    arr = np.frombuffer(p.stdout, dtype="<i2")
    bucket = 1600                       # 100ms @ 16k
    fit = (arr.size // bucket) * bucket
    mins, maxs = [], []
    if fit:
        block = arr[:fit].reshape(-1, bucket)
        mins = (block.min(axis=1) / 32768.0).round(4).tolist()
        maxs = (block.max(axis=1) / 32768.0).round(4).tolist()
    tail = arr[fit:]
    if tail.size:
        mins.append(round(float(tail.min()) / 32768.0, 4))
        maxs.append(round(float(tail.max()) / 32768.0, 4))
    return {"duration": round(arr.size / 16000.0, 3), "bucket_ms": 100,
            "mins": mins, "maxs": maxs}


@app.get("/api/peaks")
def api_peaks(file_id: str = "", stem: str = "", n: int = 2400):
    """返回降采样后的波形峰值。stem 优先（复用 16k wav），否则用刚上传的文件。"""
    src, key = None, ""
    if stem:
        s = _safe(stem)
        wav = OUT / f"{s}.16k.wav"
        if wav.exists():
            src, key = wav, s
    if src is None and file_id:
        info = UPLOADS.get(file_id)
        if info and Path(info["path"]).exists():
            src, key = Path(info["path"]), file_id
    if src is None:
        return JSONResponse({"error": "找不到音频"}, status_code=404)

    cache = PEAKS / f"{key}.json"
    data = None
    try:
        if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if not isinstance(data.get("mins"), list):
                    data = None
            except (ValueError, OSError):
                data = None        # modified(v1.2): 半截缓存自动重建，不再 500
        if data is None:
            data = _compute_peaks(src)
            _atomic_write(cache, json.dumps(data, ensure_ascii=False))
    except Exception as exc:
        return JSONResponse({"error": f"波形生成失败：{exc}"}, status_code=500)

    mins, maxs = data["mins"], data["maxs"]
    if not mins:
        return JSONResponse({"error": "音频没有可绘制的采样"}, status_code=400)
    target = max(200, min(int(n or 2400), 8000))
    if len(mins) > target:
        step = len(mins) / target
        om, ox = [], []
        for i in range(target):
            a = int(i * step)
            b = max(a + 1, int((i + 1) * step))
            om.append(min(mins[a:b]))
            ox.append(max(maxs[a:b]))
        mins, maxs = om, ox
    # modified(v1.2): 降采样后每个点代表的时长变了，原来仍返回 100ms，
    # 前端若按 bucket_ms 换算时间轴会整体偏移。
    bucket_ms = round(data["duration"] * 1000 / len(mins), 3)
    return {"duration": data["duration"], "bucket_ms": bucket_ms,
            "mins": mins, "maxs": maxs}


# ─────────────────────── 文稿对齐 ───────────────────────

@app.post("/api/align")
def api_align(payload: dict):
    """用讲义/PPT 文字稿对齐已有识别结果，不重跑模型。"""
    stem = _safe(payload.get("stem") or "")
    script = (payload.get("script") or "").strip()
    if not stem:
        return JSONResponse({"error": "缺少 stem"}, status_code=400)
    if len(script) < 10:
        return JSONResponse({"error": "文稿太短，至少 10 个字"}, status_code=400)
    if len(script) > 300000:
        return JSONResponse({"error": "文稿过长（超过 30 万字）"}, status_code=400)

    meta = OUT / f"{stem}.json"
    if not meta.exists():
        return JSONResponse({"error": "找不到该课的识别结果，请先转写"}, status_code=400)
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        asr_text = data.get("text") or ""
        ts = data.get("timestamps") or []
        if not ts:
            return JSONResponse(
                {"error": "这次识别没有保存字级时间戳；请用 Qwen3-ASR 重跑后再对齐"},
                status_code=400)
        res = AL.align(script, asr_text, ts)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"对齐失败：{exc}"}, status_code=400)

    # modified(v1.2): 对齐结果是外部 JSON 驱动算出来的，落盘前同样做一次
    # 结构校验，并原子写，避免生成半截 aligned.srt。
    try:
        cues = _normalize_cues(res["cues"])
    except ValueError as exc:
        return JSONResponse({"error": f"对齐结果异常：{exc}"}, status_code=500)
    out_srt = OUT / f"{stem}.aligned.srt"
    _atomic_write(out_srt, cues_to_srt(cues))
    res["srt"] = str(out_srt)
    return res


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


if __name__ == "__main__":
    import webbrowser
    # modified: 设 ASR_NO_BROWSER=1 可跳过自动开浏览器（无头验证/自动化场景）
    if not os.environ.get("ASR_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:7870")).start()
    uvicorn.run(app, host="127.0.0.1", port=7870, log_level="warning")
