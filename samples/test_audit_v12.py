# -*- coding: utf-8 -*-
"""v1.2 审计修复专项回归测试。

分两段：
  unit() —— 纯函数/容错逻辑，直接 import server，不需要服务；
  api()  —— HTTP 接口，需要服务已在 127.0.0.1:7870 运行。

运行：python samples/test_audit_v12.py
"""
import json
import queue
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:7870"
sys.path.insert(0, str(BASE))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS = []


def ok(name):
    PASS.append(name)
    print("  PASS", name)


# ─────────────────────── 单元 ───────────────────────

def unit():
    import server as S
    import align as AL

    print("\n[unit] 输入校验 / 原子写 / 队列容错")

    # 1. _normalize_cues：畸形输入必须变成 ValueError，而不是 500
    bad = [[], "abc", ["abc"], [{"text": "x"}],
           [{"start": "a", "end": 1, "text": "x"}],
           [{"start": float("nan"), "end": 2, "text": "x"}],
           [{"start": -1, "end": 2, "text": "x"}]]
    for i, v in enumerate(bad):
        try:
            S._normalize_cues(v)
            raise AssertionError(f"bad case {i} 未被拦下: {v!r}")
        except ValueError:
            pass
    ok("畸形 cues 7 例全部拦为 400 级错误")

    # 2. 倒挂 / 零时长自动修正，text 统一成字符串
    r = S._normalize_cues([{"start": 5, "end": 2, "text": None},
                           {"start": 3, "end": 3, "text": 123}])
    assert r[0]["start"] == 2 and r[0]["end"] == 5
    assert r[0]["text"] == "" and r[1]["text"] == "123"
    assert r[1]["end"] - r[1]["start"] >= 0.0009   # 浮点舍入容差
    ok("倒挂时间轴交换 / 零时长兜底 / text 类型归一")

    # 3. 原子写不残留 tmp
    p = BASE / "out" / ("_atomic_probe_" + uuid.uuid4().hex[:6] + ".txt")
    S._atomic_write(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"
    assert not Path(str(p) + ".tmp").exists()
    p.unlink()
    ok("原子写：内容正确且无 .tmp 残留")

    # 4. _locate：旧副本路径改判到当前工程；BASE 外路径一律拒绝
    media = S.MEDIA
    sample = next(iter(media.glob("*.mp3")), None)
    if sample:
        fake_old = Path(r"D:\WorkBuddy\2026-10-26-22-09-38\asr-pipeline\media") / sample.name
        got = S._locate(str(fake_old), media)
        assert got is not None and str(got).startswith(str(S.BASE))
        ok("迁移旧路径自动改判到当前工程")
    assert S._locate(r"D:\nowhere\zzz.mp3", media) is None
    assert S._locate("", media) is None and S._locate(None, media) is None
    ok("外部/空/None 路径一律拒绝")

    # 5. 队列容错：SystemExit 不能冲出 _run_queued（原实现会杀死整个 worker）
    tid = "_unit_" + uuid.uuid4().hex[:6]
    S.TASKS[tid] = {"id": tid, "status": "queued", "logs": [], "result": None}
    original = S._worker

    def boom(_tid):
        raise SystemExit("simulated ffmpeg failure")

    S._worker = boom
    try:
        S._run_queued(tid)                 # 不得抛出
        assert S.TASKS[tid]["status"] == "error"
        assert "SystemExit" in S.TASKS[tid]["error"]
    finally:
        S._worker = original
        S.TASKS.pop(tid, None)
        (S.JOBS / f"{tid}.json").unlink(missing_ok=True)
    ok("SystemExit 被兜住并落为 error，队列线程不会退出")

    # 6. 排队位次不计已取消任务（换一条私有队列，别惊动后台 worker）
    orig_q = S.JOB_Q
    q = queue.Queue()
    S.JOB_Q = q
    a, b, c = ("_pos_" + uuid.uuid4().hex[:4] for _ in range(3))
    try:
        for x, st in ((a, "cancelled"), (b, "queued"), (c, "queued")):
            S.TASKS[x] = {"id": x, "status": st}
            q.put(x)
        assert S._queue_pos(b) == 1, S._queue_pos(b)
        assert S._queue_pos(c) == 2, S._queue_pos(c)
    finally:
        S.JOB_Q = orig_q
        for x in (a, b, c):
            S.TASKS.pop(x, None)
    ok("排队位次跳过已取消条目")

    # 7. align.py：损坏时间戳不崩、字序不漂移
    ts = [[0, 100], None, ["bad"], [200], [-5, 300], [400, 350], [500, 600]]
    clean = AL._clean_ts(ts)
    assert len(clean) == len(ts) and all(len(x) == 2 for x in clean)
    assert all(x[0] <= x[1] and x[0] >= 0 for x in clean)
    ok("align 坏时间戳占位修复，长度与单调性保持")

    res = AL.align("今天我们讲药品经营质量管理规范的基本框架，以及冷链药品的储运要求。",
                   "今天我们讲药品经营质量管理规范的基本框架，以及冷链药品的储运要求。",
                   [[i * 100, i * 100 + 90] for i in range(40)])
    assert res["cues"] and all(c["end"] >= c["start"] for c in res["cues"])
    assert all(c["text"].strip() for c in res["cues"])
    ok("align 正常文稿：cues 时间不倒挂、无空文本")

    # 8. PUNCT 口径统一（消除 align 与 transcribe 两份硬编码）
    assert AL.PUNCT is S.T.PUNCT
    ok("align 复用 transcribe.PUNCT，口径不再漂移")


# ─────────────────────── HTTP ───────────────────────

def request(path, obj=None, method=None):
    data = None
    headers = {}
    if obj is not None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers,
                                 method=method or ("POST" if obj is not None else "GET"))
    try:
        with opener.open(req, timeout=90) as r:
            raw = r.read().decode("utf-8", "ignore")
            return r.status, (json.loads(raw) if raw.startswith(("{", "["))
                              else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def post_file(path, filename, content):
    boundary = "----asr" + uuid.uuid4().hex[:8]
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    body += content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        API + path, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def api():
    print("\n[api] 接口回归（需服务在 7870）")

    code, d = request("/api/health")
    assert code == 200 and d["ok"] and d["version"] == "1.2"
    ok(f"/api/health 200（排队 {d['queued']} / 运行 {d['running']} / 任务 {d['tasks']}）")

    # 畸形输入：修复前实测 500
    for payload in ({"stem": "audit_v12_probe", "cues": ["abc"]},
                    {"stem": "audit_v12_probe", "cues": [{"text": "x"}]}):
        code, d = request("/api/save", payload)
        assert code == 400, (code, d)
    ok("/api/save 畸形 cues → 400（原 500）")

    code, d = request("/api/export", {"stem": "audit_v12_probe", "kind": "srt",
                                     "cues": [{"text": "x"}]})
    assert code == 400, (code, d)
    ok("/api/export 畸形 cues → 400（原 500）")

    cues = [{"start": 0, "end": 1.5, "text": "第一句"},
            {"start": 1.6, "end": 3.0, "text": "第二句"}]
    code, d = request("/api/save", {"stem": "audit_v12_probe", "cues": cues})
    assert code == 200 and d["ok"]
    edited = BASE / "out" / "audit_v12_probe.edited.srt"
    assert edited.exists() and "-->" in edited.read_text(encoding="utf-8")
    code, d = request("/api/draft?stem=audit_v12_probe")
    assert d.get("exists") and len(d["cues"]) == 2
    ok("/api/save 正常写入 + 草稿可读回")

    code, d = request("/api/export", {"stem": "audit_v12_probe", "kind": "vtt",
                                     "cues": cues})
    # export 直接返回文件体，解析失败时是字符串
    assert code == 200, (code, d)
    ok("/api/export VTT 200")

    # 内部文件不再可下载（修复前实测 200 且能读到本机绝对路径）
    job = next(iter((BASE / "out" / "_jobs").glob("*.json")), None)
    if job:
        code, _ = request("/api/download?path=" + urllib.parse.quote(str(job)))
        assert code == 400, code
        ok("/api/download 拦截 _jobs 内部文件 → 400（原 200）")
    srt = BASE / "out" / "audit_v12_probe.edited.srt"
    code, _ = request("/api/download?path=" + urllib.parse.quote(str(srt)))
    assert code == 200
    ok("/api/download 正常产物仍可下载 → 200")

    # 上传校验
    code, _ = post_file("/api/upload", "evil.exe", b"MZ not audio")
    assert code == 400, code
    ok("/api/upload 非法扩展名 → 400")
    code, _ = post_file("/api/upload", "empty.mp3", b"")
    assert code == 400, code
    ok("/api/upload 空文件 → 400")
    code, _ = post_file("/api/upload", "broken.wav", b"this is not a wav")
    assert code == 400, code
    ok("/api/upload 损坏 wav 头 → 400")

    # 词表原子写：POST 原词表后仍合法，且生成一次性备份
    code, cfg = request("/api/terms")
    assert code == 200 and "groups" in cfg
    groups = {k: v.get("words", []) for k, v in cfg["groups"].items()}
    tails = {k: v.get("tail_only", []) for k, v in cfg["groups"].items()}
    code, d = request("/api/terms", {"groups": groups, "tail_only": tails})
    assert code == 200 and d.get("ok")
    raw = (BASE / "rules" / "filler_words.json").read_text(encoding="utf-8")
    cfg2 = json.loads(raw)
    total = sum(len(v.get("words", [])) + len(v.get("tail_only", []))
                for v in cfg2["groups"].values())
    assert total == 101, total
    assert (BASE / "rules" / "filler_words.json.bak").exists()
    ok(f"/api/terms 原子写后词表仍合法（{total} 词），备份已生成")

    # 波形：降采样后的 bucket_ms 应反映真实桶宽
    stem = "e2bfb3e29da0_test_lesson"
    if (BASE / "out" / f"{stem}.16k.wav").exists():
        code, d = request(f"/api/peaks?stem={stem}&n=200")
        assert code == 200 and len(d["mins"]) == 200, (code, str(d)[:120])
        expect = round(d["duration"] * 1000 / len(d["mins"]), 3)
        assert abs(d["bucket_ms"] - expect) < 0.01, (d["bucket_ms"], expect)
        ok(f"/api/peaks 200 点，bucket_ms={d['bucket_ms']} 与真实桶宽一致")

    # 清理探针产物
    for f in ("audit_v12_probe.edited.srt", "audit_v12_probe.edited.txt",
              "audit_v12_probe.edited.srt.bak", "audit_v12_probe.raw.srt"):
        (BASE / "out" / f).unlink(missing_ok=True)


def main():
    unit()
    try:
        api()
    except (urllib.error.URLError, ConnectionRefusedError) as e:
        print("\n[api] 跳过（服务未运行）：", e)
    print(f"\n==== {len(PASS)} 项通过 ====")
    for x in PASS:
        print("  -", x)


if __name__ == "__main__":
    main()
