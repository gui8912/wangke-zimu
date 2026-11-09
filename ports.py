# -*- coding: utf-8 -*-
"""端口发现与单实例保护（只依赖标准库，导入开销可忽略）。

为什么单独一个模块：server.py 一旦被导入，就会拉起队列线程、监控线程。
如果「先导入再判断端口」，第二次启动的进程会在判断出「已有实例」之前
先把队列 worker 跑起来，有可能抢走一条排队任务再退出，留下一条假 running。
所以端口决策必须在 server.py 导入重模块之前完成（见 server.py 顶部）。

规则（v2.9）：
  1. 设了 ASR_PORT → 严格用它；被别的程序占 → 报错退出（不自作主张换端口，
     否则服务台按固定端口探活会一直显示「启动中」）。
  2. 未设 → 在 7870…7899 里找「本程序」的既存实例：找到就打开页面并退出。
  3. 都没有 → 从 7870 起找第一个能 bind 的端口。
"""
import json
import os
import socket
import sys
import urllib.request
from pathlib import Path

DEFAULT = 7870
SPAN = 30


def in_use(port: int) -> bool:
    """只问操作系统能不能 bind。

    不设 SO_REUSEADDR：Windows 下它会让已被监听的端口也 bind 成功，
    占用探测直接失真。这个坑踩过，别改回去。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def health_of(port: int):
    """端口上跑的是不是本程序？是就返回 /api/health 的 JSON，否则 None。"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=0.6) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if (isinstance(d, dict) and d.get("ok") and "version" in d) else None
    except Exception:
        return None


def find_running(start: int = DEFAULT, span: int = SPAN):
    for p in range(start, start + span):
        d = health_of(p)
        if d:
            return p, d
    return 0, None


def pick(start: int = DEFAULT, span: int = SPAN) -> int:
    for p in range(start, start + span):
        if not in_use(p):
            return p
    # 整段都被占：让系统给一个临时端口，总比起不来强
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_file(path: Path, port: int) -> None:
    """把实际端口落盘，batch_add.py 与测试脚本据此找到服务。"""
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(str(port), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[server] 端口文件写入失败（不影响使用）：{exc}", flush=True)


def drop_file(path: Path, port: int) -> None:
    """正常退出时清掉，避免以后读到已经关掉的端口。"""
    try:
        if path.read_text(encoding="utf-8").strip() == str(port):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _open(url: str) -> None:
    if os.environ.get("ASR_NO_BROWSER"):
        return
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _reuse(port: int, info: dict, path: Path) -> None:
    """已经有一份在跑：不再起第二个（会抢同一份 out/ 与队列），只打开页面。"""
    url = f"http://127.0.0.1:{port}/"
    print(f"[server] 已在运行：{url}（版本 {info.get('version')}），不再重复启动",
          flush=True)
    print(f"[server] listening on http://127.0.0.1:{port}/", flush=True)
    write_file(path, port)
    _open(url)
    sys.exit(0)


def discover(path: Path, start: int = DEFAULT, span: int = SPAN) -> int:
    """给「客户端」用：找正在跑的服务端口，找不到返回 0。

    端口文件可能是上一次被强杀留下的（进程没来得及清理），所以**必须探活**，
    不能见文件就信；探不到就扫一遍常用端口段。
    """
    try:
        port = int(path.read_text(encoding="utf-8").strip())
        if health_of(port):
            return port
    except (OSError, ValueError):
        pass
    port, _ = find_running(start, span)
    return port


def resolve(path: Path, start: int = DEFAULT, span: int = SPAN) -> int:
    """返回本次要监听的端口；已有实例在跑时直接退出进程。"""
    want = (os.environ.get("ASR_PORT") or "").strip()
    if want:
        try:
            port = int(want)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            print(f"[server] ASR_PORT 不是合法端口：{want}", flush=True)
            sys.exit(2)
        info = health_of(port)
        if info:
            _reuse(port, info, path)
        if in_use(port):
            print(f"[server] 端口 {port} 已被其他程序占用。"
                  f"换一个：set ASR_PORT=7900；或不设 ASR_PORT，让程序自动选。",
                  flush=True)
            sys.exit(2)
        return port

    run_port, info = find_running(start, span)
    if run_port:
        _reuse(run_port, info, path)
    return pick(start, span)
