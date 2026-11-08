# -*- coding: utf-8 -*-
"""把一批本地课件加进夜间转写队列（命令行 / 计划任务用）。

大视频不重复拷贝：同一块盘优先建 NTFS 硬链接（秒级、零额外空间），
跨盘才退回普通复制。文件落到 media/inbox 后由服务的「文件夹监控」或
/api/queue/scan 接管，识别源、参数、跳过已转录等规则与网页端完全一致。

用法：
  python batch_add.py "D:\\课程\\*.mp4" "E:\\录音\\第一节.m4a"
  python batch_add.py "D:\\课程" --at "22:30"        # 今晚 22:30 开跑
  python batch_add.py "D:\\课程" --at "+60"           # 60 分钟后开跑
  python batch_add.py --status                        # 只看队列状态
  python batch_add.py --scan                         # 只扫描 inbox，不加文件
"""
import argparse
import datetime as dt
import glob
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
INBOX = BASE / "media" / "inbox"
API = os.environ.get("ASR_API", "http://127.0.0.1:7870")
EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
       ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".wma"}


def call(path, payload=None, method=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"连不上服务 {API}：{exc}\n"
                         f"请先用「启动界面.bat」或「服务台」把服务开起来。")


def collect(patterns):
    out = []
    for pat in patterns:
        p = Path(pat)
        if p.is_dir():
            found = [x for x in sorted(p.iterdir())
                     if x.is_file() and x.suffix.lower() in EXT]
            if not found:
                print(f"  目录里没有可识别的音视频：{p}")
            out.extend(found)
        elif any(c in pat for c in "*?["):
            out.extend(Path(x) for x in sorted(glob.glob(pat))
                       if Path(x).suffix.lower() in EXT)
        elif p.is_file() and p.suffix.lower() in EXT:
            out.append(p)
        else:
            print(f"  跳过（不是音视频或不存在）：{pat}")
    # 去重，保持顺序
    seen, uniq = set(), []
    for x in out:
        k = str(x.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def place(src: Path):
    """同盘硬链接、跨盘复制；inbox 已有同名同大小的直接跳过。"""
    INBOX.mkdir(parents=True, exist_ok=True)
    dst = INBOX / src.name
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return False, "已存在"
    if dst.exists():
        dst = INBOX / f"{src.stem}_{int(time.time()) % 100000}{src.suffix}"
    try:
        os.link(src, dst)
        return True, "硬链接"
    except OSError:
        shutil.copy2(src, dst)
        return True, "复制"


def parse_at(s):
    if not s:
        return None
    s = s.strip()
    if s.startswith("+"):
        return time.time() + float(s[1:]) * 60
    now = dt.datetime.now()
    if ":" in s and len(s) <= 5:
        h, m = map(int, s.split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += dt.timedelta(days=1)
        return t.timestamp()
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m-%d %H:%M"):
        try:
            t = dt.datetime.strptime(s, fmt)
            if t.year == 1900:
                t = t.replace(year=now.year)
            return t.timestamp()
        except ValueError:
            continue
    raise SystemExit(f"看不懂时间：{s}（可用 22:30 / +60 / 2026-11-09 22:30）")


def main():
    ap = argparse.ArgumentParser(description="把一批课件加进夜间转写队列")
    ap.add_argument("paths", nargs="*", help="文件、目录或通配符")
    ap.add_argument("--at", default=None, help="定时开始：22:30 / +60 / 2026-11-09 22:30")
    ap.add_argument("--scan", action="store_true", help="只扫描 inbox，不添加文件")
    ap.add_argument("--status", action="store_true", help="只看队列状态")
    ap.add_argument("--dry-run", action="store_true", help="只列出会添加的文件，不实际操作")
    args = ap.parse_args()

    if args.status:
        d = call("/api/queue")
        print(f"队列：排队 {d['counts']['queued']} / 识别中 {d['counts']['running']} / "
              f"已完成 {d['counts']['done']} / 失败 {d['counts']['error']}")
        print(f"暂停={d['paused']}  定时={d['start_at']}  "
              f"自动重试={d['auto_retry']}  文件夹监控={d['watch']}")
        for q in d["queue"]:
            print(f"  {q['position']:>2}. {q['name']}")
        return 0

    added = 0
    if not args.scan:
        files = collect(args.paths)
        if not files:
            print("没有找到可添加的文件。示例：python batch_add.py \"D:\\课程\\*.mp4\"")
            return 1
        print(f"找到 {len(files)} 个文件")
        for f in files:
            if args.dry_run:
                print(f"  将添加：{f}")
                continue
            ok, how = place(f)
            print(f"  {'+' if ok else '='} {f.name}（{how}）")
            added += ok
        if args.dry_run:
            return 0

    d = call("/api/queue/scan", {})
    print(f"\n入队 {d['count']} 个：")
    for x in d["created"]:
        print(f"  · {x['name']}")
    q = call("/api/queue")
    print(f"当前排队 {q['counts']['queued']} 个")
    if args.at:
        ts = parse_at(args.at)
        call("/api/queue", {"start_at": ts, "paused": False})
        print(f"定时开始：{dt.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}"
              f"（到点自动开跑，识别中不会休眠）")
    elif q["paused"]:
        print("注意：队列当前是暂停状态，记得在网页端点「继续队列」，或加 --at 设定时间。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
