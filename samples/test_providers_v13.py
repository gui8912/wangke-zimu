# -*- coding: utf-8 -*-
"""v1.3 可插拔识别源 / 双轨整理稿专项回归测试。

分两段：
  unit() —— 纯函数 + 服务端函数直调 + run_pipeline mock，不需要服务；
  api()  —— HTTP 接口，需要服务已在 127.0.0.1:7870 运行。

运行：python samples/test_providers_v13.py
"""
import json
import sys
import tempfile
import urllib.parse
import urllib.request
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
import apibase                    # 【新增 v2.9】端口可能不是 7870
API = apibase.base()
sys.path.insert(0, str(BASE))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS = []


def ok(name):
    PASS.append(name)
    print("  PASS", name)


# ─────────────────────── 单元 ───────────────────────

def unit():
    import asr_providers as P
    import transcribe as T
    import server as S

    print("\n[unit] provider 抽象层 / 双轨映射 / 零回归")

    # 1. 注册表与默认回落
    ps = P.list_providers()
    assert len(ps) == 4
    assert P.DEFAULT_PROVIDER == "local-qwen3"
    assert P.preferred_from_env().id == "local-qwen3"
    assert all("ready" in p and "configured" in p for p in ps)
    ok("注册表 4 项、默认本地、状态字段齐全")

    # 2. 云端未配置：不发请求、不伪造文本、明确报错
    for pid in ("tencent-cloud", "volcengine-doubao"):
        p = P.get_provider(pid)
        assert p.kind == "cloud" and p.configured() is False
        assert getattr(p, "ready", False) is False
        try:
            p.transcribe(None)
            raise AssertionError(f"{pid} 未拦下")
        except P.ProviderUnavailable as e:
            assert "未配置" in str(e)
    try:
        P.get_provider("not-exist")
        raise AssertionError("未知识别源未拦下")
    except KeyError:
        pass
    ok("云端未配置/未知识别源明确报错，不伪造结果")

    # 3. 腾讯云响应双轨：原文轨含语气词，整理稿轨不含，词级时间戳保留
    raw = {"Response": {"Data": {"ResultDetail": [
        {"FinalSentence": "嗯，这个，我们今天讲飞行检查。",
         "StartMs": 1000, "EndMs": 4200,
         "Words": [{"Word": "嗯", "OffsetStartMs": 0, "OffsetEndMs": 200},
                   {"Word": "飞行检查", "OffsetStartMs": 1200, "OffsetEndMs": 3000}],
         "WrittenText": "今天我们讲解飞行检查。"},
    ]}}}
    tr = P.parse_tencent_response(raw)
    assert tr.subtitle_text == "嗯，这个，我们今天讲飞行检查。"
    assert tr.script_text == "今天我们讲解飞行检查。"
    assert tr.has_script and tr.script_source == "tencent:WrittenText"
    assert tr.timestamps == [[1000, 1200], [2200, 4000]]
    assert tr.sentences[0]["start"] == 1000
    ok("腾讯云 FinalSentence+Words 原文轨 / WrittenText 整理稿轨分离")

    # 4. 火山响应：原文轨由 words 拼接，顺滑字段等于原文时不造假整理稿
    tr2 = P.parse_volc_response({"data": {"result": {"utterances": [
        {"words": [{"text": "飞行", "start_time": 0, "end_time": 300},
                   {"text": "检查", "start_time": 300, "end_time": 600}],
         "text": "飞行检查"}]}}})
    assert tr2.subtitle_text == "飞行检查" and not tr2.has_script
    assert tr2.timestamps == [[0, 300], [300, 600]]
    tr3 = P.parse_volc_response({"data": {"result": {
        "utterances": [{"words": [{"text": "飞行", "start_time": 0, "end_time": 300}]}],
        "smooth_text": "飞行器检查要求"}}})
    assert tr3.has_script and tr3.script_text == "飞行器检查要求"
    ok("火山原文轨由 words 拼接，顺滑稿不伪造、不覆盖原文")

    # 5. 时间戳净化：非法值不污染对齐
    assert P._to_ms("abc") == 0 and P._to_ms(-5) == 0
    assert P._to_ms("1234") == 1234
    ok("非法时间戳净化为 0，不污染 align.py")

    # 6. diff 与一键回退
    ops = P.diff_ops("甲\n乙\n丙", "甲\n乙改\n丙\n丁")
    assert P.restore_from_diff(ops) == "甲\n乙\n丙"
    assert any(o["op"] == "insert" for o in ops)
    ok("diff 逐段、一键回退还原原稿")

    # 7. Tracks 序列化往返
    back = P.Tracks.from_dict(json.loads(tr.to_json()))
    assert back.script_text == tr.script_text and back.has_script
    assert P.Tracks.from_dict({}).subtitle_text == ""
    ok("Tracks 序列化往返 + 旧产物缺字段兼容")

    # 8. 热词表：超长词被丢弃，权重被剥离
    hp = BASE / "terms" / "hotwords.txt"
    terms = P.hotword_terms(hp, max_chars=10)
    assert isinstance(terms, list) and all(len(x) <= 10 for x in terms)
    assert all("|" not in x for x in terms)
    ok(f"云端热词表可用 {len(terms)} 条，超长词已剔除")

    # 9. run_pipeline 默认路径零回归（mock 掉真实模型）
    outdir = Path(tempfile.mkdtemp(prefix="asr_v13_"))
    src = outdir / "v13_probe.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    original = T.transcribe

    # 【修改 v2.4】run_pipeline 自 v2.0 起多传一个 progress 回调，
    # 假函数签名必须跟上，否则整条本地识别链路的用例会 TypeError。
    def fake(wav, cfg, hotword, device, language, log, progress=None):
        return {"text": "嗯，今天我们讲飞行检查。",
                "timestamp": [[i * 300, i * 300 + 300] for i in range(12)]}

    T.transcribe = fake
    try:
        r = T.run_pipeline(src, "qwen3-asr", False, outdir=outdir,
                           log=lambda m: None)
        assert r["provider"] == "local-qwen3"
        assert not r["has_script"] and r["script_text"] == ""
        assert r["chars"] == len("嗯，今天我们讲飞行检查。")
        tp = json.loads((outdir / "v13_probe.tracks.json").read_text("utf-8"))
        assert tp["subtitle_text"] == "嗯，今天我们讲飞行检查。"
        assert tp["script_text"] == "" and len(tp["timestamps"]) == 12
        old = json.loads((outdir / "v13_probe.json").read_text("utf-8"))
        assert {"meta", "text", "sentences", "timestamps"} <= set(old)
        assert old["meta"]["provider"] == "local-qwen3"
        ok("默认路径零回归，旧 <stem>.json 结构兼容 align.py")

        # 10. 云端分支：字幕轨仍是原文，整理稿另存，本地清理被跳过
        class FakeCloud(P.BaseProvider):
            id = "tencent-cloud"
            name = "fake cloud"
            kind = "cloud"
            supports_script = True
            ready = True

            def configured(self):
                return True

            def require(self):
                return None

            def transcribe(self, wav, language="Chinese", hotwords=None, log=print):
                return P.Tracks(
                    provider=self.id,
                    subtitle_text="嗯，今天我们讲飞行检查。",
                    timestamps=[[i * 300, i * 300 + 300] for i in range(12)],
                    script_text="今天我们讲解飞行检查的相关要求。",
                    script_source="tencent:WrittenText")

        saved = P.REGISTRY["tencent-cloud"]
        P.REGISTRY["tencent-cloud"] = FakeCloud()
        try:
            r2 = T.run_pipeline(src, "qwen3-asr", True, outdir=outdir,
                                log=lambda m: None,
                                provider_key="tencent-cloud",
                                use_script_track=True)
            assert r2["has_script"] and r2["script_text"]
            assert r2["clean_srt_text"] == "", "有整理稿时应跳过本地清理"
            assert Path(r2["script_txt_path"]).exists()
            srt = Path(r2["srt_path"]).read_text("utf-8")
            assert "嗯" in srt and "相关要求" not in srt, "字幕轨被改写了"
            ok("云端双轨：字幕原文不动、整理稿另存、避免双重改写")
        finally:
            P.REGISTRY["tencent-cloud"] = saved
    finally:
        T.transcribe = original

    # 11. 服务端函数直调
    assert S.api_providers()["default"] == "local-qwen3"
    d = S.api_diff({"before": "甲\n乙", "after": "甲\n乙改\n丙"})
    assert d["restored"] == "甲\n乙" and d["ops"]
    h = S.api_health()
    # 【修改 v1.4】原来硬编码 == "1.3"，每次升版都要改测试；改为下限比较
    ver = tuple(int(x) for x in str(h["version"]).split("."))
    assert ver >= (1, 3) and h["provider"] == "local-qwen3", h
    ok(f"服务端函数：providers / diff / health v{h['version']}")

    # 12. /api/tracks 路径穿越防护
    old_out = S.OUT
    S.OUT = Path(tempfile.mkdtemp(prefix="tracks_v13_"))
    try:
        assert S.api_tracks("../_jobs/x").status_code == 404
        assert S.api_tracks("").status_code == 404
        ok("/api/tracks 路径穿越被拦")
    finally:
        S.OUT = old_out

    # 13. 前端新增控件存在
    html = (BASE / "web" / "index.html").read_text("utf-8")
    for needle in ("useScriptTrack", "btnTracks", "tracksMask",
                   "tracksRestore", "rvLoadFromScript", "initProviders"):
        assert needle in html, needle
    ok("前端控件与逻辑齐全")


