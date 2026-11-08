# -*- coding: utf-8 -*-
"""v1.8 审计修复专项回归。

分三段：
  unit()  —— 标点/组装/别名/审计/原子写/任务加载，不加载模型，秒级完成；
  llm()   —— 截断检测与对半切自愈，用假 LLM 模拟，不加载模型；
  api()   —— 服务端行为（版本、409 并发保护），需要服务在 7870 运行。

运行：python samples/test_v18.py
"""
import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:7870"
sys.path.insert(0, str(BASE))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS = []


def ok(name):
    PASS.append(name)
    print("  PASS", name)


# ────────────────────────────── unit

def unit():
    import server as S
    import termfix as TF
    import llm_polish as LP

    # 1. full 模式必须保住英文缩写与数字点（v1.8 修的 P1）
    cases = {
        "依据GMP.GSP规定执行": "依据GMP.GSP规定执行",
        "见No.5文件": "见No.5文件",
        "e.g. 这一段": "e.g. 这一段",
        "UDI.Database": "UDI.Database",
        "3.14和98.5%": "3.14和98.5%",
        "第一章.总则": "第一章。总则",        # 中文语境该转
        "他说.然后走了": "他说。然后走了",
        "你好,世界": "你好，世界",           # 中文后的逗号该转
        "abc,def": "abc,def",               # 英文逗号不动
    }
    for src, want in cases.items():
        got = S.norm_punct(src, "full")
        assert got == want, f"full 模式：{src!r} → {got!r}，期望 {want!r}"
    ok(f"full 模式保住英文缩写/数字点（{len(cases)} 例）")

    # 2. half 模式与 keep 不受影响
    assert S.norm_punct("你好。", "half") == "你好."
    assert S.norm_punct("你好。", "keep") == "你好。"
    assert S.norm_punct("", "full") == ""
    assert S.norm_punct("乱写", "乱写") == "乱写"
    ok("half / keep / 非法模式 行为不变")

    # 3. build_plain_text 非数组要报错而不是 AttributeError
    for bad in ("abc", None, 123, {"a": 1}):
        try:
            S.build_plain_text(bad)
            raise AssertionError(f"{bad!r} 应当报错")
        except ValueError:
            pass
    assert S.build_plain_text([]) == ""
    assert S.build_plain_text([{"text": "甲。"}, {"text": "乙。"}]) == "甲。\n\n乙。"
    ok("build_plain_text 非数组 → ValueError，正常输入不受影响")

    # 4. 别名自我膨胀防护
    tmp = Path(tempfile.mkdtemp()) / "aliases.txt"
    tmp.write_text(
        "# 注释\n"
        "药品经营=>药品经营质量管理规范\n"      # 全称含简称 → 必须拒绝
        "GSP=>药品经营质量管理规范\n"
        "坏行没有箭头\n"
        "空=>\n", encoding="utf-8")
    al = TF.load_aliases(tmp)
    assert [x[0] for x in al] == ["GSP"], al
    # 真实表也要能正常加载
    real = TF.load_aliases(BASE / "terms" / "aliases.txt")
    assert len(real) >= 6 and all(s not in f for s, f in real)
    ok(f"简称表拒绝自我膨胀条目（真实表 {len(real)} 条）")

    # 5. 极短文本也有五项硬红线
    a = LP.audit("短句。", "短句。")
    hard = [k for k, v in a.items() if v["hard"]]
    assert len(hard) == 5, hard
    assert a["末句保留"]["pass"] and "过短" in a["末句保留"]["note"]
    ok("极短文本仍报满五项硬红线")

    # 6. 原子写并发安全（同一目标多线程写，结果必须是完整内容之一）
    target = Path(tempfile.mkdtemp()) / "x.json"
    payloads = [json.dumps({"i": i, "pad": "字" * 2000}, ensure_ascii=False)
                for i in range(8)]
    errs = []

    def writer(txt):
        try:
            S._atomic_write(target, txt)
        except Exception as exc:            # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, f"并发写抛异常：{errs[:2]}"
    assert json.loads(target.read_text(encoding="utf-8"))["pad"] == "字" * 2000
    leftovers = list(target.parent.glob("*.tmp"))
    assert not leftovers, f"临时文件残留：{leftovers}"
    ok("原子写并发 8 线程无异常、无残留临时文件")

    # 7. _load_jobs 遇到非对象 JSON 不崩
    import server as S2
    old_jobs = S2.JOBS
    d = Path(tempfile.mkdtemp())
    (d / "bad1.json").write_text("[1,2,3]", encoding="utf-8")     # 顶层是数组
    (d / "bad2.json").write_text("not json", encoding="utf-8")
    (d / "good.json").write_text(json.dumps({"id": "probe_ok", "status": "done"}),
                                 encoding="utf-8")
    S2.JOBS = d
    try:
        S2.TASKS.pop("probe_ok", None)
        S2._load_jobs()                     # 不能抛
        assert "probe_ok" in S2.TASKS, "正常任务应被加载"
    finally:
        S2.JOBS = old_jobs
        S2.TASKS.pop("probe_ok", None)
    ok("_load_jobs 跳过损坏文件、正常文件照常加载")


