# -*- coding: utf-8 -*-
"""测试脚本共用的服务地址发现。

【新增 v2.9】服务端口不再写死 7870：7870 被占用时 server.py 会自动顺延，
并把实际端口写进 out/_port.txt。测试若继续硬编码，就会「服务明明在跑却全部
SKIP/失败」。优先级：环境变量 ASR_API > 端口文件（探活）> 扫描常用端口 > 7870。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def base() -> str:
    env = (os.environ.get("ASR_API") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        import ports
        port = ports.discover(ROOT / "out" / "_port.txt")
        if port:
            return f"http://127.0.0.1:{port}"
    except Exception:
        pass
    return "http://127.0.0.1:7870"
