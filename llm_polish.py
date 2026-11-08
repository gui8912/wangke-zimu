# -*- coding: utf-8 -*-
"""讲稿层 LLM 顺滑（七遍改稿的「第 0 遍」底稿）。

定位：**只作用于讲稿层**。字幕（cues + 字级时间戳）一个字都不碰——
整段改写会打断 align.py 的字级对齐，音画就不同步了。这条红线在 v1.3 的
双轨设计里已经写死，这里继续遵守。

为什么直接对原始识别稿做，而不是接在规则清理（Cleaner）之后：
PoC 实测（samples/LLM_PoC报告.md），「先规则再 LLM」把结尾
「那今天就到这里，谢谢大家」整句丢了，而「LLM 直接对原文」保留了。
推测是规则先删掉衔接词后文本更紧凑，模型更容易把剩下的当冗余压掉。
所以这里走「LLM 直接对原文」这条路，规则清理留给人工七遍里的第 1、5 遍。

三道闸门，缺一不可：
  1. 术语 / 条款号 / 数字 / 末句 / 长度 五项硬红线，任一不过则前端禁用「采用」
  2. 逐段 diff 强制预览，不预览不能采用
  3. 采用后另起 stem（后缀 _润色），原稿 v0 不动，随时可回溯

模型缺失、llama-cpp-python 未装、文本过长，一律明确报错，**不伪造结果**。
"""
import os
import re
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
TERMS = BASE / "terms"

# 模型目录可用环境变量覆盖：换更大的模型、或放到别的盘时不用改代码
MODEL_DIR = Path(os.environ.get("ASR_LLM_DIR") or r"D:\AI\models\llm")

# 单次请求的输入上限。8000 字约等于 25 分钟口播，分 12 块左右，
# CPU 上要跑两三分钟——再长用户等不住，也会让网关超时，所以拦在前面。
MAX_INPUT = 8000

# 每块送给模型的字数。Qwen3-4B 的 n_ctx=4096，中文 1 字约 1.5 token，
# 700 字输入 + 提示词 + 输出，留有余量；再大就有截断风险。
CHUNK_CHARS = 700
N_CTX = 4096
MAX_TOKENS = 2400
THREADS = max(4, min(12, (os.cpu_count() or 8) - 4))


class TruncatedOutput(RuntimeError):
    """模型输出被 max_tokens 截断。

    必须单独识别：llama.cpp 截断时不会报错，只把 finish_reason 置为 "length"，
    返回的是「写了一半」的正文。不检查就会把它当完整结果继续拼进讲稿，
    中间整段内容静默消失（实测 max_tokens=80 时 343 字稿只剩 161 字）。
    """

SYSTEM = "你是药品监管培训讲稿的文字编辑。你的工作只是整理语言，不改事实。"

PROMPT = """把下面这段口语转写整理成书面讲稿。

硬性要求（违反任何一条都算失败）：
1. 专业术语、法规名称、条款号、数字必须原样保留，一个字都不能改；
2. 每一句的**信息**都要保留，包括开头和最后一句，不得省略段落。
   注意「保留信息」不等于「保留原话」；
3. 不得增加原文没有的信息，不得改写结论、不得补充举例；
4. 必须删掉：语气词（嗯、呃、啊、哦）、口头禅（这个、那个、就是说、然后呢）、
   无意义的重复、没有实际含义的起承转合（那么、其实、怎么说呢）；
5. **最后一句必须原样保留**，哪怕它是「谢谢大家」「今天就讲到这里」这类收尾——
   那是讲课实录的一部分，不是冗余，删掉即算失败；
6. 调整语序让句子通顺，可以合并短句，但不要写成提纲、不要分点、不要加标题；
7. 只输出整理后的正文，不要任何说明。

原文：
{text}
"""

# 句末标点，用于切分「句子单元」做分块
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])")
CLAUSE_RE = re.compile(r"第[〇零一二三四五六七八九十百千两]+(?:条|款|项|章|节|编)")
NUM_RE = re.compile(
    r"\d+(?:\.\d+)?%?"
    r"|百分之[〇零一二三四五六七八九十百千两]+(?:点[〇零一二三四五六七八九十百千两]+)?"
)
FILLERS = ("呃", "嗯", "那个", "这个", "就是说", "然后呢", "好吧")

_LLM = None
_LOAD_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()      # llama.cpp 同一实例不能并发推理，串行更稳


# ---------------------------------------------------------------- 模型管理

_MODEL_CACHE = {"path": None, "at": 0.0}


