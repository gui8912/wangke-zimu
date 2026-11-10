"""Qwen3-ASR-1.7B 在本机 CPU 上的可用性与速度测试（本地模型路径）"""
import os
import sys
import time
import warnings

# 【修改 v2.12】这是「真机探针」不是逻辑回归：要加载 1.7B + 0.6B 权重、
# 占数 GB 内存；与其它进程（例如常驻服务）争内存时可能在加载阶段
# SIGSEGV（exit 139），会把全量回归误判成失败。默认跳过，
# 需要实测速度时单独运行：ASR_TEST_REAL_ASR=1 python samples/test_qwen3.py
if os.environ.get("ASR_TEST_REAL_ASR") != "1":
    print("跳过：真机 ASR 探针（设 ASR_TEST_REAL_ASR=1 单独运行）")
    sys.exit(0)

os.environ["MODELSCOPE_CACHE"] = r"D:\AI\models\modelscope"
os.environ["HF_HOME"] = r"D:\AI\models\huggingface"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
warnings.filterwarnings("ignore")

import torch
from qwen_asr import Qwen3ASRModel

ASR_DIR = r"D:\AI\models\modelscope\Qwen\Qwen3-ASR-1___7B"
ALIGNER_DIR = r"D:\AI\models\modelscope\Qwen\Qwen3-ForcedAligner-0___6B"

AUDIO = "out/seg15.wav"
AUDIO_SEC = 15

t0 = time.time()
print("[load] 加载中（本地路径，无网络）", flush=True)
model = Qwen3ASRModel.from_pretrained(
    ASR_DIR,
    dtype=torch.float32,
    device_map="cpu",
    max_new_tokens=512,
    forced_aligner=ALIGNER_DIR,
    forced_aligner_kwargs=dict(dtype=torch.float32, device_map="cpu"),
)
print(f"[load] {time.time() - t0:.1f}s", flush=True)

t1 = time.time()
res = model.transcribe(audio=AUDIO, language="Chinese", return_time_stamps=True)
cost = time.time() - t1
print(f"[infer] {cost:.1f}s for {AUDIO_SEC}s audio → {AUDIO_SEC / max(cost, 0.01):.2f}x realtime", flush=True)

r = res[0]
print("TEXT:", r.text, flush=True)
ts = getattr(r, "time_stamps", None)
if ts:
    print(f"TS count={len(ts)}", flush=True)
    for item in ts[:10]:
        print(f"  {item.text} {item.start_time:.2f}-{item.end_time:.2f}", flush=True)
else:
    print("NO TIMESTAMPS", flush=True)
