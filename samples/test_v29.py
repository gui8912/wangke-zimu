# -*- coding: utf-8 -*-
"""v2.9 专项：端口自动分配、单实例保护、客户端端口发现。

背景：端口写死 7870，上一份没退干净就起不来；改成自动顺延后，又必须保证
①不重复起第二份（共用 out/ 与队列会互相抢任务）；②批处理/测试能跟上实际端口；
③服务台能从日志抓到端口（管道里中文是 GBK，锚点必须是 ASCII）。
"""
import json
import socket
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ports                      # noqa: E402
import batch_add                  # noqa: E402

PASS = FAIL = 0
# 用一段高位端口做实验，别碰正在跑的 7870–7899
TEST_PORT = 29870


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def listen(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def port_basics():
    print("[端口探测]")
    s = listen(TEST_PORT)
    try:
        ok("被监听 → in_use=True", ports.in_use(TEST_PORT))
        ok("被监听 ≠ 本程序", ports.health_of(TEST_PORT) is None)
        picked = ports.pick(TEST_PORT, 5)
        ok("pick 跳过被占端口", picked != TEST_PORT and picked > TEST_PORT, picked)
    finally:
        s.close()
    ok("关闭后 in_use=False", not ports.in_use(TEST_PORT))
    ok("空端口 health=None", ports.health_of(TEST_PORT) is None)


def discover():
    print("[客户端发现]")
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "_port.txt"

    f.write_text("9", encoding="utf-8")          # 关着的端口
    ok("过期端口文件不被轻信", ports.discover(f, TEST_PORT, 3) == 0)

    f.write_text("abc", encoding="utf-8")        # 脏数据
    ok("非法端口文件不抛异常", ports.discover(f, TEST_PORT, 3) == 0)

    if not f.exists():
        f.write_text("", encoding="utf-8")
    ok("discover 找不到返回 0", ports.discover(f, TEST_PORT, 3) == 0)

    # batch_add：环境变量优先
    import os
    old = os.environ.get("ASR_API")
    os.environ["ASR_API"] = "http://127.0.0.1:12345"
    try:
        ok("batch_add 认 ASR_API", batch_add.api_base() == "http://127.0.0.1:12345")
    finally:
        if old is None:
            os.environ.pop("ASR_API", None)
        else:
            os.environ["ASR_API"] = old

    # samples/apibase 也要认环境变量
    sys.path.insert(0, str(BASE / "samples"))
    import apibase
    os.environ["ASR_API"] = "http://127.0.0.1:23456"
    try:
        ok("测试脚本认 ASR_API", apibase.base() == "http://127.0.0.1:23456")
    finally:
        if old is None:
            os.environ.pop("ASR_API", None)
        else:
            os.environ["ASR_API"] = old


def explicit_port():
    print("[ASR_PORT 固定端口]")
    import os
    s = listen(TEST_PORT)
    old = os.environ.get("ASR_PORT")
    os.environ["ASR_PORT"] = str(TEST_PORT)
    try:
        try:
            ports.resolve(Path("unused.txt"), TEST_PORT, 3)
            raised = False
        except SystemExit as exc:
            raised = (exc.code == 2)
        ok("显式端口被别的程序占用 → 退出码 2", raised)
    finally:
        s.close()
        if old is None:
            os.environ.pop("ASR_PORT", None)
        else:
            os.environ["ASR_PORT"] = old

    os.environ["ASR_PORT"] = "abc"
    try:
        try:
            ports.resolve(Path("unused.txt"), TEST_PORT, 3)
            raised = False
        except SystemExit as exc:
            raised = (exc.code == 2)
        ok("非法 ASR_PORT → 退出码 2", raised)
    finally:
        if old is None:
            os.environ.pop("ASR_PORT", None)
        else:
            os.environ["ASR_PORT"] = old


def hub_pattern():
    print("[服务台联动]")
    hub = Path("D:/Tools/服务台/services.json")
    if not hub.exists():
        print("  SKIP 没装服务台，跳过联动检查")
        return
    svc = json.loads(hub.read_text(encoding="utf-8"))
    item = next((s for s in svc.get("services", []) if s.get("id") == "asr-pipeline"), None)
    ok("服务台有网课转字幕条目", item is not None)
    if not item:
        return
    ok("不再写死端口", not item.get("port"))
    ok("不再写死 url", not item.get("url"))
    pat = item.get("urlPattern") or ""
    ok("配置了 urlPattern", bool(pat))
    if pat:
        import re
        m = re.search(pat, "[server] listening on http://127.0.0.1:7873/")
        ok("urlPattern 能抓到端口", bool(m) and m.group(1) == "7873",
           m.group(0) if m else None)


def source_contract():
    print("[源码契约]")
    src = (BASE / "server.py").read_text(encoding="utf-8")
    ok("启动行含 ASCII 锚点", 'listening on http://127.0.0.1:{port}/' in src)
    ok("端口来自 ports.resolve", "ports.resolve(PORT_FILE" in src)
    ok("端口决策在重模块之前", src.index("ports.resolve(PORT_FILE")
       < src.index("import transcribe as T"))
    ok("退出时清理端口文件", "ports.drop_file(PORT_FILE, PORT)" in src)

    pr = (BASE / "ports.py").read_text(encoding="utf-8")
    ok("不设 SO_REUSEADDR（Windows 下会失真）", "setsockopt" not in pr)
    ok("复用已有实例不再起第二份", "不再重复启动" in pr)

    bat = (BASE / "启动网课转字幕.bat").read_bytes()
    ok("bat 仍是纯 ASCII", all(b < 128 for b in bat), max(bat))
    ok("bat 不再宣称固定 7870", b"URL: http://127.0.0.1:7870" not in bat)


def upload_name():
    """长中文名不再变成 xxx.mp4.mp4（v2.9 修复）。

    需要服务在跑；连不上就跳过，不让纯单元测试因没开服务而红。
    """
    print("[长文件名]")
    import sys as _sys
    _sys.path.insert(0, str(BASE / "samples"))
    import apibase
    url = apibase.base()
    import urllib.request
    import uuid
    boundary = "----t" + uuid.uuid4().hex[:8]
    fname = "解析医疗器械生产质量管理规范检查指导原则" * 6 + ".mp3"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{fname}\"\r\nContent-Type: audio/mpeg\r\n\r\n").encode("utf-8")
    body += b"\0" * 2048 + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url + "/api/upload", data=body, method="POST",
                                 headers={"Content-Type":
                                          f"multipart/form-data; boundary={boundary}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:
        print(f"  SKIP 服务未运行（{exc}）")
        return
    name = d.get("name") or ""
    ok("上传成功", bool(d.get("file_id")), d)
    ok("扩展名只有一个", name.endswith(".mp3") and not name.endswith(".mp3.mp3"), name)
    ok("文件名被截到 120 字内", len(name) <= 120, len(name))


def main():
    print("=" * 58)
    port_basics()
    discover()
    explicit_port()
    hub_pattern()
    source_contract()
    upload_name()
    print("=" * 58)
    print(f"{PASS}/{PASS + FAIL} 通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
