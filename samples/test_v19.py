# -*- coding: utf-8 -*-
"""v1.9 长音频段错误修复专项回归。

背景：79 分钟课程整段交给 Qwen3-ASR，库内部按 180s 切成 27 片后整批前向，
内存爆掉、进程段错误（exit 139），Python 侧连异常都拿不到。修复方式是
自己切片、逐片识别、时间戳加偏移拼回。

本测试用假模型验证「切片—偏移—拼接—重试—空片」这一整套控制流，
不加载任何真实模型，秒级完成。

运行：python samples/test_v19.py
"""
import importlib
import os
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import transcribe as T  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")


def make_wav(path: Path, sec: float, sr: int = 16000, amp: float = 0.3):
    """生成 16k 单声道正弦波；每 3 秒留 1 秒静音，模拟讲课的自然停顿。"""
    n = int(sec * sr)
    t = np.arange(n, dtype=np.float32) / sr
    wave_f = (amp * 32767 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
    wave_f[(np.arange(n) // sr) % 3 == 2] = 0      # 每 3 秒的最后一秒静音
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(wave_f.tobytes())


class FakeStamp:
    def __init__(self, s, e):
        self.start_time = s
        self.end_time = e


class FakeResult:
    def __init__(self, text, stamps):
        self.text = text
        self.time_stamps = stamps


class FakeModel:
    """第 n 次调用返回「段n。」；fail_times 控制前几次抛错；empty_calls 指定返回空文本的序号。"""

    def __init__(self, fail_times=0, empty_calls=(), stamps=((0.0, 1.0),)):
        self.fail_times = fail_times
        self.empty_calls = set(empty_calls)
        self.calls = 0
        self.stamps = stamps

    def transcribe(self, audio, language=None, return_time_stamps=False):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟内存不足")
        text = "" if self.calls in self.empty_calls else f"段{self.calls}。"
        return [FakeResult(text, [FakeStamp(s, e) for s, e in self.stamps])]


def run_with(model, wav: Path, window=None):
    """把假模型塞进缓存绕开真实加载，跑一遍 _run_qwen3。"""
    old = T.QWEN3_WINDOW_SEC
    if window is not None:
        T.QWEN3_WINDOW_SEC = window
    logs = []
    cfg = {"model": "FAKE/Model", "aligner": "FAKE/Aligner", "dtype": "float32"}
    T._QWEN3_CACHE[cfg["model"]] = model
    try:
        return T._run_qwen3(wav, cfg, "cpu", "Chinese", logs.append), logs
    finally:
        T.QWEN3_WINDOW_SEC = old
        T._QWEN3_CACHE.pop(cfg["model"], None)


def chunk_logs(logs):
    return [m for m in logs if m.startswith("识别 ")]


def unit(tmp: Path):
    print("[单元] 切片 / 偏移 / 拼接 / 重试 / 空片")
    wav = tmp / "long.wav"
    make_wav(wav, 100.0)          # 100 秒，按 40 秒窗口切

    # ① 多片切分：片数由低能量边界决定，只校验结构正确
    m = FakeModel()
    item, logs = run_with(m, wav, window=40.0)
    n = len(chunk_logs(logs))
    ok("切出多片", n >= 2 and m.calls == n, f"片={n} 调用={m.calls}")
    ok("文本按顺序拼接", item["text"] == "".join(f"段{i}。" for i in range(1, n + 1)), item["text"])
    ts = item["timestamp"]
    ok("时间戳数量与片数一致", len(ts) == n, f"{len(ts)} vs {n}")
    ok("首片从 0 开始", ts[0][0] == 0, ts[0])
    ok("相邻片偏移递增", all(ts[i][0] > ts[i - 1][0] for i in range(1, n)), ts)
    ok("偏移量级合理", ts[-1][0] >= 35000, ts[-1])
    ok("日志报时长与片数", any("分钟" in x and "切" in x for x in logs), logs)

    # ② 首片失败一次 → 重试后成功，整体不报错（重试会多一次调用，故只校验片数与总调用数）
    m2 = FakeModel(fail_times=1)
    item2, logs2 = run_with(m2, wav, window=40.0)
    ok("单片失败会重试并继续",
       m2.calls == n + 1 and item2["text"].count("。") == n,
       f"调用={m2.calls} 期望={n + 1} 文本={item2['text']}")
    ok("重试有日志", any("重试" in x for x in logs2), logs2)

    # ③ 某片返回空文本（纯静音）→ 计数但整体可用
    m3 = FakeModel(empty_calls=(1,))
    item3, logs3 = run_with(m3, wav, window=40.0)
    ok("空片不中断", item3["text"].startswith("段2。"), item3["text"])
    ok("空片有计数日志", any("片静音" in x for x in logs3), logs3)

    # ④ 全空 → 明确报错，而不是交一份空稿
    try:
        run_with(FakeModel(empty_calls=range(1, 20)), wav, window=40.0)
        ok("全空要报错", False)
    except RuntimeError as exc:
        ok("全空要报错", "空结果" in str(exc), exc)

    # ⑤ 持续失败 → 报错必须带片号，否则不知道卡在哪
    try:
        run_with(FakeModel(fail_times=99), wav, window=40.0)
        ok("持续失败带片号", False)
    except RuntimeError as exc:
        # v2.1 起分片可并行，最先抛错的不一定是第 1 片，只校验「带片号 + 带原因」
        import re as _re
        ok("持续失败带片号", bool(_re.search(rf"第 \d+/{n} 片", str(exc)))
           and "识别失败" in str(exc), exc)

    # ⑥ 短音频（不足一个窗口）走单片，偏移为 0
    item4, _ = run_with(FakeModel(), wav, window=170.0)
    ok("短音频单片", len(item4["timestamp"]) == 1 and item4["timestamp"][0][0] == 0, item4)

    # ⑦ 默认参数护栏
    ok("默认窗口 <180s", 120 <= T.QWEN3_WINDOW_SEC <= 175, T.QWEN3_WINDOW_SEC)
    ok("搜索半径小于窗口一半", T.QWEN3_EXPAND_SEC * 2 < T.QWEN3_WINDOW_SEC, T.QWEN3_EXPAND_SEC)
    ok("默认 max_new_tokens ≥1500", T.QWEN3_MAX_NEW_TOKENS >= 1500, T.QWEN3_MAX_NEW_TOKENS)


def config(tmp: Path):
    print("[配置] 环境变量覆盖与窗口夹取（重载模块，不动全局状态）")

    def reload_with(window, maxtok="2048"):
        os.environ["ASR_QWEN3_WINDOW"] = str(window)
        os.environ["ASR_QWEN3_MAXTOK"] = maxtok
        try:
            return importlib.reload(T)
        finally:
            os.environ.pop("ASR_QWEN3_WINDOW", None)
            os.environ.pop("ASR_QWEN3_MAXTOK", None)

    try:
        T2 = reload_with(95, "3000")
        ok("窗口可配", T2.QWEN3_WINDOW_SEC == 95.0, T2.QWEN3_WINDOW_SEC)
        ok("token 上限可配", T2.QWEN3_MAX_NEW_TOKENS == 3000, T2.QWEN3_MAX_NEW_TOKENS)
        # ① 窗口设太小 → 库的切点会原地打转（实测十几万片、内存爆掉），必须夹到 30s
        ok("过小窗口被夹到 30s", reload_with(5).QWEN3_WINDOW_SEC == 30.0)
        ok("过大窗口被夹到 175s", reload_with(999).QWEN3_WINDOW_SEC == 175.0)
        ok("搜索半径按窗口缩放", abs(reload_with(30).QWEN3_EXPAND_SEC - 5.0) < 1e-6)
    finally:
        importlib.reload(T)


def guards():
    print("[护栏] 建模型参数与崩溃栈记录")
    src = (BASE / "transcribe.py").read_text(encoding="utf-8", errors="replace")
    ok("批大小固定为 1", "max_inference_batch_size=1" in src)
    ok("token 上限显式传入", "max_new_tokens=QWEN3_MAX_NEW_TOKENS" in src)
    srv = (BASE / "server.py").read_text(encoding="utf-8", errors="replace")
    ok("服务开 faulthandler", "faulthandler.enable" in srv)
    ok("崩溃栈落 out/_crash.log", "_crash.log" in srv)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="v19_"))
    unit(tmp)
    config(tmp)
    guards()
    bad = [n for n, c in PASS if not c]
    print(f"\n{len(PASS) - len(bad)}/{len(PASS)} 通过")
    if bad:
        print("失败：" + "、".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
