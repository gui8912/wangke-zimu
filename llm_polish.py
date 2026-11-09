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

# 【修改 v2.4】原来 8000 字直接拒绝，3 万字的课件稿根本没法润色。
# 底层本来就按 CHUNK_CHARS=700 整句分块、逐块送模型、再拼回一整篇，
# 所以把 8000 从「硬拒绝」改成「整体硬上限」（只防滥用与内存失控）：
# 长稿自动分割 → 逐块传输 → 合并回传 → 预览通过后写入最终稿。
# 可用 ASR_LLM_MAX_CHARS 调整；200000 字约 11 小时口播，足够覆盖整门课。
MAX_INPUT = int(os.environ.get("ASR_LLM_MAX_CHARS") or 200000)
AUTO_CHUNK_AT = 8000       # 超过这个字数写一行日志，说明已自动分块

# 每块送给模型的字数。Qwen3-4B 的 n_ctx=4096，中文 1 字约 1.5 token，
# 700 字输入 + 提示词 + 输出，留有余量；再大就有截断风险。
CHUNK_CHARS = 700
# 【新增 v2.7】云端模型上下文 128K–1M，700 字一块纯属浪费：
# 3 万字要发 43 次请求，每次都要重复 600 余字系统提示，慢且贵。
# 3000 字一块 → 10 次请求，提示词开销降到 1/4，长稿墙钟时间约 1/4。
# 输出约 3000–4500 token，云端 max_tokens=8192 足够；真截断还有对半自愈。
CLOUD_CHUNK_CHARS = int(os.environ.get("ASR_LLM_CLOUD_CHUNK") or 3000)
N_CTX = 4096
MAX_TOKENS = 2400
THREADS = max(4, min(12, (os.cpu_count() or 8) - 4))


class TruncatedOutput(RuntimeError):
    """模型输出被 max_tokens 截断。

    必须单独识别：llama.cpp 截断时不会报错，只把 finish_reason 置为 "length"，
    返回的是「写了一半」的正文。不检查就会把它当完整结果继续拼进讲稿，
    中间整段内容静默消失（实测 max_tokens=80 时 343 字稿只剩 161 字）。
    """

class EchoOutput(RuntimeError):
    """模型把提示词规则当成正文复述了。

    【修复 v2.5】实测 GLM-4-Flash 在「规则 + 原文」同一条 user 消息里，
    会把 7 条硬性要求原样整理一遍输出：32 块每块都复述，成稿 505 行里
    190 行是规则（38%），数字 15/20 变成 1–7、条款号翻倍。整稿 12 分钟
    白跑，红线闸门虽拦住了，但用户只看到「采用」是灰的。
    现在规则全进 system、正文单独包 <transcript>，并要求 <out> 返回；
    一旦仍出现复述，就当错误重试，绝不把污染稿拼进结果。
    """


# 【v2.5】按用户指定的固定提示词，10 条规则逐条保留（只把粘贴产生的
# 英文引号/多余空格整理成中文标点，语义一字未改）。
# 末尾追加一行技术边界说明：规则进 system、正文进 user，避免模型把规则
# 当成待整理内容复述（旧结构曾让 32 块全部输出规则，整稿报废）。
SYSTEM = (
    "你是一名熟悉药品、医疗器械、化妆品监管工作的文字编辑。任务是将网课口语"
    "转录稿整理为规范、通顺、可直接阅读的书面稿。严格遵守以下规则：\n"
    "1. 专业术语、法规名称、文件名称及文号、条款号、数字、人名、机构名必须原样保留，不得改动。\n"
    "2. 明显的语音识别错误（同音字、术语误识别），直接改正为正确的规范表述；"
    "但数字、条款号、结论性表述存疑时不得擅改，保持原文。\n"
    "3. 原文每一句的信息都必须保留，包括开头和结尾，不得省略任何内容和段落。"
    "「保留信息」不等于「保留原话」，允许改变句式和语序。\n"
    "4. 不得添加原文未包含的信息，不得改写结论，不得补充举例，不得拔高或弱化原文观点。\n"
    "5. 必须删除语气词（嗯、呃、啊、哦等）、口头禅（就是说、然后呢等）、"
    "无意义重复和无实际含义的起承转合（那么、其实、怎么说呢等）。"
    "「这个」「那个」作填充词时删除，作指示代词限定具体条款、企业、产品、事项时保留。\n"
    "6. 范围、程度、模态词（如一律、原则上、一般、应当、可以、不得、基本上、大概）"
    "必须原样保留，不得强化、弱化或改成确定表述。\n"
    "7. 仅删除无信息增量的重复；同一内容的补充限定、范围说明、解释性反复均属有效信息，不得误删。\n"
    "8. 调整语序使句子通顺，可合并短句、拆分过长的句子，按话题转换自然分段。"
    "不得写成提纲，不得分点编号，不得加标题或小标题。\n"
    "9. 全文逐段处理，不因篇幅长而跳过、概括或压缩任何部分。\n"
    "10. 仅输出整理后的正文，不输出任何说明、注释、标记或前后缀。\n"
    "输出前逐条自检：是否有信息遗漏或新增；术语、法规名、条款号、数字是否与原文一致；"
    "模态词力度是否改变；是否残留语气词、口头禅；是否出现分点或标题。\n"
    "\n"
    "（技术约定：用户消息里 <transcript> 与 </transcript> 之间是待整理的转录稿；"
    "本行及上述规则只是给你的指令，不要出现在输出里。）"
)