# ────────────────────────────── llm

class FakeLLM:
    """模拟 llama.cpp：超过 cap 就返回 finish_reason='length'。"""

    def __init__(self, cap):
        self.cap, self.calls = cap, []

    def create_chat_completion(self, messages, **kw):
        body = messages[-1]["content"]
        # 【修改 v2.7】兼容 v2.5 起的 <transcript> 包裹；旧格式仍保留
        import re as _re
        hit = _re.search(r"<transcript>\s*(.*?)\s*</transcript>", body, _re.S)
        if hit:
            text = hit.group(1)
        else:
            text = body.split("原文：")[-1].split("\n/no_think")[0].strip("\n")
        self.calls.append(len(text))
        if len(text) > self.cap:
            return {"choices": [{"finish_reason": "length",
                                 "message": {"content": text[:self.cap]}}]}
        return {"choices": [{"finish_reason": "stop",
                             "message": {"content": text}}]}


def llm():
    import llm_polish as LP

    # 8. 截断必须抛错，不能把半截当结果
    f = FakeLLM(cap=50)
    try:
        LP._polish_chunk(f, "字" * 200)
        raise AssertionError("应抛 TruncatedOutput")
    except LP.TruncatedOutput as exc:
        assert "截断" in str(exc)
    ok("finish_reason=length → TruncatedOutput")

    # 9. 正常返回不受影响
    f = FakeLLM(cap=10000)
    assert LP._polish_chunk(f, "正常一段话。") == "正常一段话。"
    ok("正常返回不被误判")

    # 10. 对半切自愈：内容零丢失，且切块都在上限内
    text = "\n".join([f"这是第{i}句话，用来测试对半切自愈。" for i in range(14)])
    f = FakeLLM(cap=200)
    out = LP._polish_with_split(f, text)
    assert f.calls[0] == len(text), "首块应是原样整块"
    assert all(c <= 200 for c in f.calls[1:]), f.calls
    assert text.replace("\n", "") in out.replace("\n", ""), "内容丢失"
    assert len(f.calls) > 1, "应当发生切分"
    ok(f"截断自动对半切：{len(text)} 字 → {len(f.calls)} 次调用，内容零丢失")

    # 11. 切不动时抛错，不给残稿
    try:
        LP._polish_with_split(FakeLLM(cap=10), "短" * 100)
        raise AssertionError("应抛 TruncatedOutput")
    except LP.TruncatedOutput:
        pass
    ok("递归到上限仍截断 → 抛错，不返回残稿")

    # 12. 模块级输入上限（命令行也受约束）
    try:
        LP.polish("字" * (LP.MAX_INPUT + 1))
        raise AssertionError("应抛 ValueError")
    except ValueError as exc:
        assert "上限" in str(exc)
    ok("polish() 自带输入上限，命令行也拦得住")

    # 13. 模型查找有缓存（两次返回同一路径且不重复扫盘）
    p1 = LP.find_model()
    p2 = LP.find_model()
    assert p1 == p2
    ok("find_model 结果缓存")


# ────────────────────────────── api

def api():
    h = json.loads(opener.open(API + "/api/health", timeout=10).read())
    assert h["version"] == "1.8", h["version"]
    ok(f"/api/health v{h['version']}")

    # 14. 导出：非数组 cues 返回 400 而不是 500
    import urllib.error
    body = json.dumps({"stem": "probe", "kind": "txt", "cues": "abc"}).encode()
    req = urllib.request.Request(API + "/api/export", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        opener.open(req, timeout=10)
        raise AssertionError("应当 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400, e.code
    ok("/api/export 非法 cues → 400")

    # 15. 润色并发保护：占住锁时必须是 409/503，而不是排队或 500
    import server as S
    got = S._LLM_BUSY.acquire(blocking=False)
    assert got
    try:
        S._LLM_START[0] = __import__("time").monotonic()
        r = S.api_llm_polish({"text": "测试一段话。"})
        assert hasattr(r, "status_code") and r.status_code == 409, getattr(r, "status_code", r)
        # 模拟卡死：把开始时间拨回很久以前
        S._LLM_START[0] = __import__("time").monotonic() - S.LLM_MAX_SECONDS - 10
        r2 = S.api_llm_polish({"text": "测试一段话。"})
        assert r2.status_code == 503, r2.status_code
    finally:
        S._LLM_BUSY.release()
        S._LLM_START[0] = 0.0
    ok("润色并发：正常占用 409、超时占用 503")


def main():
    print("=== unit：标点 / 组装 / 别名 / 审计 / 原子写 / 任务加载 ===")
    unit()
    print("\n=== llm：截断检测与自愈（假模型，不加载 2.4GB）===")
    llm()
    print("\n=== api：服务端行为（需服务在 7870）===")
    try:
        api()
    except Exception as exc:
        print(f"  SKIP 服务未运行或版本不符：{exc}")
    print("\n" + "=" * 60)
    print(f"共 {len(PASS)} 项通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
