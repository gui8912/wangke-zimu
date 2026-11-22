#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""网课转字幕 · 环境自检与模型清单校验（v2.12-p8）

只读检查，不改任何业务文件、不启动服务、不加载模型权重。
逐项核对：Python / CPU 指令集 / 内存 / 第三方包 / ffmpeg / 模型清单 /
词表与规则 / 服务端口 / 磁盘 / 云端润色配置。

用法：
    python tools/selfcheck.py                  # 控制台报告 + out/_selfcheck.json
    python tools/selfcheck.py --quiet          # 只打汇总与失败项
    python tools/selfcheck.py --json D:\\x.json  # 自定义报告路径
    python tools/selfcheck.py --hash           # 额外校验 catalog 里已登记的 sha256
    python tools/selfcheck.py --no-service     # 跳过服务端口探测

退出码：有 FAIL → 1；只有 WARN / PASS → 0。
"""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CATALOG = BASE / "assets" / "model-catalog.json"
DEFAULT_REPORT = BASE / "out" / "_selfcheck.json"
VERSION = "2.12-p8"

# 必需：缺了主流程直接不可用；可选：缺了只影响某个功能
DEPS = [
    ("fastapi", True, "HTTP 服务端"),
    ("uvicorn", True, "HTTP 服务端"),
    ("numpy", True, "PCM / 波形计算"),
    ("imageio_ffmpeg", True, "内置 ffmpeg 转码"),
    ("rapidfuzz", True, "文稿对齐 / 术语模糊匹配"),
    ("torch", True, "Qwen3-ASR 推理"),
    ("funasr", True, "Paraformer 识别源"),
    ("modelscope", True, "模型下载与缓存"),
    ("transformers", True, "Qwen3 模型加载"),
    ("qwen_asr", False, "Qwen3-ASR 识别源"),
    ("llama_cpp", False, "本地 GGUF 润色"),
    ("gradio", False, "旧版桌面界面 gui.py"),
]

PROJECT_FILES = [
    "server.py", "transcribe.py", "llm_polish.py", "clean_transcript.py",
    "web/index.html", "terms/hotwords.txt", "terms/corrections.txt",
    "rules/filler_words.json", "assets/model-catalog.json",
]


class Report:
    def __init__(self):
        self.rows = []
        self.models = []
        self.env = {}

    def add(self, level, section, name, detail=""):
        self.rows.append({"level": level, "section": section,
                          "name": name, "detail": detail})
        return level == "PASS"

    def count(self, level):
        return sum(1 for r in self.rows if r["level"] == level)

    @property
    def fail(self):
        return self.count("FAIL")


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def human_gb(n):
    return f"{n / (1024 ** 3):.1f} GB"


# ---------------------------------------------------------------- 环境

def _win_feature(index):
    """Windows 的 IsProcessorFeaturePresent；非 Windows 返回 None。"""
    if os.name != "nt":
        return None
    try:
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(index))
    except Exception:
        return None


def _total_memory():
    if os.name != "nt":
        return 0
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys)
    except Exception:
        pass
    return 0


def check_env(rep):
    v = sys.version_info
    rep.add("PASS" if v >= (3, 10) else "FAIL", "环境",
            f"Python {v.major}.{v.minor}.{v.micro}",
            "" if v >= (3, 10) else "需要 3.10 及以上")
    cpu = os.cpu_count() or 0
    rep.env.update(python=f"{v.major}.{v.minor}.{v.micro}",
                   platform=platform.platform(), cpu_count=cpu)
    rep.add("PASS" if cpu >= 4 else "WARN", "环境",
            f"{platform.system()} {platform.release()} · {platform.machine()} · {cpu} 核",
            "" if cpu >= 4 else "核数偏少，并行识别会很慢")

    avx2 = _win_feature(40)       # PF_AVX2_INSTRUCTIONS_AVAILABLE
    avx512 = _win_feature(41)     # PF_AVX512F_INSTRUCTIONS_AVAILABLE
    rep.env.update(avx2=avx2, avx512=avx512)
    if avx2 is None:
        rep.add("SKIP", "环境", "AVX2 探测", "非 Windows，跳过")
    elif avx2:
        rep.add("PASS", "环境", "AVX2 可用",
                "llama.cpp / CTranslate2 走快速路径" + ("，AVX-512 也可用" if avx512 else ""))
    else:
        rep.add("WARN", "环境", "CPU 不支持 AVX2",
                "llama.cpp 与 CTranslate2 可能直接 0xc000001d 退出；改用云端润色 + FunASR")

    mem = _total_memory()
    if mem:
        rep.env["total_memory_bytes"] = mem
        if mem < 8 * 1024 ** 3:
            rep.add("WARN", "环境", f"内存 {human_gb(mem)}", "低于 8 GB，Qwen3-ASR 容易吃满")
        else:
            rep.add("PASS", "环境", f"内存 {human_gb(mem)}")


# ---------------------------------------------------------------- 依赖

def check_deps(rep):
    for name, required, why in DEPS:
        try:
            found = importlib.util.find_spec(name) is not None
        except Exception:
            found = False
        if found:
            rep.add("PASS", "依赖", f"{name}", why)
        else:
            rep.add("FAIL" if required else "WARN", "依赖", f"{name} 未安装",
                    f"{why}；pip install {name}")


def check_ffmpeg(rep):
    try:
        import imageio_ffmpeg
        exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:
        rep.add("SKIP", "依赖", "ffmpeg 可执行文件", f"imageio_ffmpeg 不可用：{exc}")
        return
    if exe.is_file():
        rep.add("PASS", "依赖", "ffmpeg 可执行文件", f"{fmt_bytes(exe.stat().st_size)} · {exe}")
    else:
        rep.add("FAIL", "依赖", "ffmpeg 可执行文件缺失", str(exe))


# ---------------------------------------------------------------- 模型

def _resolve_model_dir(item, roots):
    """优先用 catalog 登记的 path；找不到再用 find 在模型根下兜底。"""
    p = Path(item.get("path") or "")
    if p.is_dir() and any(p.iterdir()):
        return p, "path"
    pat = item.get("find")
    if not pat:
        return None, ""
    best = None
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        try:
            for cand in rp.rglob(pat):
                if not cand.is_dir():
                    continue
                score = sum(1 for f in item.get("files", [])
                            if (cand / f["path"]).is_file())
                if score and (best is None or score > best[0]):
                    best = (score, cand)
        except OSError:
            continue
    return (best[1], "find") if best else (None, "")


def _sha256(path, chunk=8 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def check_models(rep, do_hash=False):
    if not CATALOG.is_file():
        rep.add("FAIL", "模型", "模型清单缺失", str(CATALOG))
        return
    try:
        cat = json.loads(CATALOG.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        rep.add("FAIL", "模型", "模型清单无法解析", str(exc))
        return
    if cat.get("version") != 1:
        rep.add("WARN", "模型", f"清单版本 {cat.get('version')}", "只认 version=1")
    roots = cat.get("modelRoots") or []
    for item in cat.get("items") or []:
        mid = item.get("id") or "?"
        title = item.get("title") or mid
        required = bool(item.get("required"))
        d, how = _resolve_model_dir(item, roots)
        rec = {"id": mid, "title": title, "required": required,
               "path": str(d) if d else "", "state": "missing",
               "bytes": 0, "files": len(item.get("files") or []),
               "missing": [], "small": [], "drift": False}
        if d is None:
            rec["missing"] = [f["path"] for f in item.get("files") or []]
            rep.models.append(rec)
            rep.add("FAIL" if required else "WARN", "模型", f"{title} 未找到",
                    f"登记位置 {item.get('path')}，模型根 {roots}")
            continue
        total = 0
        for f in item.get("files") or []:
            fp = d / f["path"]
            if not fp.is_file():
                rec["missing"].append(f["path"])
                continue
            size = fp.stat().st_size
            total += size
            low = int(f.get("minBytes") or 0)
            if low and size < low:
                rec["small"].append(f"{f['path']}（{fmt_bytes(size)} < {fmt_bytes(low)}）")
        rec["bytes"] = total
        rec["drift"] = how == "find" and Path(str(d)) != Path(str(item.get("path") or ""))
        if do_hash and item.get("sha256") and not rec["missing"]:
            bad = []
            for name, want in (item.get("sha256") or {}).items():
                fp = d / name
                if not fp.is_file():
                    continue
                got = _sha256(fp)
                if got.lower() != str(want).lower():
                    bad.append(f"{name} 应为 {want[:12]}…，实际 {got[:12]}…")
            rec["hash_bad"] = bad
            if bad:
                rep.models.append(rec)
                rep.add("FAIL", "模型", f"{title} 哈希不符", "；".join(bad))
                continue
        if rec["missing"] or rec["small"]:
            rec["state"] = "bad"
            rep.models.append(rec)
            detail = []
            if rec["missing"]:
                detail.append("缺 " + "、".join(rec["missing"][:4]))
            if rec["small"]:
                detail.append("过小 " + "、".join(rec["small"][:3]))
            rep.add("FAIL" if required else "WARN", "模型", f"{title} 不完整",
                    "；".join(detail))
        else:
            rec["state"] = "ok"
            rep.models.append(rec)
            note = f"{fmt_bytes(total)} · {len(item.get('files') or [])} 个文件"
            if rec["drift"]:
                note += f" · 清单路径已漂移，实际在 {d}"
            rep.add("PASS", "模型", title, note)


# ---------------------------------------------------------------- 项目资产

def _read_text_any(p):
    """先 UTF-8（含 BOM），再 GB18030；返回 (文本, 编码名)。"""
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None, ""


def check_project(rep):
    for rel in PROJECT_FILES:
        p = BASE / rel
        if p.is_file():
            rep.add("PASS", "项目", rel, fmt_bytes(p.stat().st_size))
        else:
            rep.add("FAIL", "项目", f"{rel} 缺失", str(p))

    # 词表编码与条数
    for name in ("hotwords.txt", "corrections.txt", "aliases.txt"):
        p = BASE / "terms" / name
        if not p.is_file():
            continue
        text, enc = _read_text_any(p)
        if text is None:
            rep.add("FAIL", "词表", f"{name} 编码无法识别", "既不是 UTF-8 也不是 GB18030")
            continue
        lines = [ln for ln in text.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        lvl = "PASS" if enc == "utf-8-sig" else "WARN"
        rep.add(lvl, "词表", f"{name} · {len(lines)} 条",
                f"编码 {enc}" + ("" if enc == "utf-8-sig" else "，建议统一 UTF-8"))

    # 用户硬命令：语气词组必须无条件删除「我们」「咱们」
    fp = BASE / "rules" / "filler_words.json"
    if fp.is_file():
        try:
            rules = json.loads(fp.read_text(encoding="utf-8-sig"))
            grp = (rules.get("groups") or {}).get("语气词") or {}
            words = grp.get("words") or []
            has_both = "我们" in words and "咱们" in words
            always = str(grp.get("mode") or "") == "always"
            if has_both and always:
                rep.add("PASS", "规则", "「我们 / 咱们」无条件删除",
                        f"语气词 {len(words)} 个 · mode=always")
            else:
                rep.add("FAIL", "规则", "「我们 / 咱们」硬规则被改动",
                        f"mode={grp.get('mode')} · 含我们={'我们' in words} · 含咱们={'咱们' in words}")
        except Exception as exc:
            rep.add("FAIL", "规则", "filler_words.json 无法解析", str(exc))


# ---------------------------------------------------------------- 服务 / 磁盘 / 配置

def check_service(rep):
    try:
        sys.path.insert(0, str(BASE))
        import ports
        port, info = ports.find_running()
    except Exception as exc:
        rep.add("WARN", "服务", "端口探测失败", str(exc))
        return
    if port:
        rep.add("PASS", "服务", f"正在运行 · 127.0.0.1:{port}",
                f"版本 {info.get('version')}")
        rep.env["service_port"] = port
        rep.env["service_version"] = info.get("version")
    else:
        rep.add("WARN", "服务", "未运行", "自检本身不需要服务；双击「启动网课转字幕.bat」可启动")


def check_disk(rep):
    try:
        usage = shutil.disk_usage(str(BASE))
    except Exception as exc:
        rep.add("WARN", "磁盘", "剩余空间读取失败", str(exc))
        return
    free = usage.free
    rep.env["disk_free_bytes"] = free
    if free < 5 * 1024 ** 3:
        rep.add("FAIL", "磁盘", f"{BASE.drive} 剩余 {human_gb(free)}", "低于 5 GB，转码与识别会中途失败")
    elif free < 20 * 1024 ** 3:
        rep.add("WARN", "磁盘", f"{BASE.drive} 剩余 {human_gb(free)}", "低于 20 GB，建议清理 out/*.16k.wav")
    else:
        rep.add("PASS", "磁盘", f"{BASE.drive} 剩余 {human_gb(free)}")


def check_config(rep):
    cfg_path = BASE / "config" / "cloud_llm.json"
    if not cfg_path.is_file():
        rep.add("WARN", "配置", "cloud_llm.json 不存在", "首次使用云端润色时会自动创建")
        return
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        rep.add("FAIL", "配置", "cloud_llm.json 无法解析", str(exc))
        return
    preset = str(cfg.get("preset") or "")
    enabled = bool(cfg.get("enabled"))
    keys = cfg.get("keys") or {}
    saved = [k for k, v in keys.items() if str(v or "").strip()]
    try:
        sys.path.insert(0, str(BASE))
        import cloud_llm as CL
        known = set(CL.PRESETS)
    except Exception:
        known = set()
    level = "PASS"
    detail = f"预设 {preset or '未设'} · {len(saved)} 个预设已存 Key"
    if known and preset not in known:
        level, detail = "WARN", f"预设 {preset} 不在内置清单里"
    if enabled and preset and preset not in saved:
        level = "WARN"
        detail += "；当前预设没有 Key"
    rep.add(level, "配置", f"润色后端 {'云端' if enabled else '本地'}", detail)
    rep.env["llm_preset"] = preset
    rep.env["llm_keys_saved"] = len(saved)


# ---------------------------------------------------------------- 输出

ORDER = {"FAIL": 0, "WARN": 1, "PASS": 2, "SKIP": 3}


def print_report(rep, quiet=False):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n网课转字幕 · 环境自检   {stamp}")
    print("=" * 62)
    section = None
    for row in rep.rows:
        if quiet and row["level"] not in ("FAIL", "WARN"):
            continue
        if row["section"] != section:
            section = row["section"]
            print(f"[{section}]")
        detail = f"  {row['detail']}" if row["detail"] else ""
        print(f"  {row['level']:<4} {row['name']}{detail}")
    print("-" * 62)
    print(f"合计 {len(rep.rows)} 项：PASS {rep.count('PASS')} · "
          f"WARN {rep.count('WARN')} · FAIL {rep.count('FAIL')} · "
          f"SKIP {rep.count('SKIP')}")


def write_json(rep, path):
    data = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": VERSION,
        "ok": rep.fail == 0,
        "summary": {k.lower(): rep.count(k) for k in ("PASS", "WARN", "FAIL", "SKIP")},
        "env": rep.env,
        "models": rep.models,
        "checks": rep.rows,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="网课转字幕 · 环境自检与模型清单校验")
    ap.add_argument("--json", default=str(DEFAULT_REPORT), help="报告输出路径")
    ap.add_argument("--quiet", action="store_true", help="只显示汇总与失败/警告项")
    ap.add_argument("--hash", action="store_true", help="校验 catalog 里已登记的 sha256")
    ap.add_argument("--no-service", action="store_true", help="跳过服务端口探测")
    args = ap.parse_args(argv)

    t0 = time.time()
    rep = Report()
    check_env(rep)
    check_deps(rep)
    check_ffmpeg(rep)
    check_models(rep, do_hash=args.hash)
    check_project(rep)
    if not args.no_service:
        check_service(rep)
    check_disk(rep)
    check_config(rep)

    print_report(rep, quiet=args.quiet)
    out = write_json(rep, args.json)
    print(f"报告已写入：{out}   （耗时 {time.time() - t0:.1f}s）")
    if rep.fail:
        print("\n有 FAIL 项，先按上面的提示补齐；模型缺失可从「术语表 → 备份」或 ModelScope 重新下载。")
    return 1 if rep.fail else 0


if __name__ == "__main__":
    sys.exit(main())