def find_model():
    """找 gguf：优先 Q4_K_M，其次任意 gguf。找不到返回 None，不抛异常。

    【修改 v1.8】结果缓存 30 秒。`/api/llm/status` 每次请求都会走到这里，
    模型目录下文件多时 rglob 要几十毫秒；状态查询是高频调用（页面加载、
    服务台探活），没必要每次都扫盘。
    """
    now = time.monotonic()
    cached = _MODEL_CACHE["path"]
    if cached and now - _MODEL_CACHE["at"] < 30:
        p = Path(cached)
        return p if p.exists() else None
    if not MODEL_DIR.exists():
        return None
    for pat in ("*Q4_K_M*.gguf", "*.gguf"):
        cands = sorted(MODEL_DIR.rglob(pat))
        if cands:
            _MODEL_CACHE["path"], _MODEL_CACHE["at"] = str(cands[0]), now
            return cands[0]
    return None


def available():
    """返回 (是否可用, 说明)。只查文件与依赖，**不加载模型**（加载要吃 2.4GB）。

    前端据此置灰按钮并写明原因，而不是让用户点了等半天才失败。
    """
    try:
        import llama_cpp  # noqa: F401
    except Exception as exc:
        return False, f"未安装 llama-cpp-python（{exc.__class__.__name__}）"
    m = find_model()
    if m is None:
        return False, f"未找到 gguf 模型（{MODEL_DIR}）"
    return True, f"{m.name} · {m.stat().st_size / 1048576:.0f} MB"


def _ensure(log=print):
    """懒加载：第一次真正润色时才占内存，避免服务一启动就吃掉 2.4GB。"""
    global _LLM
    if _LLM is not None:
        return _LLM
    ok, msg = available()
    if not ok:
        raise RuntimeError(msg)
    with _LOAD_LOCK:
        if _LLM is not None:
            return _LLM
        from llama_cpp import Llama
        m = find_model()
        log(f"加载模型 {m.name}（{m.stat().st_size / 1048576:.0f} MB，{THREADS} 线程）")
        _LLM = Llama(model_path=str(m), n_ctx=N_CTX, n_threads=THREADS, verbose=False)
    return _LLM


def release():
    """释放模型内存（改配置或长时间不用时可调）。"""
    global _LLM
    with _LOAD_LOCK:
        _LLM = None


# ---------------------------------------------------------------- 分块

def _units(text, limit=None):
    """切成句子单元：先按行，超长行再按句末标点切。

    字幕/讲稿本来就是一句一行，所以按行最安全；但整段粘贴的稿子可能没有换行，
    这时按句号切，仍比按字数硬切好得多（硬切会把句子劈成两半，模型读不懂）。

    limit 必须由调用方传进来：用全局 CHUNK_CHARS 判断的话，
    测试或调用方把 limit 调小时行内切分就不会触发，分块会失效。
    """
    limit = limit or CHUNK_CHARS
    units = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if len(ln) <= limit // 2:
            units.append(ln)
            continue
        for seg in _SENT_SPLIT.split(ln):
            seg = seg.strip()
            if seg:
                units.append(seg)
    return units


def chunk_text(text, limit=None):
    """把文本按「整句」累积成块，不切断句子。"""
    limit = limit or CHUNK_CHARS
    chunks, cur, n = [], [], 0
    for u in _units(text, limit):
        if cur and n + len(u) > limit:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(u)
        n += len(u)
    if cur:
        chunks.append("\n".join(cur))
    return chunks


# ---------------------------------------------------------------- 推理

def _polish_chunk(llm, text):
    resp = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": SYSTEM},
            # /no_think：Qwen3 默认会先输出思考段，讲稿整理不需要，
            # 关掉能省一半时间，也避免思考内容混进正文
            {"role": "user", "content": PROMPT.format(text=text) + "\n/no_think"},
        ],
        temperature=0.15, top_p=0.9, max_tokens=MAX_TOKENS,
    )
    ch = (resp.get("choices") or [{}])[0]
    out = (ch.get("message") or {}).get("content") or ""
    # modified(v1.8):截断必须当错误抛出。llama.cpp 截断时不报错，只把
    # finish_reason 置为 "length" 并返回「写了一半」的正文；不拦就会静默丢内容
    # （实测 max_tokens=80 时，343 字稿只剩 161 字，四项红线里只有覆盖率会掉）。
    if ch.get("finish_reason") == "length":
        raise TruncatedOutput(
            f"输出达到 {MAX_TOKENS} token 上限被截断（本块 {len(text)} 字）")
    out = out.strip()
    # 双保险：万一模型仍输出思考段或代码围栏
    out = re.sub(r"<think.*?</think\s*>", "", out, flags=re.S).strip()
    out = re.sub(r"^```[a-z]*\s*|```$", "", out).strip()
    return out