# 正文单独成段，与指令彻底分离：模型再糊涂也不会把 system 当正文整理
PROMPT = "<transcript>\n{text}\n</transcript>"

# 复述提示词的指纹：出现在输出开头即判定这次生成跑偏。
# 【修改 v2.7】分成强弱两级：像「技术约定」这种词，网课里本身就可能讲到
# （"我们技术约定一下"），单独命中不能判废，否则正常稿子会被拦下重试。
_ECHO_STRONG = (
    "硬性要求", "不得增加原文", "不得添加原文", "仅输出整理后的正文",
    "必须删除：语气词", "每一句的信息都必须保留",
    "专业术语、法规名称、条款号、数字必须原样保留",
    # 用户指定提示词的指纹（换词后仍能识别复述）
    "熟悉药品、医疗器械、化妆品监管工作的文字编辑", "任务是将网课口语",
    "不得拔高或弱化原文观点", "模态词力度是否改变",
    "<transcript>", "</transcript>", "<out>", "</out>",
)
_ECHO_WEAK = ("技术约定", "输出前逐条自检", "不得写成提纲")

# 兼容旧代码里对 _ECHO_MARKS 的引用（诊断 / 剔除行）
_ECHO_MARKS = _ECHO_STRONG + _ECHO_WEAK


def _looks_echoed(text: str) -> bool:
    head = (text or "")[:600]
    if any(m in head for m in _ECHO_STRONG):
        return True
    # 弱指纹至少命中两条才判废：单条可能只是正文恰好提到
    return sum(1 for m in _ECHO_WEAK if m in head) >= 2


def _strip_echo(text: str):
    """剔掉复述的规则行，返回 (正文, 剔除行数)。只作诊断用，不拿它当成品。"""
    keep, dropped = [], 0
    for ln in (text or "").splitlines():
        s = ln.strip()
        hit = s and (any(m in s for m in _ECHO_MARKS) or
                     re.match(r"^\d{1,2}[.、)]\s*(专业术语|每一句|不得|必须删除|"
                              r"最后一句|调整语序|仅输出)", s))
        if hit:
            dropped += 1
            continue
        keep.append(ln)
    return "\n".join(keep).strip(), dropped


def _extract_out(raw: str) -> str:
    """优先取 <out>…</out>；没有标签就把整段当正文，交给回显检测判断。"""
    m = re.search(r"<out>\s*(.*?)\s*</out>", raw or "", re.S)
    body = m.group(1) if m else (raw or "")
    body = re.sub(r"<think.*?</think\s*>", "", body, flags=re.S)
    body = body.strip()
    body = re.sub(r"^```[a-z]*\s*", "", body)
    body = re.sub(r"```$", "", body).strip()
    return body


def _sanity(src: str, out: str):
    """块级健康检查：复述提示词 / 长度塌陷 / 没有中文，任一命中即判废。"""
    if not (out or "").strip():
        return False, "输出为空"
    if _looks_echoed(out):
        return False, "复述了提示词规则"
    if len(out) < len(src) * 0.45:
        return False, f"输出过短（{len(src)} → {len(out)} 字）"
    if not re.search(r"[\u4e00-\u9fff]", out):
        return False, "没有中文正文"
    return True, ""

