# -*- coding: utf-8 -*-
"""v2.4 专项回归：Paraformer 分片并行、多 Key 配置、首页说明书。

运行：python samples/test_v24.py
全部用假模型 / 临时目录，不加载 1GB 权重、不碰真实 config/ 与 out/。
"""
import json
import math
import struct
import sys
import tempfile
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import transcribe as T  # noqa: E402
import cloud_llm as CL  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")


def make_wav(path, sec, sr=16000):
    frames = bytearray()
    for i in range(int(sec * sr)):
        v = 0 if (i // sr) % 5 == 4 else int(0.25 * 32767 * math.sin(2 * math.pi * 180 * i / sr))
        frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


class FakeModel:
    """按 input 文件名里的片号返回固定文本，用来验证顺序与偏移。"""

    def __init__(self):
        self.calls = []

    def generate(self, input=None, **kw):
        idx = int(Path(input).stem.lstrip("p"))
        self.calls.append(idx)
        return [{"text": f"段{idx}。", "timestamp": [[0, 100], [100, 200]],
                 "sentence_info": [{"text": f"句{idx}", "start": 0, "end": 200}]}]


def parallel_unit(tmp):
    print("[并行] Paraformer 分片、偏移、顺序、进度")
    wav = tmp / "clip.wav"
    make_wav(wav, 100)
    old_workers, old_chunk = T.FUNASR_WORKERS, T.FUNASR_CHUNK_SEC
    T.FUNASR_WORKERS, T.FUNASR_CHUNK_SEC = 2, 40.0
    try:
        events, logs = [], []
        m = FakeModel()
        r = T._run_funasr_parallel(wav, m, {}, logs.append,
                                   lambda pct, stage="": events.append((pct, stage)))
        n = len(m.calls)
        ok("切出多片", n >= 2, n)
        ok("文本按原顺序拼接", r["text"] == "".join(f"段{i}。" for i in range(n)), r["text"])
        ts = r["timestamp"]
        ok("时间戳数量 = 片数×2", len(ts) == n * 2, len(ts))
        ok("首片从 0 起", ts[0] == [0, 100], ts[0])
        ok("后续片有偏移", ts[2][0] > 30000, ts[2])
        ok("句级信息也带偏移", r["sentence_info"][1]["start"] > 30000,
           r["sentence_info"])
        ok("逐片上报进度", len(events) >= n and events[-1][0] == 90, events)
        ok("进度单调递增", all(events[i][0] <= events[i + 1][0]
                          for i in range(len(events) - 1)), events)
        ok("日志说明并行", any("并行" in x for x in logs), logs)
    finally:
        T.FUNASR_WORKERS, T.FUNASR_CHUNK_SEC = old_workers, old_chunk

    ok("默认 2 路并行", T.FUNASR_WORKERS == 2, T.FUNASR_WORKERS)
    ok("默认片长 600s", T.FUNASR_CHUNK_SEC == 600.0, T.FUNASR_CHUNK_SEC)
    src = (BASE / "transcribe.py").read_text(encoding="utf-8")
    ok("workers=1 可回退整段", "if FUNASR_WORKERS > 1:" in src)
    ok("入口心跳仍在", "funasr-beat" in src)


def keys_unit(tmp):
    print("[云端] 每个预设独立保存 API Key")
    old = CL.CONFIG
    CL.CONFIG = tmp / "cloud.json"
    try:
        CL.save_config({"enabled": True, "preset": "glm-4-flash",
                        "base_url": "https://open.bigmodel.cn/api/paas/v4",
                        "model": "glm-4-flash", "api_key": "sk-glm-1234567890abcd"})
        CL.save_config({"enabled": True, "preset": "deepseek-v4-flash",
                        "base_url": "https://api.deepseek.com",
                        "model": "deepseek-v4-flash",
                        "api_key": "sk-ds-9876543210efgh"})
        ok("两个 Key 并存", set(CL.status()["keys_saved"]) ==
           {"glm-4-flash", "deepseek-v4-flash"}, CL.status()["keys_saved"])
        back = CL.save_config({"preset": "glm-4-flash",
                               "base_url": "https://open.bigmodel.cn/api/paas/v4",
                               "model": "glm-4-flash"})
        ok("切回带出 GLM Key", back["api_key"] == "sk-glm-1234567890abcd", back["api_key"])
        ok("切换预设不串 Key", CL.apply_preset("custom")["api_key"] == "")
        ok("已存预设显示脱敏", CL.apply_preset("deepseek-v4-flash")["has_key"])
        # 旧格式（无 keys 字段）无损迁移
        CL.CONFIG.write_text(json.dumps({
            "enabled": True, "preset": "glm-4-flash",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4-flash", "api_key": "sk-legacy-1122334455"}),
            encoding="utf-8")
        ok("旧配置自动迁移", CL.load_config()["keys"].get("glm-4-flash")
           == "sk-legacy-1122334455", CL.load_config()["keys"])
    finally:
        CL.CONFIG = old


def long_polish_unit():
    print("[长稿] 自动分块、合并、块尾句补回")
    import llm_polish as LP

    class FakeLLM:
        model_name = "fake"

        def create_chat_completion(self, messages=None, **kw):
            # 模拟「模型把块尾客套话删了」，但保留足够长度：
            # v2.5 起首块有健康检查，固定返回 9 个字会被判「输出过短」而中止，
            # 那是真实保护、不是要测的行为，所以按原文 80% 返回、只去掉最后一行。
            import re as _re
            body = ""
            for m in messages or []:
                if m.get("role") == "user":
                    body = m.get("content") or ""
            hit = _re.search(r"<transcript>\s*(.*?)\s*</transcript>", body, _re.S)
            src = (hit.group(1) if hit else body).strip()
            lines = [x for x in src.splitlines() if x.strip()]
            if len(lines) > 1:
                lines = lines[:-1]
            out = "\n".join(lines)
            if len(out) < len(src) * 0.5:
                out = src[: int(len(src) * 0.8)]
            return {"choices": [{"message": {"content": out},
                                 "finish_reason": "stop"}]}

    old = LP._ensure
    LP._ensure = lambda log=print: FakeLLM()
    try:
        text = "\n".join(f"这是第{i}句关于药品经营质量管理规范的内容。" for i in range(1, 600))
        logs = []
        r = LP.polish(text, log=logs.append)
        ok("万字稿不再被 8000 拦", len(r["source"]) == len(text), len(r["source"]))
        ok("自动切成多块", r["chunks"] > 8, r["chunks"])
        ok("长稿写明自动分块", any("自动分" in x for x in logs), logs[:2])
        ok("块尾句自动补回", r["tail_restored"] > 0, r["tail_restored"])
        ok("返回块大小", r["chunk_chars"] == LP.CHUNK_CHARS)
        ok("整体上限仍兜底", LP.MAX_INPUT >= 200000, LP.MAX_INPUT)
    finally:
        LP._ensure = old


def guards():
    print("[护栏] 首页说明书与源文件禁删")
    web = (BASE / "web/index.html").read_text(encoding="utf-8")
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    manual = BASE / "web" / "说明书.html"
    ok("顶栏有使用说明按钮", 'id="btnManual"' in web)
    ok("说明书静态页存在", manual.exists())
    ok("说明书已更新到 2.4", manual.exists() and "版本 2.4" in manual.read_text(encoding="utf-8"))
    ok("说明书无旧删源表述", manual.exists() and "连源文件一起删" not in manual.read_text(encoding="utf-8"))
    ok("前端无删源复选框", 'id="purgeMedia"' not in web)
    ok("后端强制不删源", "purge = False" in srv)
    ok("生成脚本存在", (BASE / "samples" / "gen_manual.py").exists())
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    web = (BASE / "web/index.html").read_text(encoding="utf-8")
    ok("成稿改为复制文本", ">复制文本</button>" in web and "导出成稿 ▾</button>" not in web)
    ok("复制有兜底", "execCommand('copy')" in web)
    ok("服务端不再拒绝长稿", "请分成两三段分别润色" not in srv)
    ok("服务端按块数放宽卡死判定", "est_chunks * 90" in srv)
    ok("前端按块数放宽超时", "chunks * 90 * 1000" in web)
    corr = (BASE / "terms" / "corrections.txt").read_text(encoding="utf-8")
    hot = (BASE / "terms" / "hotwords.txt").read_text(encoding="utf-8")
    ok("管带→管代已配置", "管带=>管代" in corr)
    ok("管代列入热词", "管代|20" in hot and "管理者代表" in hot)
    ok("用户新增三条规则保留", (BASE / "rules" / "filler_words.json")
       .read_text(encoding="utf-8").count('"我们"') >= 1)
    # 【新增 v2.4 补丁】播放器 / 润色进度 / 零拷贝导入
    ok("上传地址可按 fid 找回", "MEDIA.glob(f\"{fid}_*\")" in srv)
    ok("播放器有错误回退", "el.onerror" in web and "/api/task/" in web)
    ok("润色进度接口已注册", '@app.get("/api/llm/progress")' in srv)
    ok("润色按块回调", "progress(done, total, stage)" in
       (BASE / "llm_polish.py").read_text(encoding="utf-8"))
    ok("前端轮询进度", "startLlmProgress" in web and "/api/llm/progress" in web)
    ok("卡死判定用当前稿预算", "stuck > _LLM_BUDGET[0]" in srv)
    ok("本地路径零拷贝接口", '@app.post("/api/link")' in srv and "os.link(src, dst)" in srv)
    ok("前端有路径导入入口", 'id="linkPath"' in web and "linkLocal" in web)
    ok("云端连续失败快速止损", "fail_streak" in
       (BASE / "cloud_llm.py").read_text(encoding="utf-8"))


def llm_result_unit(tmp: Path):
    """【新增 v2.4-p2】长稿结果落盘 / 找回 / 容错，全部写临时目录。"""
    print("[结果保护] 润色结果落盘与找回")
    import server as S
    orig = S.LLM_RESULT
    S.LLM_RESULT = tmp / "_llm" / "last.json"
    try:
        ok("初始无结果", S._load_llm_result() is None)
        ok("取结果接口 ready=False", S.api_llm_result().get("ready") is False)
        sample = {"source": "原文。", "polished": "整理稿。", "tail": "原文。",
                  "audit": {}, "diff": [], "chunks": 3, "elapsed": 12.3,
                  "model": "glm-4-flash", "blocked": []}
        ok("落盘返回完成时间", bool(S._save_llm_result(sample)))
        ok("结果文件存在", S.LLM_RESULT.exists())
        got = S._load_llm_result()
        ok("内容可回读", bool(got) and got["polished"] == "整理稿。")
        ok("带完成时间", bool(got) and bool(got.get("finished_at")))
        r = S.api_llm_result()
        ok("接口可找回整稿", r.get("ready") and r["result"]["polished"] == "整理稿。")
        ok("进度接口标记结果就绪", S.api_llm_progress().get("result_ready") is True)
        S.LLM_RESULT.write_text("{坏 JSON", encoding="utf-8")
        ok("损坏文件容错", S._load_llm_result() is None)
        ok("容错后接口不抛", S.api_llm_result().get("ready") is False)
    finally:
        S.LLM_RESULT = orig
    web = (BASE / "web/index.html").read_text(encoding="utf-8")
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    ok("首页禁缓存", "no-store" in srv)
    ok("前端有醒目进度条", 'id="llmProg"' in web and "showLlmProgress" in web)
    ok("前端会找回结果", "recoverLlmResult" in web and "/api/llm/result" in web)
    ok("请求断开先找回结果", "await recoverLlmResult()" in web)
    ok("polish 完成先落盘", "_save_llm_result(r)" in srv)
    # 【新增 v2.4-p3】刷新后自动恢复字幕
    ok("刷新后自动恢复任务", "function restoreLastTask" in web and "asrLastTask" in web)
    ok("打开任务时记住", "setItem('asrLastTask', id)" in web)
    ok("换素材清旧记录", "removeItem('asrLastTask')" in web)
    ok("恢复失败有说明", "任务记录已清理" in web)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="v24_"))
    parallel_unit(tmp)
    keys_unit(tmp)
    long_polish_unit()
    guards()
    llm_result_unit(tmp)
    bad = [n for n, c in PASS if not c]
    print(f"\n{len(PASS) - len(bad)}/{len(PASS)} 通过")
    if bad:
        print("失败：" + "、".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