def _polish_with_split(llm, text, depth=0):
    """单块润色；被截断就把这块对半切开重试。

    截断的根因是「输出比输入长」，通常说明这段密度高（术语多、句子长）。
    对半切是最省事的自愈：每半的输出自然减半，不会再撞上限。
    递归上限 2 层（最多切成 4 份）；再切句子就太碎、模型反而改得差，
    这时直接报错让人知道，而不是继续产出半截稿。
    """
    try:
        return _polish_chunk(llm, text)
    except TruncatedOutput:
        if depth >= 2 or len(text) < 240:
            raise
        parts = chunk_text(text, limit=max(160, len(text) // 2))
        if len(parts) < 2:                      # 一整句超长、切不开：按字数硬分
            mid = len(text) // 2
            parts = [text[:mid], text[mid:]]
        return "\n".join(_polish_with_split(llm, p, depth + 1) for p in parts)


def last_sentence(text):
    """取原文最后一句（**带标点**），供前端「一键补回末句」用。

    模型删结尾是实测高频行为（提示词点名了也仍会发生），与其静默替它补上、
    把闸门变成永远通过，不如把原句交给用户，由他一键决定补不补、补哪句。
    """
    parts = []
    for ln in (text or "").splitlines():
        parts.extend(s.strip() for s in _SENT_SPLIT.split(ln) if s.strip())
    return parts[-1] if parts else ""


def polish(text, log=print):
    """润色整段讲稿。返回 {source, polished, tail, audit, diff, chunks, elapsed, model}。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("没有可润色的文本")
    if len(text) > MAX_INPUT:
        # modified(v1.8) 新增:上限放进模块里，命令行直接调用也受约束（原来只在服务端拦）
        raise ValueError(f"文本 {len(text)} 字，超过单次上限 {MAX_INPUT} 字，"
                         f"请分成两三段分别润色")
    llm = _ensure(log)
    chunks = chunk_text(text)
    parts, t0 = [], time.time()
    with _RUN_LOCK:
        for i, ck in enumerate(chunks, 1):
            log(f"润色第 {i}/{len(chunks)} 块（{len(ck)} 字）")
            parts.append(_polish_with_split(llm, ck))   # modified(v1.8):截断自愈
    out = "\n".join(p for p in parts if p)
    if not out.strip():
        raise RuntimeError("模型没有返回内容")
    m = find_model()
    return {
        "source": text,
        "polished": out,
        "tail": last_sentence(text),
        "audit": audit(text, out),
        "diff": diff_ops(text, out),
        "chunks": len(chunks),
        "elapsed": round(time.time() - t0, 1),
        "model": m.name if m else "",
    }


# ---------------------------------------------------------------- 审计

def must_keep_terms(src):
    """从项目术语表里取「原文中真的出现了」的术语当检查项。

    不硬编码清单：硬编码只对一份稿子有效，术语表才是项目里真正的真相源。
    hotwords.txt 是「词|权重」或纯词；aliases.txt 是「简称=>全称」，
    两侧都算——原文写的是哪一侧，润色后就该还是哪一侧。
    """
    out = set()
    for name in ("hotwords.txt", "aliases.txt"):
        p = TERMS / name
        if not p.exists():
            continue
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            for part in re.split(r"[|=>]", ln):
                t = part.strip()
                # 4 字以上才检查：「药品」「企业」这类太泛，留着是常识，不用盯
                if len(t) >= 4 and t in src:
                    out.add(t)
    return sorted(out)


def coverage(src, out):
    """按句检查原文被覆盖的比例。返回 (覆盖率, 丢失句子列表)。

    LLM 顺滑最危险的失败不是改错字，而是**整句漏掉**——PoC 里就丢了结尾，
    而术语/数字/长度四项全过，完全查不出来。这条是专门为它加的。
    """
    from rapidfuzz import fuzz
    sents = [s.strip() for s in re.split(r"[。！？\n]", src) if len(s.strip()) >= 6]
    if not sents:
        return 1.0, []
    miss = [s for s in sents if fuzz.partial_ratio(s, out) < 70]
    return round(1 - len(miss) / len(sents), 2), miss


def audit(src, out):
    """红线检查。返回 {项: {pass, note, hard}}，保持插入顺序供前端直接渲染。

    hard=True 的项目任一不过，前端就禁用「采用」——这是硬闸门，
    不是给个警告让人自己判断，因为漏句和改数字在几万字的稿子里肉眼发现不了。
    """
    res = {}

    terms = must_keep_terms(src)
    lost = [t for t in terms if t not in out]
    res["术语保留"] = {
        "pass": not lost, "hard": True,
        "note": f"{len(terms)} 条全部保留" if not lost
                else "丢失/改写：" + "、".join(lost[:6]),
    }

    sc, oc = CLAUSE_RE.findall(src), CLAUSE_RE.findall(out)
    res["条款号保留"] = {
        "pass": sorted(sc) == sorted(oc), "hard": True,
        "note": ("原文没有条款号，跳过" if not sc else f"{len(sc)} 处一致")
                if sorted(sc) == sorted(oc)
                else f"{len(sc)} → {len(oc)}：" + "、".join(sorted(set(sc) - set(oc))[:4]),
    }

    sn, on = NUM_RE.findall(src), NUM_RE.findall(out)
    res["数字保留"] = {
        "pass": sorted(sn) == sorted(on), "hard": True,
        "note": ("原文没有数字，跳过" if not sn else f"{len(sn)} 处一致")
                if sorted(sn) == sorted(on)
                else f"原文 {sn[:5]} → 结果 {on[:5]}",
    }

    cov, miss = coverage(src, out)
    sents = [s.strip() for s in re.split(r"[。！？\n]", src) if len(s.strip()) >= 6]
    if sents:
        from rapidfuzz import fuzz
        last = sents[-1]
        keep = fuzz.partial_ratio(last, out) >= 60
        res["末句保留"] = {
            "pass": keep, "hard": True,
            "note": "保留" if keep else f"疑似被删：{last[:18]}",
        }
    else:
        # modified(v1.8) 新增:原文过短（一句话不到 6 字）时也要给出这一项。
        # 原来直接跳过，界面上说的「五项硬红线」只剩四项，看起来像漏检。
        res["末句保留"] = {"pass": True, "hard": True,
                           "note": "原文过短，跳过"}

    ratio = len(out) / max(1, len(src))
    res["长度合理"] = {
        "pass": 0.6 <= ratio <= 1.25, "hard": True,
        "note": f"{len(src)} → {len(out)} 字（{ratio:.2f}×）",
    }

    # 以下两项只作参考：LLM 会重组句子，覆盖率低不必然等于丢内容；
    # 残留口语是规则层删不掉的（「这个/那个」后面跟名词，删了不通），
    # 正是 LLM 的增量所在，不该反过来当失败。
    res["内容覆盖（参考）"] = {
        "pass": cov >= 0.6, "hard": False,
        "note": f"{cov * 100:.0f}%" + (
            "；改写较大：" + "；".join(m[:12] for m in miss[:2]) if miss else ""),
    }
    left = [f for f in FILLERS if f in out]
    res["残留口语（参考）"] = {
        "pass": not left, "hard": False,
        "note": "无" if not left else "仍有：" + "、".join(left),
    }
    return res


def blocked(audit_result):
    """任一硬红线不过就返回原因列表，供前端直接展示。"""
    return [f"{k}：{v['note']}" for k, v in audit_result.items()
            if v.get("hard") and not v.get("pass")]


# ---------------------------------------------------------------- diff

def diff_ops(before, after):
    """**句级**差异，不是行级。

    讲稿润色会把十几行合并成整段，行级 diff 就退化成一个巨大的 replace，
    界面上看不出到底改了哪几句。这里按句末标点切分后再对齐，才能逐句对照。

    （v1.3 双轨对照用的 asr_providers.diff_ops 是行级，那个场景下整理稿
    和原文行数接近，行级更直观；这里是重写，语义不同，所以单写。）
    """
    import difflib

    def sents(t):
        return [s for s in re.split(r"(?<=[。！？；!?;\n])", t or "") if s.strip()]

    a, b = sents(before), sents(after)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [{"op": op,
             "before": "".join(a[i1:i2]).strip(),
             "after": "".join(b[j1:j2]).strip()}
            for op, i1, i2, j1, j2 in sm.get_opcodes()]


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    print("模型可用:", available())
    demo = ("那么我们说，嗯，这个两品一械的监管，啊，主要依据是药品管理法第一百二十条。\n"
            "就是说，二零二六年我们检查了三百五十家零售药店，合格率百分之九十八点五。\n"
            "那今天就到这里，谢谢大家。")
    chunks = chunk_text(demo)
    print(f"分块：{len(chunks)} 块 → {[len(c) for c in chunks]}")
    r = audit(demo, demo)
    for k, v in r.items():
        print(f"  {'✓' if v['pass'] else '✗'} {k}: {v['note']}")
    # 故意删掉末句，验证闸门能拦住
    bad = "\n".join(demo.splitlines()[:2])
    print("\n删掉末句后：", blocked(audit(demo, bad)) or "没拦住（有问题）")
