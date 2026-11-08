#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
授课文稿转写主脚本
==================
视频/音频 → 抽取 16k 单声道 → FunASR 识别 → 字级时间戳对齐 → SRT / TXT / JSON

用法：
    python transcribe.py -i 网课.mp4
    python transcribe.py -i 网课.mp4 --model sensevoice --clean
    python transcribe.py -i ./课程目录/           # 批处理整个目录
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading                    # 【新增 v2.4】FunASR 整段识别期间的心跳进度
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

BASE = Path(__file__).resolve().parent
HOTWORD_FILE = BASE / "terms" / "hotwords.txt"

# 模型统一放 D 盘，不占 C 盘空间。可用环境变量 ASR_MODEL_ROOT 覆盖。
MODEL_ROOT = Path(os.environ.get("ASR_MODEL_ROOT") or r"D:\AI\models")
os.environ.setdefault("MODELSCOPE_CACHE", str(MODEL_ROOT / "modelscope"))
os.environ.setdefault("HF_HOME", str(MODEL_ROOT / "huggingface"))
os.environ.setdefault("HF_HUB_CACHE", str(MODEL_ROOT / "huggingface" / "hub"))

MEDIA_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
             ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".wma"}

MODELS = {
    "paraformer": {
        "model": "paraformer-zh",
        "vad": "fsmn-vad",
        "punc": "ct-punc",
        "hotword": True,
        "backend": "funasr",
        "note": "中文稳、字级时间戳、支持热词 —— 出字幕首选",
    },
    "sensevoice": {
        "model": "iic/SenseVoiceSmall",
        "vad": "fsmn-vad",
        "punc": None,
        "hotword": False,
        "backend": "funasr",
        "note": "最快，自带标点，不支持热词",
    },
    "qwen3-asr": {
        "model": "Qwen/Qwen3-ASR-1.7B",
        "vad": None,
        "punc": None,
        "hotword": False,
        "backend": "qwen3",
        "aligner": "Qwen/Qwen3-ForcedAligner-0.6B",
        "dtype": "bfloat16",
        "note": "精度最高，CPU 约 2.4 倍实时，字级时间戳靠强制对齐，不支持热词",
    },
}

# modified(v1.2): 补上换行/制表符，与 align.py 的「有效字」口径统一。
# 原来两处各写一份 PUNCT，ASR 文本含换行时字级时间戳会整体错位。
PUNCT = set("，。、；：？！,.;:?!…—~·\"'“”‘’（）()《》【】<>　 \n\r\t")
SENT_END = "。！？!?"


# ────────────────────────── 音频准备 ──────────────────────────

def _is_16k_mono(path: Path) -> bool:
    """判断 wav 是否已经是 16kHz 单声道。

    modified: 原实现只看后缀就 return src，48kHz 立体声 wav 会被直接送进模型。
    这里用标准库 wave 读文件头；读不了（浮点 / WAVE_FORMAT_EXTENSIBLE）就
    当作不合格，交给 ffmpeg 统一转码，宁可多转一次也不要识别出怪结果。
    """
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            return w.getframerate() == 16000 and w.getnchannels() == 1
    except Exception:
        return False


def ensure_wav(src: Path, outdir: Path, stem: str = None) -> Path:
    """非 16k 单声道 wav 一律转码；确认合格才原样返回"""
    stem = stem or src.stem                                  # modified: 支持外部指定去重后的名字
    if src.suffix.lower() == ".wav" and _is_16k_mono(src):   # modified: 原来只判断后缀
        return src
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    dst = outdir / (stem + ".16k.wav")
    if dst.exists() and dst.stat().st_mtime > src.stat().st_mtime:
        print(f"  复用已转码音频：{dst.name}")
        return dst
    print(f"  转码中：{src.name} → 16kHz 单声道")
    cmd = [exe, "-y", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1",
           "-f", "wav", str(dst)]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        # modified(v1.2): 库函数不该调 sys.exit()。SystemExit 是 BaseException，
        # 服务端队列线程的 except Exception 抓不到，一个坏音频就能让整个
        # 串行队列静默停摆。改成普通异常，由 CLI / worker 各自处理。
        raise RuntimeError(
            f"ffmpeg 转码失败：{p.stderr.decode('utf-8', 'ignore')[-800:]}")
    return dst


def unique_stem(src: Path, outdir: Path) -> str:
    """给同一输出目录下的同名源文件分配不冲突的 stem。

    modified: 新增。批处理 ./课程A/第1讲.mp4 与 ./课程B/第1讲.mp4 时，
    原来两者都叫「第1讲」，产物互相覆盖，还会错误复用对方的转码音频。
    规则：已有同名产物且 meta.source 指向别的文件 → 追加 _2、_3…
    单文件场景、或重复处理同一个文件，仍然拿到原名。
    """
    def _src_of(meta_path: Path) -> str:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return str((data.get("meta") or {}).get("source") or "")
        except Exception:
            return ""

    stem = src.stem
    meta = outdir / f"{stem}.json"
    if not meta.exists():
        return stem
    old = _src_of(meta)
    if not old or Path(old).resolve() == src.resolve():
        return stem

    for i in range(2, 100):
        cand = f"{stem}_{i}"
        m2 = outdir / f"{cand}.json"
        if not m2.exists():
            return cand
        if Path(_src_of(m2)).resolve() == src.resolve():
            return cand                      # 就是自己，复用已有产物
    return stem