# 句末标点，用于切分「句子单元」做分块
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])")
CLAUSE_RE = re.compile(r"第[〇零一二三四五六七八九十百千两\d]+(?:条|款|项|章|节|编)")
NUM_RE = re.compile(
    r"\d+(?:\.\d+)?%?"
    r"|百分之[〇零一二三四五六七八九十百千两]+(?:点[〇零一二三四五六七八九十百千两]+)?"
)
FILLERS = ("呃", "嗯", "那个", "这个", "就是说", "然后呢", "好吧")


def _partial_ratio(s: str, out: str) -> float:
    """片段相似度 0–100。

    【新增 v2.7】rapidfuzz 未安装时不再让整次润色崩掉：原来 audit 里
    直接 `from rapidfuzz import fuzz`，缺包时润色已跑完却在校验阶段抛
    ImportError，十几分钟的结果整份丢掉。这里用 3-gram 覆盖率兜底，
    精度略低但方向一致（只用于「是否漏句」的粗判，不参与改写）。
    """
    try:
        from rapidfuzz import fuzz
        return float(fuzz.partial_ratio(s, out))
    except Exception:
        s = s or ""
        out = out or ""
        if not s:
            return 0.0
        n = 3
        grams = [s[i:i + n] for i in range(max(1, len(s) - n + 1))]
        hit = sum(1 for g in grams if g in out)
        return 100.0 * hit / max(1, len(grams))

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
    【修改 v2.2】云端模型启用时优先走云端；没配好就明确说缺什么，
    不静默回退本地（用户以为在用免费额度，其实 CPU 在慢慢跑）。
    """
    import cloud_llm as CL
    cfg = CL.load_config()
    if cfg.get("enabled"):
        if not cfg.get("api_key") and not str(cfg.get("base_url") or "").startswith("http://127.0.0.1"):
            return False, "云端模型已启用但没填 API Key，请到「润色模型设置」里补上"
        if not cfg.get("base_url") or not cfg.get("model"):
            return False, "云端模型的接口地址 / 模型名没填全"
        return True, f"云端 {cfg['model']}（{CL.preset_info(cfg.get('preset'))['label']}）"
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
    import cloud_llm as CL
    cfg = CL.load_config()
    if cfg.get("enabled"):
        # 云端客户端不占内存、可随时换配置，不缓存
        log(f"使用云端润色模型 {cfg['model']}（{cfg['base_url']}）")
        return CL.CloudChat(cfg, log=log)
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
            if not seg:
                continue
            # 【新增 v2.7】整段粘贴、没有句末标点的稿子：单句超长时按字数硬切，
            # 否则一个 3 万字的「句子」会整块塞进模型，直接超上下文。
            if len(seg) <= limit:
                units.append(seg)
            else:
                units.extend(seg[i:i + limit]
                             for i in range(0, len(seg), limit))
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


def estimate_chunks(text) -> int:
    """【新增 v2.7】按当前后端估算块数，供服务端算「卡死」预算。

    云端 3000 字一块、本地 700 字一块，服务端不能再用固定 CHUNK_CHARS
    估——会把云端长稿的合理耗时误判成卡死。
    """
    try:
        import cloud_llm as CL
        limit = (CLOUD_CHUNK_CHARS if CL.load_config().get("enabled")
                 else CHUNK_CHARS)
    except Exception:
        limit = CHUNK_CHARS
    return max(1, len(chunk_text(text or "", limit=limit)))


# ---------------------------------------------------------------- 推理

def _polish_chunk(llm, text):
    # /no_think 只对本地 Qwen3 有意义（默认先输出思考段），云端模型只会
    # 把它当噪声，所以按后端区分
    is_cloud = hasattr(llm, "estimate")
    user = PROMPT.format(text=text) + ("" if is_cloud else "\n/no_think")
    # 【修复 v2.5】DeepSeek V4 这类模型会把「思考」也算进 max_tokens：
    # 实测 746 字一块 reasoning 就吃掉 1033 token，2400 的上限会把正文挤断。
    # 云端放宽到 8192；本地 llama.cpp 的 n_ctx=4096，保持原值不动。
    limit = 8192 if is_cloud else MAX_TOKENS
    resp = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.15, top_p=0.9, max_tokens=limit,
    )
    ch = (resp.get("choices") or [{}])[0]
    raw = (ch.get("message") or {}).get("content") or ""
    # modified(v1.8):截断必须当错误抛出。llama.cpp 截断时不报错，只把
    # finish_reason 置为 "length" 并返回「写了一半」的正文；不拦就会静默丢内容
    # （实测 max_tokens=80 时，343 字稿只剩 161 字，四项红线里只有覆盖率会掉）。
    if ch.get("finish_reason") == "length":
        raise TruncatedOutput(
            f"输出达到 {limit} token 上限被截断（本块 {len(text)} 字）")
    out = _extract_out(raw)
    # 【修复 v2.5】复述提示词 = 这次生成跑偏了。剔出来的残稿不能当成品，
    # 直接抛错让上层重试；否则会把规则文本拼进成稿，红线也救不回来。
    if _looks_echoed(out):
        cleaned, dropped = _strip_echo(out)
        raise EchoOutput(
            f"输出复述了提示词规则（剔除 {dropped} 行后仅剩 {len(cleaned)} 字）")
    if not out:
        raise RuntimeError("模型没有返回内容")
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
    except EchoOutput:
        # 复述提示词多为采样偶发，原样重试一次；仍复述就交给上层判定，
        # 由 polish() 决定「首块直接中止」还是「这一块保留原文」
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


def _tail_present(part: str, tail: str) -> bool:
    """块尾句是否已经出现在这一块里（允许模型只去掉了句末标点）。

    【新增 v2.7】原来只做严格 `tail in part`，模型把句号改成逗号或删掉标点
    就会误判成「缺失」，于是同一句被追加两遍。
    这里只放宽到「去掉句末标点后原样出现」，不做模糊匹配——实测课程里
    相邻句子高度相似（"这是第98句…" / "这是第99句…"），模糊匹配会把
    真的被删掉的末句误判成已保留，那才是更严重的内容丢失。
    """
    if not tail:
        return True
    if tail in part:
        return True
    core = tail.rstrip("。！？!?…；;，, ")
    if len(core) < 4:
        return False
    return part.rstrip("。！？!?…；;，, ").endswith(core)


def polish(text, log=print, progress=None):
    """润色整段讲稿。返回 {source, polished, tail, audit, diff, chunks, elapsed, model}。

    【新增 v2.4】progress(done, total, stage) 每块回调，服务端据此提供
    /api/llm/progress 给前端轮询；长稿不再是一个没有任何反馈的黑盒 POST。
    """
    def emit(done, total, stage):
        if callable(progress):
            try:
                progress(done, total, stage)
            except Exception:
                pass                  # 进度上报失败绝不影响润色本身

    text = (text or "").strip()
    if not text:
        raise ValueError("没有可润色的文本")
    if len(text) > MAX_INPUT:
        # 【修改 v2.4】8000 字的硬拒绝已取消：底层按整句分块，
        # 现在这里只是防滥用 / 防内存失控的总量上限。
        raise ValueError(f"文本 {len(text)} 字，超过硬上限 {MAX_INPUT} 字，"
                         f"可用 ASR_LLM_MAX_CHARS 调整")
    llm = _ensure(log)
    # 【新增 v2.7】云端用大块（3000 字），本地小模型仍用 700 字。
    is_cloud = hasattr(llm, "estimate")
    limit = CLOUD_CHUNK_CHARS if is_cloud else CHUNK_CHARS
    chunks = chunk_text(text, limit=limit)
    if len(text) > AUTO_CHUNK_AT:
        log(f"长稿 {len(text)} 字，自动分 {len(chunks)} 块"
            f"（每块约 {limit} 字）逐块润色后合并")
    parts, t0, restored = [], time.time(), 0
    total = len(chunks)
    emit(0, total, "开始分块润色")
    fallback = 0
    with _RUN_LOCK:
        for i, ck in enumerate(chunks, 1):
            emit(i - 1, total, f"润色第 {i}/{total} 块")
            log(f"润色第 {i}/{len(chunks)} 块（{len(ck)} 字）")
            # 【修复 v2.5】首块先验收：模型若在复述提示词 / 大段丢内容，
            # 立刻中止，别让 32 块、十几分钟全部白跑（今天就白跑过两次）。
            try:
                part = _polish_with_split(llm, ck)   # modified(v1.8):截断自愈
            except EchoOutput as exc:
                if i == 1:
                    raise RuntimeError(
                        f"第 1 块输出异常（{exc}），已中止以免整稿白跑。"
                        f"请重试一次；若反复出现，换本地模型或稍后再试") from exc
                part = ""
            okk, why = _sanity(ck, part)
            if not okk:
                if i == 1:
                    raise RuntimeError(
                        f"第 1 块输出异常（{why}），已中止以免整稿白跑。"
                        f"请重试一次；若反复出现，换本地模型或稍后再试")
                # 单块出问题不拖垮整稿：这一块保留原文，宁可略生硬也不丢内容
                fallback += 1
                log(f"第 {i} 块未通过检查（{why}），保留原文")
                part = ck
            # 【新增 v2.4】块是人为切的，模型常把块尾客套句删掉。
            # 除最后一块外，块尾句由程序补回——这是分块引入的丢失，
            # 不该让用户逐块去找；原文真正的末句仍交给界面「补回原末句」，
            # 不静默补，红线闸门保持有效。
            tail = last_sentence(ck)
            if (i < len(chunks) and tail and not _tail_present(part, tail)):
                part = (part.rstrip() + "\n" + tail).strip()
                restored += 1
            parts.append(part)
            emit(i, total, f"第 {i}/{total} 块完成")
    emit(total, total, "合并与红线检查")
    out = "\n".join(p for p in parts if p)
    if not out.strip():
        raise RuntimeError("模型没有返回内容")
    m = find_model()
    # 【新增 v2.2】云端模型把这一稿的 token 用量与折算费用带回界面
    usage = llm.estimate() if hasattr(llm, "estimate") else None
    return {
        "source": text,
        "polished": out,
        "tail": last_sentence(text),
        "audit": audit(text, out),
        "diff": diff_ops(text, out),
        "chunks": len(chunks),
        "chunk_chars": limit,
        "tail_restored": restored,     # 【新增 v2.4】自动补回的块尾句数量
        "fallback_chunks": fallback,   # 【新增 v2.5】保留原文、未润色的块数
        "elapsed": round(time.time() - t0, 1),
        "model": getattr(llm, "model_name", None) or (m.name if m else ""),
        "backend": "cloud" if usage else "local",
        "usage": usage,
    }


# ---------------------------------------------------------------- 审计

def must_keep_terms(src):
    """从项目术语表里取「原文中真的出现了」的术语当检查项。

    不硬编码清单：硬编码只对一份稿子有效，术语表才是项目里真正的真相源。

    【修复 v2.7】返回「可接受形式组」而不是扁平词表：
      · 热词 → ("药品经营质量管理规范",)，必须原样出现；
      · 简称 → ("医疗器械GMP", "医疗器械生产质量管理规范")，
        保留简称或展开成全称都算通过。
    原来两侧都当独立硬项，`医疗器械GMP=>…` 会把简称本身判成「丢失」，
    可模型按规则保留简称、根本不展开，硬红线永远不过、「采用」永远是灰的。
    """
    groups = []
    p = TERMS / "hotwords.txt"
    if p.exists():
        for ln in p.read_text(encoding="utf-8-sig").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            t = ln.split("|", 1)[0].strip()
            # 4 字以上才检查：「药品」「企业」这类太泛，留着是常识，不用盯
            if len(t) >= 4 and t in src:
                groups.append((t,))

    p = TERMS / "aliases.txt"
    if p.exists():
        for ln in p.read_text(encoding="utf-8-sig").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=>" not in ln:
                continue
            short, _, full = ln.partition("=>")
            short, full = short.strip(), full.strip()
            forms = tuple(x for x in (short, full) if len(x) >= 4)
            if forms and any(x in src for x in forms):
                groups.append(forms)
    # 去重但保持稳定顺序
    seen, out = set(), []
    for g in groups:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def coverage(src, out):
    """按句检查原文被覆盖的比例。返回 (覆盖率, 丢失句子列表)。

    LLM 顺滑最危险的失败不是改错字，而是**整句漏掉**——PoC 里就丢了结尾，
    而术语/数字/长度四项全过，完全查不出来。这条是专门为它加的。

    【修改 v2.8】比对前先做数字/条款号归一化：模型把「第一百二十条」
    写成「第120条」时，逐字相似的句子会被判成 0% 覆盖，属于纯误报。
    """
    src_n, out_n = _norm_text(src), _norm_text(out)
    sents = [s.strip() for s in re.split(r"[。！？\n]", src_n) if len(s.strip()) >= 6]
    if not sents:
        return 1.0, []
    miss = [s for s in sents if _partial_ratio(s, out_n) < 70]
    return round(1 - len(miss) / len(sents), 2), miss


# ---------------------------------------------------------------- 红线
# 【新增 v2.8】数字 / 条款号的归一化比对。
# 模型常把「第一百二十条」写成「第120条」、「百分之九十八点五」写成「98.5%」、
# 「二零二六年」写成「2026年」——语义完全没变，旧实现按字符串比对必然判成
# 「改动了数字」，于是好稿子被挡在采用门外。这里先归一化再比集合。
_CN_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}
YEAR_CN_RE = re.compile(r"[〇零一二三四五六七八九]{4}年")


def _cn_int(s):
    """中文数字转整数（支持 十/百/千）；无法解析返回 None。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = num = 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            total += (num or 1) * _CN_UNIT[ch]
            num = 0
        else:
            return None
    return total + num