# ─────────────────────── API ───────────────────────

def api():
    print("\n[api] HTTP 接口")

    def get(path):
        with opener.open(API + path, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(path, payload):
        req = urllib.request.Request(
            API + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    h = get("/api/health")
    assert h["ok"] and h["version"] == "1.3"
    ok("/api/health version 1.3")

    ps = get("/api/providers")
    assert ps["default"] == "local-qwen3" and len(ps["providers"]) == 4
    blob = json.dumps(ps, ensure_ascii=False)
    # 只回变量名，不回密钥值
    assert "ASR_TENCENT_SECRET_KEY" in blob
    ok("/api/providers 4 项且不含密钥值")

    d = post("/api/diff", {"before": "嗯，今天讲飞行检查。",
                           "after": "今天讲解飞行检查。"})
    assert d["ok"] and d["ops"] and "嗯" in d["restored"]
    ok("/api/diff 返回 ops 与原稿")

    with opener.open(API + "/", timeout=10) as r:
        assert r.status == 200
    ok("首页 200")

    # 云端未配置时提交任务必须被 400 拦下，不能排队后失败
    body = urllib.parse.urlencode({
        "file_id": "no-such-file", "model": "qwen3-asr",
        "provider": "tencent-cloud"}).encode()
    req = urllib.request.Request(API + "/api/transcribe", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        opener.open(req, timeout=10)
        raise AssertionError("云端未配置未被拦下")
    except urllib.error.HTTPError as e:
        assert e.code == 400, e.code
        detail = json.loads(e.read().decode("utf-8"))
        assert "未配置" in detail["error"] or "请先上传" in detail["error"]
    ok("云端未配置提交被拦为 400")


if __name__ == "__main__":
    unit()
    try:
        api()
    except Exception as exc:
        print(f"\n[api] 跳过（服务未运行或不可达）：{type(exc).__name__}: {exc}")
        api_skipped = True
    else:
        api_skipped = False
    print(f"\n合计 {len(PASS)} 项通过"
          + ("（API 段跳过）" if api_skipped else ""))
