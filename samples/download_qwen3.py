"""从 ModelScope 下载 Qwen3-ASR 与强制对齐模型到 D 盘，返回本地路径"""
import os
import sys

os.environ["MODELSCOPE_CACHE"] = r"D:\AI\models\modelscope"

CACHE = r"D:\AI\models\modelscope"
MODELS = ["Qwen/Qwen3-ASR-1.7B", "Qwen/Qwen3-ForcedAligner-0.6B"]

from modelscope import snapshot_download  # noqa: E402

for mid in MODELS:
    print(f"[下载] {mid}", flush=True)
    try:
        path = snapshot_download(mid, cache_dir=CACHE)
        print(f"  -> {path}", flush=True)
    except Exception as e:
        print(f"  失败：{e}", flush=True)
        sys.exit(1)

print("全部就绪", flush=True)