def _prep_numbers(text):
    """把「二零二六年」这类中文年份先换成阿拉伯数字，便于统一比对。"""
    def rep(m):
        return "".join(str(_CN_DIGIT[c]) for c in m.group(0)[:4]) + "年"
    return YEAR_CN_RE.sub(rep, text or "")


def _norm_num(tok):
    tok = (tok or "").strip().replace("％", "%")
    if tok.startswith("百分之"):
        body = tok[3:].rstrip("%")
        if "点" in body:
            a, _, b = body.partition("点")
            iv, fv = _cn_int(a), _cn_int(b)
            return f"{iv}.{fv}%" if iv is not None and fv is not None else tok
        iv = _cn_int(body)
        return f"{iv}%" if iv is not None else tok
    if tok.endswith("%"):
        try:
            return f"{float(tok[:-1]):g}%"
        except ValueError:
            return tok
    try:
        return f"{float(tok):g}"
    except ValueError:
        return tok


def _norm_clause(tok):
    m = re.match(r"第(.+?)([条款项章节编])$", tok or "")
    if not m:
        return tok
    n = _cn_int(m.group(1))
    return f"第{n}{m.group(2)}" if n is not None else tok


def _norm_text(text):
    """整段文本的数字归一化，用于「是否漏句」这类相似度比对。

    只做三种确定等价的替换：中文年份→阿拉伯、条款号→阿拉伯、百分比→小数。
    不做通用中文数字转换（「三百五十家」这种没有可靠边界，宁可不转）。
    """
    t = _prep_numbers(text)
    t = CLAUSE_RE.sub(lambda m: _norm_clause(m.group(0)), t)
    t = NUM_RE.sub(lambda m: _norm_num(m.group(0)), t)
    return t