# ────────────────────────── 热词 ──────────────────────────

def load_hotwords(path: Path, default_weight: int = 20) -> str:
    """读取热词表，返回 FunASR 要求的 '词 权重 词 权重' 格式"""
    if not path.exists():
        return ""
    parts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            w, _, wt = line.partition("|")
            w, wt = w.strip(), wt.strip()
            parts.append(f"{w} {wt or default_weight}")
        else:
            parts.append(f"{line} {default_weight}")
    return " ".join(parts)


# ────────────────────────── 时间戳对齐 ──────────────────────────

def fmt_ts(ms) -> str:
    ms = int(max(0, ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def count_effective(text: str) -> int:
    """统计参与时间戳对齐的有效字数（去掉标点与空白）"""
    return sum(1 for c in text if c not in PUNCT)


def align_sentences(text: str, ts: list) -> list:
    """把整段文本按句末标点切片，用字级时间戳给每句赋起止时间。

    FunASR 的 timestamp 是逐字的 [[start_ms, end_ms], ...]，只对应非标点字符。
    """
    if not text:
        return []
    if not ts:
        return [{"text": text.strip(), "start": 0, "end": 0}]

    parts = re.split(r"(?<=[%s])" % re.escape(SENT_END), text)
    out, idx = [], 0
    for p in parts:
        p = p.strip()
        if not p:
            continue
        n = count_effective(p)
        if n == 0:
            continue
        head = min(idx, len(ts) - 1)
        tail = min(idx + n - 1, len(ts) - 1)
        out.append({"text": p, "start": ts[head][0], "end": ts[tail][1]})
        idx += n
    return out


MAX_LINE = 20          # 单行字幕的目标字数，超出就按标点 + 词边界切
# 21–28 字的句子故意不切：宁可单条略超目标值，也不要把一句完整的话劈成两半。
# 所以「MAX_LINE 20」是切分目标，「MAX_CUE_CHARS 28」是不切分的容忍上限。
MAX_CUE_CHARS = 28


_JIEBA = None


def _split_by_word(seg: str, limit: int) -> list:
    """按词边界切分，避免把「冷链药品」切成「冷链药/品」"""
    global _JIEBA
    if _JIEBA is None:
        import jieba
        jieba.setLogLevel(60)
        _JIEBA = jieba

    out, buf = [], ""
    for w in _JIEBA.cut(seg):
        if len(buf) + len(w) > limit and buf:
            out.append(buf)
            buf = w
        else:
            buf += w
    if buf:
        out.append(buf)
    return out


def split_cue(text: str, limit: int = MAX_LINE) -> list:
    """把长句切成适合字幕的短行。

    先按标点切，再按词边界切，最后合并过短片段。
    顿号不切：「出租、出借证照」是一个整体。
    """
    text = text.strip()
    if len(text) <= MAX_CUE_CHARS:
        return [text]

    parts = [p for p in re.split(r"(?<=[，。！？；：])", text) if p]
    pieces = []
    for p in parts:
        if len(p) <= limit:
            pieces.append(p)
        else:
            pieces.extend(_split_by_word(p, limit))

    merged = []
    for x in pieces:
        if merged and len(merged[-1]) + len(x) <= limit:
            merged[-1] += x
        else:
            merged.append(x)
    return [m for m in merged if m.strip()] or [text]


def cues_with_time(sentences: list) -> list:
    """把句子展开为字幕条目；长句按字数比例分配时间，保证时间轴连续"""
    cues = []
    for s in sentences:
        body, st, et = s["text"], s["start"], s["end"]
        pieces = split_cue(body)
        if len(pieces) == 1:
            cues.append((st, et, body))
            continue
        total = sum(len(p) for p in pieces) or 1
        cur = st
        for p in pieces:
            span = (et - st) * len(p) / total
            cues.append((int(cur), int(cur + span), p))
            cur += span
    return cues


# ────────────────────────── 识别 ──────────────────────────

def _emit(progress, pct: float, stage: str = ""):
    """【新增 v2.0】进度回调的唯一出口。

    前端靠它画进度条。回调是「锦上添花」，一旦它抛异常绝不能把识别带崩，
    所以这里一律吞掉——进度条不动比任务失败好得多。
    """
    if progress is None:
        return
    try:
        progress(max(0.0, min(100.0, float(pct))), str(stage))
    except Exception:
        pass


def transcribe(wav: Path, cfg: dict, hotword: str, device: str,
               language: str = "Chinese", log=print, progress=None) -> dict:
    t1 = time.time()
    if cfg.get("backend") == "qwen3":
        item = _run_qwen3(wav, cfg, device, language, log, progress)
    else:
        _emit(progress, 12, "识别中")
        item = _run_funasr(wav, cfg, hotword, device, log, progress)
    item["_cost_s"] = round(time.time() - t1, 1)
    return item


def _run_funasr(wav: Path, cfg: dict, hotword: str, device: str,
                log=print, progress=None) -> dict:
    _emit(progress, 11, "加载识别模型")
    model = get_model(cfg, device)
    gen = dict(batch_size_s=300, disable_pbar=True)
    if cfg.get("hotword") and hotword:
        gen["hotword"] = hotword

    if FUNASR_WORKERS > 1:
        return _run_funasr_parallel(wav, model, gen, log, progress)

    # ASR_FUNASR_WORKERS=1：整段推理，零回归路径。
    # 【新增 v2.4】generate() 是阻塞调用，长课期间进度条会一直停在 12%，
    # 用守护心跳把「已用时长」写进阶段文案；pct 不动，前端不算假 ETA。
    gen["input"] = str(wav)
    stop = threading.Event()
    t0 = time.time()

    def beat():
        while not stop.wait(5):
            _emit(progress, 12, f"整段识别中 · 已用 {int(time.time() - t0)} 秒")

    threading.Thread(target=beat, daemon=True, name="funasr-beat").start()
    try:
        res = model.generate(**gen)
    finally:
        stop.set()
    # modified: 空返回原来会 res[0] IndexError，报错信息完全看不出原因
    if isinstance(res, list):
        if not res:
            raise RuntimeError("识别返回空结果：音频可能是静音或过短（不足 1 秒）")
        return res[0]
    return res


def _run_funasr_parallel(wav: Path, model, gen: dict, log, progress) -> dict:
    """【新增 v2.4】Paraformer 分片并行：低能量处切片、多线程共用一个模型实例。

    为什么不用多实例：实测同一进程创建第 2 个 AutoModel 会原生段错误（exit 139）。
    多线程并发调用同一个实例的 generate() 则安全（基准 2/4 路都跑通）。
    每片独立跑 VAD+标点，时间戳按片偏移拼回，字幕轨仍是「识别原文 + 字级时间戳」，
    没有把任何改写塞进字幕层。代价是切点附近约 1%~3% 的字差异，见模块常量注释。
    """
    import tempfile
    import wave as _wave
    import numpy as np
    import torch
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from qwen_asr.inference.utils import normalize_audio_input, split_audio_into_chunks

    pcm = normalize_audio_input(str(wav))
    parts = split_audio_into_chunks(
        wav=pcm, sr=16000, max_chunk_sec=FUNASR_CHUNK_SEC,
        search_expand_sec=min(30.0, FUNASR_CHUNK_SEC / 6))
    total = len(parts)
    workers = min(FUNASR_WORKERS, total)
    # 2 路各 8 线程 / 4 路各 4 线程，与基准一致；核多了只会互相抢。
    torch.set_num_threads(max(2, min(8, 16 // workers)))
    log(f"Paraformer：{len(pcm) / 16000 / 60:.1f} 分钟切 {total} 片，{workers} 路并行")

    def write_part(chunk, path):
        with _wave.open(str(path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes((np.clip(chunk, -1, 1) * 32767).astype("<i2").tobytes())

    results = [None] * total
    done = [0]
    lock = threading.Lock()

    def one(i, chunk, offset):
        off_ms = int(round(offset * 1000))
        with tempfile.TemporaryDirectory(prefix="fasr_") as td:
            p = Path(td) / f"p{i}.wav"
            write_part(chunk, p)
            res = model.generate(input=str(p), **gen)
        d = res[0] if isinstance(res, list) and res else {}
        sents = []
        for s in (d.get("sentence_info") or []):
            s = dict(s)
            s["start"] = int(s.get("start") or 0) + off_ms
            s["end"] = int(s.get("end") or 0) + off_ms
            sents.append(s)
        with lock:
            done[0] += 1
            # 每片完成就上报，长课进度条真的会走，不再停在 12%
            _emit(progress, 12 + 78 * done[0] / total, f"识别 {done[0]}/{total}")
        return {
            "text": str(d.get("text") or ""),
            "timestamp": [[int(a) + off_ms, int(b) + off_ms]
                          for a, b in (d.get("timestamp") or [])],
            "sentence_info": sents,
        }

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="funasr") as ex:
        futs = {ex.submit(one, i, c, o): i for i, (c, o) in enumerate(parts)}
        for f in as_completed(futs):
            results[futs[f]] = f.result()      # 异常在这里抛出，别静默吞掉

    _emit(progress, 90, "识别完成")
    return {
        "text": "".join(r["text"] for r in results),
        "timestamp": [x for r in results for x in r["timestamp"]],
        "sentence_info": [s for r in results for s in r["sentence_info"]] or None,
    }


_QWEN3_CACHE = {}

# 【新增 v1.9】长音频必须自己切片、逐片推理。
# 事故现场：79 分钟课程整段交给 model.transcribe()，库内部按 180s 切成 27 片后
# 「整批」送进前向，单次 logits 就几十 GB，进程直接段错误（exit 139）消失，
# Python 侧一个异常都拿不到——任务状态永远停在 running。
# 另一个坑：库默认 max_new_tokens=512，170s 的中文片段约 700 字，会被静默截断。
# 窗口还要夹在安全区间：库的切片会在「目标切点 ±search_expand」里找最低能量处下刀，
# 窗口小于 2×search_expand 时切点会落回起点附近，每轮只推进 1 个采样点（实测把
# 12s 全静音音频切成十几万片、内存涨到被系统杀掉），所以窗口下限设 30s、
# 搜索半径压到窗口的 1/6。
QWEN3_WINDOW_SEC = min(175.0, max(30.0, float(os.environ.get("ASR_QWEN3_WINDOW") or 170)))
QWEN3_EXPAND_SEC = min(5.0, QWEN3_WINDOW_SEC / 6.0)
QWEN3_MAX_NEW_TOKENS = int(os.environ.get("ASR_QWEN3_MAXTOK") or 2048)

# 【新增 v2.1】分片并行度。基准实测（20 核 / 240 秒真实课程，两片 120s）：
#   顺序、torch 20 线程  125s，整机 CPU 100%；
#   顺序、torch 10 线程  134s（更慢，说明减线程没用）；
#   两片并行、各 8 线程  99s（1.26×），内存 7.8GB → 9.5GB（模型权重共享）。
# 所以「CPU 还有余量」的真相是：单步自回归解码喂不满核心，多片并行才能榨出吞吐。
# 并发每 +1 约多 2~6GB 内存，默认 2 是 63GB 机器上的稳妥值；内存紧张设 1。
QWEN3_WORKERS = min(4, max(1, int(os.environ.get("ASR_QWEN3_WORKERS") or 2)))

# 【新增 v2.4】Paraformer 分片并行。基准实测（20 核 / 300 秒真实课程，单模型实例）：
#   单路 4 线程 17.5s ｜ 单路 20 线程 16.5s（只快 6%，把核塞满没用）
#   2 路并行 8.7s（2.01×，文本一致率 98.9%）
#   4 路并行 6.2s（2.82×，文本一致率 96.8%）
# 同进程创建多个 AutoModel 实例会原生段错误（exit 139），所以只用一个实例
# + 多线程并发调用 generate()；片在低能量处切，时间戳按片偏移拼回。
# 代价：切点附近有约 1%~3% 的字差异；追求逐字精确设 ASR_FUNASR_WORKERS=1
# 即可回到「整段推理」的零回归路径。
FUNASR_WORKERS = min(4, max(1, int(os.environ.get("ASR_FUNASR_WORKERS") or 2)))
FUNASR_CHUNK_SEC = min(1800.0, max(120.0,
                                  float(os.environ.get("ASR_FUNASR_CHUNK") or 600)))
QWEN3_THREADS = max(4, min(8, (os.cpu_count() or 8) // 2))


def _resolve_ms_dir(model_id: str) -> str:
    """把 ModelScope 模型 ID 解析成本地目录；已缓存则秒回，不重复下载"""
    try:
        from modelscope import snapshot_download
        return snapshot_download(model_id, cache_dir=str(MODEL_ROOT / "modelscope"))
    except Exception:
        return model_id


def _run_qwen3(wav: Path, cfg: dict, device: str, language: str, log, progress=None) -> dict:
    """Qwen3-ASR：LLM 架构精度高，字级时间戳由强制对齐模型给出"""
    import torch
    from qwen_asr import Qwen3ASRModel

    model = _QWEN3_CACHE.get(cfg["model"])
    if model is None:
        log(f"加载 {cfg['model']}（本地缓存优先）")
        t0 = time.time()
        asr_dir = _resolve_ms_dir(cfg["model"])
        aligner_dir = _resolve_ms_dir(cfg["aligner"]) if cfg.get("aligner") else None
        dtype = getattr(torch, cfg.get("dtype", "float32"))
        model = Qwen3ASRModel.from_pretrained(
            asr_dir,
            dtype=dtype,
            device_map=device,
            forced_aligner=aligner_dir,
            forced_aligner_kwargs=dict(dtype=dtype, device_map=device),
            # modified(v1.9): 库默认批大小 32，长音频会整批前向吃爆内存；
            # 同时 max_new_tokens 默认 512，170s 的片段约 700 字会被截断。
            max_inference_batch_size=1,
            max_new_tokens=QWEN3_MAX_NEW_TOKENS,
        )
        _QWEN3_CACHE[cfg["model"]] = model
        log(f"模型就绪，耗时 {time.time() - t0:.1f}s")

    lang = None if language in (None, "", "auto") else language

    # 【修改 v1.9】原来把整段音频一次性交给库：model.transcribe(audio=str(wav))。
    # 现在按低能量边界切成 QWEN3_WINDOW_SEC 的片，逐片识别后拼回，内存恒定在单片水平。
    from qwen_asr.inference.utils import normalize_audio_input, split_audio_into_chunks

    pcm = normalize_audio_input(str(wav))
    parts = split_audio_into_chunks(wav=pcm, sr=16000, max_chunk_sec=QWEN3_WINDOW_SEC,
                                    search_expand_sec=QWEN3_EXPAND_SEC)
    total = len(parts)
    workers = QWEN3_WORKERS if total > 1 else 1
    if workers > 1:
        # 并行时把每个算子的线程数压到 8：实测 2 路×8 线程比 2 路×20 线程快，
        # 否则 40 个线程抢 20 个核，光上下文切换就把收益吃回去。
        import torch
        torch.set_num_threads(QWEN3_THREADS)
    log(f"音频 {len(pcm) / 16000 / 60:.1f} 分钟，切 {total} 片"
        + (f"，{workers} 路并行识别" if workers > 1 else "逐片识别"))

    def one(i, chunk, offset):
        """识别一片，返回（片号、文本、字级时间戳）。失败重试一次。"""
        r = None
        for attempt in (1, 2):
            try:
                res = model.transcribe(audio=(chunk, 16000), language=lang,
                                       return_time_stamps=True)
                r = res[0] if res else None
                break
            except Exception as exc:
                # 单片失败不拖垮整段：先重试一次，仍失败才带位置报错，
                # 免得 70 分钟的活干到第 20 片白跑一场还看不出卡在哪。
                if attempt == 2:
                    raise RuntimeError(
                        f"第 {i + 1}/{total} 片（{offset / 60:.1f} 分钟处）识别失败：{exc}"
                    ) from exc
                log(f"第 {i} 片出错，重试一次：{exc}")
        text = ((getattr(r, "text", "") or "") if r else "").strip()
        stamps = (getattr(r, "time_stamps", None) or []) if r else []
        return i, text, stamps

    results = [None] * total
    _emit(progress, 12, f"识别 0/{total}")
    if workers > 1:
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        lock, done = threading.Lock(), [0]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qwen3") as ex:
            futs = [ex.submit(one, i, ch, off)
                    for i, (ch, off) in enumerate(parts)]
            for f in as_completed(futs):
                i, text, stamps = f.result()      # 有异常就在这里抛，带片号
                results[i] = (text, stamps)
                with lock:
                    done[0] += 1
                    _emit(progress, 12 + 78 * done[0] / total, f"识别 {done[0]}/{total}")
                log(f"识别 {i + 1}/{total}：{len(text)} 字")
    else:
        for i, (chunk, offset) in enumerate(parts):
            _emit(progress, 12 + 78 * i / total, f"识别 {i + 1}/{total}")
            _, text, stamps = one(i, chunk, offset)
            results[i] = (text, stamps)
            log(f"识别 {i + 1}/{total}（{offset / 60:.1f} 分钟）：{len(text)} 字")
            _emit(progress, 12 + 78 * (i + 1) / total, f"识别 {i + 1}/{total}")

    # 按片号顺序拼回，绝不按完成顺序——否则字幕时间轴会整段错乱
    texts, ts, silent = [], [], 0
    for i, (text, stamps) in enumerate(results):
        off_ms = int(round(parts[i][1] * 1000))
        if not text:
            silent += 1
        texts.append(text)
        ts.extend([int(t.start_time * 1000) + off_ms, int(t.end_time * 1000) + off_ms]
                  for t in stamps)
    _emit(progress, 90, "识别完成")

    full = "".join(texts).strip()
    if not full:
        raise RuntimeError("识别返回空结果：音频可能是静音或过短")
    tail = f"，其中 {silent} 片静音" if silent else ""
    log(f"转写 {len(full)} 字，对齐 {len(ts)} 个字{tail}")
    return {"text": full, "timestamp": ts}



_MODEL_CACHE = {}


def get_model(cfg: dict, device: str):
    """模型只加载一次，批处理时省下每个文件 30 秒的加载时间"""
    from funasr import AutoModel

    key = (cfg["model"], device)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]

    kwargs = dict(model=cfg["model"], vad_model=cfg["vad"],
                  device=device, disable_update=True, log_level="ERROR")
    if cfg.get("punc"):
        kwargs["punc_model"] = cfg["punc"]

    print(f"  加载模型 {cfg['model']}（首次运行需从 ModelScope 下载）")
    t0 = time.time()
    model = AutoModel(**kwargs)
    print(f"  模型就绪，耗时 {time.time() - t0:.1f}s")
    _MODEL_CACHE[key] = model
    return model


# ────────────────────────── 输出 ──────────────────────────

def write_outputs(item: dict, stem: str, outdir: Path, meta: dict) -> dict:
    text = (item.get("text") or "").strip()

    # 优先用 sentence_info（挂了说话人分离时才有），否则靠字级 timestamp 对齐
    sents = item.get("sentence_info")
    if sents:
        sentences = [{"text": (s.get("text") or "").strip(),
                      "start": s.get("start", 0),
                      "end": s.get("end", 0)} for s in sents]
    else:
        sentences = align_sentences(text, item.get("timestamp") or [])
    if not sentences:
        sentences = [{"text": text, "start": 0, "end": 0}]

    raw_txt = outdir / f"{stem}.raw.txt"
    raw_txt.write_text("\n".join(s["text"] for s in sentences), encoding="utf-8")

    cues = cues_with_time(sentences)
    srt_lines = []
    for i, (st, et, body) in enumerate(cues, 1):
        srt_lines.append(f"{i}\n{fmt_ts(st)} --> {fmt_ts(et)}\n{body}\n")
    raw_srt = outdir / f"{stem}.raw.srt"
    raw_srt.write_text("\n".join(srt_lines), encoding="utf-8")

    # modified: 落盘字级时间戳，文稿对齐（align.py）直接复用，不必重跑模型
    payload = {"meta": meta, "text": text, "sentences": sentences,
               "timestamps": item.get("timestamp") or []}
    # modified(v1.2): 原子写。这个 json 同时是 unique_stem 去重和文稿对齐的
    # 依据，半截文件会让两者悄悄退化（误覆盖旧产物 / 对齐报「没时间戳」）。
    meta_path = outdir / f"{stem}.json"
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, meta_path)

    return {"cues": len(cues), "chars": len(text),
            "srt": raw_srt, "txt": raw_txt, "json": outdir / f"{stem}.json"}


# ────────────────────────── 主流程 ──────────────────────────

def run_pipeline(src: Path, model_key: str = "paraformer", do_clean: bool = True,
                 outdir=None, device: str = "cpu", hotword_file=None,
                 language: str = "Chinese", log=print,
                 provider_key: str = None, use_script_track: bool = False,
                 itn: bool = True, termfix: bool = True,
                 termfix_auto: bool = True, progress=None) -> dict:
    """完整流程，命令行与图形界面共用。返回结构化结果。

    【修改 v1.3】新增 provider_key / use_script_track 两个可选参数，默认 None/False
    时行为与 v1.2 **完全一致**（仍走本地 MODELS 识别 + 原有口语清理）。
    【新增 v1.3】provider_key 指定云端源时走 asr_providers 抽象层，识别结果拆成
    双轨：字幕轨仍是原文+时间戳（SRT/VTT/对齐不变），整理稿另存 script.txt，
    **不参与字幕**，避免时间轴被改写打断（align.py:159）。
    """
    outdir = Path(outdir) if outdir else (BASE / "out")
    outdir.mkdir(parents=True, exist_ok=True)
    cfg = MODELS[model_key]

    hotword = load_hotwords(Path(hotword_file) if hotword_file else HOTWORD_FILE)
    if hotword and cfg.get("hotword"):
        log(f"热词表：{len(hotword.split()) // 2} 条")
    elif hotword and not cfg.get("hotword"):
        log(f"提示：{model_key} 不支持热词偏置，术语改由错词校正表兜底")

    log(f"处理：{src.name}")
    stem = unique_stem(src, outdir)          # modified: 同名文件不再互相覆盖
    _emit(progress, 4, "抽取音频")
    wav = ensure_wav(src, outdir, stem)
    _emit(progress, 10, "音频就绪")
    t0 = time.time()

    # 【新增 v1.3】provider 分支。默认（provider_key 为空或本地）保持原调用，
    # 一行都不改；云端才引入 asr_providers，本地用户零回归。
    import asr_providers as P               # 【新增 v1.3】
    # 【新增 v1.3】未显式指定时按环境变量选源，但云端没配密钥则自动回落本地，
    # 避免「开了开关却没凭据」让日常转写全线失败；显式指定云端则明确报错。
    prov = (P.preferred_from_env() if provider_key is None
            else P.get_provider(provider_key))
    tracks = None
    if prov.kind == "cloud":
        log(f"识别源：{prov.name}（云端）")
        _emit(progress, 15, f"云端识别（{prov.name}）")
        tracks = prov.transcribe(wav, language=language,
                                 hotwords=None, log=log)
        item = {"text": tracks.subtitle_text,
                "timestamp": tracks.timestamps,
                "sentence_info": tracks.sentences or None}
        # 【新增 v1.3】云端自带整理稿时，本地 101 词清理默认降级为兜底，
        # 否则「云端顺滑 + 本地再删一遍」会双重改写。用户仍可显式勾选。
        if tracks.has_script and use_script_track:
            do_clean = False
            log("云端已提供整理稿，跳过本地口语清理（避免双重改写）")
        _emit(progress, 88, "云端识别完成")
    else:
        item = transcribe(wav, cfg, hotword, device, language, log, progress)
    dur = time.time() - t0

    meta = {
        "source": str(src),
        "model": cfg["model"],
        "device": device,
        "elapsed_s": item.get("_cost_s", round(dur, 1)),
        "hotwords": len(hotword.split()) // 2 if hotword else 0,
        "provider": prov.id,                 # 【新增 v1.3】
    }
    _emit(progress, 92, "写入字幕")
    out = write_outputs(item, stem, outdir, meta)

    # 【新增 v1.3】双轨产物落盘。本地源也写一份（讲稿轨为空），前端逻辑统一。
    if tracks is None:
        payload = json.loads((outdir / f"{stem}.json").read_text(encoding="utf-8"))
        tracks = P.Tracks(
            provider=prov.id,
            subtitle_text=payload.get("text") or "",
            timestamps=payload.get("timestamps") or [],
            sentences=payload.get("sentences") or [],
            script_text="", script_source="",
            notes=["本地识别，未启用云端整理稿"],
        )
    tracks_path = outdir / f"{stem}.tracks.json"
    tmp = tracks_path.with_suffix(".json.tmp")
    tmp.write_text(tracks.to_json(), encoding="utf-8")
    os.replace(tmp, tracks_path)

    # 【新增 v1.3】讲稿轨单独成文，供七遍改稿直接取用；没有整理稿时不生成。
    script_txt = outdir / f"{stem}.script.txt"
    if tracks.has_script:
        tmp = script_txt.with_suffix(".txt.tmp")
        tmp.write_text(tracks.script_text, encoding="utf-8")
        os.replace(tmp, script_txt)
        log(f"整理稿已另存：{script_txt.name}（不影响字幕时间轴）")
    log(f"✓ {out['cues']} 条字幕 / {out['chars']} 字，识别耗时 {meta['elapsed_s']}s")

    result = {
        "stem": stem,
        "cues": out["cues"],
        "chars": out["chars"],
        "elapsed": meta["elapsed_s"],
        "srt_path": out["srt"],
        "txt_path": out["txt"],
        "json_path": out["json"],
        "raw_srt_text": out["srt"].read_text(encoding="utf-8"),
        "doc_text": out["txt"].read_text(encoding="utf-8"),
        "clean_srt_path": None,
        "clean_txt_path": None,
        "report_path": None,
        "clean_srt_text": "",
        "clean_doc_text": "",
        "report_text": "",
        # 【新增 v1.3】双轨字段；本地源时 script_text 为空、has_script False，
        # 前端按钮据此禁用，界面与 v1.2 一致。
        "provider": prov.id,
        "script_text": tracks.script_text,
        "has_script": tracks.has_script,
        "script_source": tracks.script_source,
        "tracks_path": tracks_path,
        "script_txt_path": script_txt if tracks.has_script else None,
    }

    if do_clean:
        _emit(progress, 95, "口语清理")
        import clean_transcript as ct
        cleaner = ct.Cleaner(ct.load_rules(BASE / "rules" / "filler_words.json"),
                             ct.load_corrections(BASE / "terms" / "corrections.txt"),
                             itn=itn,            # 【新增 v1.4】受限数字规整
                             termfix=termfix,     # 【新增 v1.5】简称膨胀 + 漏字补全
                             termfix_auto=termfix_auto)

        # 文档走句子级（不按字幕断行，读起来是完整句子）
        doc, rows = [], []
        for ln in out["txt"].read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            new, changes, warns = cleaner.clean(ln)
            if new.strip():
                doc.append(new)
            if changes or warns:
                rows.append((ln, new, changes, warns))
        clean_txt = outdir / f"{stem}.clean.txt"
        clean_txt.write_text("\n".join(doc), encoding="utf-8")

        # 字幕走 cue 级
        cues_in = ct.parse_srt(out["srt"].read_text(encoding="utf-8"))
        kept, cue_changes = [], []      # 【新增 v1.6】逐条变更，供界面做 diff 预览
        for c in cues_in:
            orig = " ".join(c["lines"])
            new, chg, _ = cleaner.clean(orig)
            if new.strip("，。、；：？！,.;:?! "):
                kept.append({"idx": c["idx"], "time": c["time"], "lines": [new]})
                if chg:
                    cue_changes.append({
                        "idx": c["idx"], "before": orig, "after": new,
                        "changes": [f"{a}·{b}" for a, b, _ in chg],
                    })
        clean_srt = outdir / f"{stem}.clean.srt"
        clean_srt.write_text(ct.build_srt(kept), encoding="utf-8")

        rep = [f"# 清洗报告 · {src.name}", "",
               f"- 原始字数：{out['chars']}",
               f"- 清洗后字数：{sum(len(x) for x in doc)}",
               f"- 改动句子：{len(rows)}", "",
               "## 逐条变更", "",
               "| 原文 | 清洗后 | 变更 | 告警 |", "|---|---|---|---|"]
        for orig, new, changes, warns in rows:
            c = "；".join(f"{a[0]}:{a[1]}" for a in changes) or "—"
            w = "；".join(warns)
            esc = lambda s: str(s).replace("|", "\\|")
            rep.append(f"| {esc(orig)} | {esc(new)} | {esc(c)} | {esc(w)} |")
        report = outdir / f"{stem}.clean.report.md"
        report.write_text("\n".join(rep), encoding="utf-8")

        result.update({
            "clean_srt_path": clean_srt, "clean_txt_path": clean_txt,
            "report_path": report,
            "clean_srt_text": clean_srt.read_text(encoding="utf-8"),
            "clean_doc_text": "\n".join(doc),
            "report_text": "\n".join(rep),
            # 【新增 v1.6】逐条变更（原文 / 清理后 / 变更类型），界面据此做 diff 预览
            "cue_changes": cue_changes,
        })
        log(f"口语清理完成：{len(cues_in)}→{len(kept)} 条")

    result["outdir"] = outdir
    _emit(progress, 100, "完成")
    return result


def process(src: Path, args, outdir: Path, hotword: str):
    print(f"\n▶ {src.name}")
    r = run_pipeline(src, args.model, args.clean, outdir, args.device,
                     args.hotword_file, getattr(args, "language", "Chinese"),
                     log=lambda m: print(f"  {m}"),
                     # 【新增 v1.3】CLI 也能指定识别源；不传则保持原行为
                     provider_key=getattr(args, "provider", None),
                     use_script_track=getattr(args, "use_script_track", False),
                     # 【新增 v1.4】数字规整默认开，--no-itn 可关
                     itn=not getattr(args, "no_itn", False),
                     # 【新增 v1.5】术语修复默认开；--no-termfix 全关，
                     # --termfix-suggest-only 只建议不改
                     termfix=not getattr(args, "no_termfix", False),
                     termfix_auto=not getattr(args, "termfix_suggest_only", False))
    print(f"    → {r['srt_path'].name}")


def main():
    ap = argparse.ArgumentParser(description="网课语音转字幕/文稿")
    ap.add_argument("-i", "--input", required=True, help="音频/视频文件或目录")
    ap.add_argument("-o", "--outdir", default=str(BASE / "out"))
    ap.add_argument("--model", choices=list(MODELS), default="qwen3-asr",
                    help="默认 qwen3-asr（精度优先）；要快要热词选 paraformer")
    ap.add_argument("--device", default="cpu", help="cpu 或 cuda:0")
    ap.add_argument("--language", default="Chinese",
                    help="语种提示：Chinese / auto（仅 Qwen3-ASR 生效）")
    ap.add_argument("--hotword-file", default=str(HOTWORD_FILE))
    ap.add_argument("--clean", action="store_true", help="识别后立即清理口语")
    # 【新增 v1.3】识别源与双轨整理稿开关（默认仍是本地 Qwen3，零回归）
    ap.add_argument("--provider", default=None,
                    help="识别源：local-qwen3(默认)/local-paraformer/"
                         "tencent-cloud/volcengine-doubao")
    ap.add_argument("--use-script-track", action="store_true",
                    help="云端自带整理稿时跳过本地口语清理，避免双重改写")
    # 【新增 v1.4】数字规整（默认开）：百分之九十八点五 → 98.5%；
    # 条款号（第一百二十条）与监管术语（四不两直）永不改动
    ap.add_argument("--no-itn", action="store_true",
                    help="关闭数字规整，保留纯汉字数字写法")
    # 【新增 v1.5】术语修复：简称膨胀（GSP→全称）+ 漏字补全
    ap.add_argument("--no-termfix", action="store_true",
                    help="关闭术语修复（不做简称膨胀与漏字补全）")
    ap.add_argument("--termfix-suggest-only", action="store_true",
                    help="术语修复只出建议不改原文（保守模式）")
    ap.add_argument("--list-models", action="store_true")
    args = ap.parse_args()

    if args.list_models:
        for k, v in MODELS.items():
            print(f"{k:12s} {v['note']}")
        return

    src = Path(args.input)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    hotword = load_hotwords(Path(args.hotword_file))
    if hotword:
        print(f"热词表：{len(hotword.split()) // 2} 条")

    if src.is_dir():
        files = sorted(p for p in src.iterdir() if p.suffix.lower() in MEDIA_EXT)
        if not files:
            sys.exit(f"目录内没有可处理的音视频：{src}")
        print(f"批处理：{len(files)} 个文件")
        for f in files:
            try:
                process(f, args, outdir, hotword)
            except Exception as e:
                print(f"  ✗ 失败：{e}")
    elif src.is_file():
        # modified(v1.2): ensure_wav 改成抛 RuntimeError 后，单文件 CLI 也要
        # 给友好提示，保持原有「不弹 traceback」的调用体验。
        try:
            process(src, args, outdir, hotword)
        except Exception as e:
            sys.exit(f"✗ 失败：{e}")
    else:
        sys.exit(f"路径不存在：{src}")


if __name__ == "__main__":
    main()