def _item(pass_, level, note, hard=None):
    """一条检查结果。level：block=拦住不让采用 / warn=可确认后采用 / info=参考。"""
    return {"pass": bool(pass_), "level": level,
            "hard": bool(level != "info" if hard is None else hard),
            "note": note}


def audit(src, out):
    """红线检查。返回 {项: {pass, level, hard, note}}，保持顺序供前端直接渲染。

    【v2.8 放宽】原来五项全部 hard，任一不过就禁用「采用」——用户实测
    「跑了二十分钟，最后不给采用」，等于白做。现在分三级：
      block：只有「输出严重截断 / 整段复述」这类真的不能用的稿子才拦；
      warn ：术语、条款号、数字、末句、长度——提示待复核，可二次确认后采用；
      info ：覆盖率、残留口语，只作参考。
    不放弃检查，只把「否决权」收窄到真·废稿，判断权还给使用者。
    """
    res = {}

    terms = must_keep_terms(src)
    # 【修改 v2.7】每组允许多种可接受写法（简称 / 全称），命中任一即算保留
    lost = ["／".join(g) for g in terms if not any(x in out for x in g)]
    miss_ratio = len(lost) / max(1, len(terms))
    res["术语保留"] = _item(
        not lost, "block" if miss_ratio > 0.5 else "warn",
        ("原文未命中术语表词条，跳过" if not terms else
         f"{len(terms)} 条全部保留") if not lost
        else f"{len(terms)} 条里丢了 {len(lost)} 条：" + "、".join(lost[:6]))

    sc = [_norm_clause(t) for t in CLAUSE_RE.findall(_prep_numbers(src))]
    oc = [_norm_clause(t) for t in CLAUSE_RE.findall(_prep_numbers(out))]
    same = sorted(sc) == sorted(oc)
    res["条款号保留"] = _item(
        same, "warn",
        ("原文没有条款号，跳过" if not sc else f"{len(sc)} 处一致") if same
        else f"{len(sc)} → {len(oc)}：" + "、".join(sorted(set(sc) - set(oc))[:4]))

    sn = [_norm_num(t) for t in NUM_RE.findall(_prep_numbers(src))]
    on = set(_norm_num(t) for t in NUM_RE.findall(_prep_numbers(out)))
    # 【v2.8】只查「原文的数字有没有被打丢/改掉」：模型把中文数字写成阿拉伯
    # 数字会凭空多出 token（第一百二十条 → 第120条），多出来的不算错。
    missing = [t for t in dict.fromkeys(sn) if t not in on]
    res["数字保留"] = _item(
        not missing, "warn",
        ("原文没有数字，跳过" if not sn else f"{len(sn)} 处一致") if not missing
        else f"原文 {len(sn)} 处，结果缺：" + "、".join(missing[:4]))

    cov, miss = coverage(src, out)
    sents = [s.strip() for s in re.split(r"[。！？\n]", _norm_text(src)) if len(s.strip()) >= 6]
    if sents:
        last = sents[-1]
        keep = _partial_ratio(last, _norm_text(out)) >= 60
        res["末句保留"] = _item(keep, "warn",
                               "保留" if keep else f"疑似被删：{last[:18]}")
    else:
        # modified(v1.8) 新增:原文过短（一句话不到 6 字）时也要给出这一项。
        res["末句保留"] = _item(True, "warn", "原文过短，跳过")

    ratio = len(out) / max(1, len(src))
    if ratio < 0.4 or ratio > 1.8:
        res["长度合理"] = _item(False, "block",
                               f"{len(src)} → {len(out)} 字（{ratio:.2f}×），"
                               "疑似严重截断或复述提示词", hard=True)
    else:
        res["长度合理"] = _item(
            0.6 <= ratio <= 1.25, "warn" if ratio < 0.6 or ratio > 1.25 else "info",
            f"{len(src)} → {len(out)} 字（{ratio:.2f}×）", hard=True)

    # 以下两项只作参考：LLM 会重组句子，覆盖率低不必然等于丢内容；
    # 残留口语是规则层删不掉的（「这个/那个」后面跟名词，删了不通），
    # 正是 LLM 的增量所在，不该反过来当失败。
    res["内容覆盖（参考）"] = _item(
        cov >= 0.6, "info",
        f"{cov * 100:.0f}%" + (
            "；改写较大：" + "；".join(m[:12] for m in miss[:2]) if miss else ""))
    left = [f for f in FILLERS if f in out]
    res["残留口语（参考）"] = _item(
        not left, "info", "无" if not left else "仍有：" + "、".join(left))
    return res


def blocked(audit_result):
    """所有「非参考项」里没过关的原因，供前端逐条展示。"""
    return [f"{k}：{v['note']}" for k, v in audit_result.items()
            if v.get("hard") and not v.get("pass")]


def blocking(audit_result):
    """真正的否决项（会禁用「采用」）。只有严重截断 / 整篇复述会进来。"""
    return [f"{k}：{v['note']}" for k, v in audit_result.items()
            if v.get("level") == "block" and not v.get("pass")]


def warnings(audit_result):
    """待复核项：允许采用，但需要用户再确认一次。"""
    return [f"{k}：{v['note']}" for k, v in audit_result.items()
            if v.get("level") == "warn" and not v.get("pass")]


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
