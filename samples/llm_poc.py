# -*- coding: utf-8 -*-
"""C 方案 PoC：本地 LLM 做讲稿顺滑，与规则清理正面对比。

只跑讲稿层，**绝不碰字幕**。目的是拿到可人工判断的证据，而不是直接接线。

三种处理：
  A 规则清理       —— 现状（Cleaner：语气词 / 术语修复 / 数字规整）
  B LLM 直接整理   —— 把 ASR 原文丢给模型
  C 规则清理 + LLM —— 先规则再用模型顺滑（推荐候选）

自动检查四件事（都是硬红线）：
  1. 专业术语是否被改坏
  2. 法规条款号是否被改（第一百二十条 → 第120条 之类）
  3. 数字是否被改（98.5% / 五十万元）
  4. 是否新增了原文没有的内容

运行：
  python samples/llm_poc.py                # 用 out 里的真实转写稿
  python samples/llm_poc.py -i 其他稿.txt
"""
import argparse
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

MODEL_DIR = Path(r"D:\AI\models\llm")

SYSTEM = (
    "你是药品监管培训讲稿的文字编辑。你的工作只是整理语言，不改事实。"
)

PROMPT = """把下面这段口语转写整理成书面讲稿。

硬性要求（违反任何一条都算失败）：
1. 专业术语、法规名称、条款号、数字必须原样保留，一个字都不能改；
2. 不得增加原文没有的信息，不得改写结论、不得补充举例；
3. 删除语气词、口头禅、重复；
4. 调整语序让句子通顺，可以合并短句，但不要写成提纲或分点；
5. 只输出整理后的正文，不要任何说明、不要标题。

原文：
{text}
"""

# 用于自动判定的关键项（来自原文，必须原样出现在结果里）
MUST_KEEP_TERMS = ["两品一械", "药品经营质量管理规范", "药品上市许可持有人",
                   "飞行检查", "挂靠经营", "走票过票", "一致性评价",
                   "中药饮片", "医疗器械唯一标识", "UDI"]
CLAUSE_RE = re.compile(r"第[〇零一二三四五六七八九十百千两]+(?:条|款|项|章)")
NUM_RE = re.compile(r"\d+(?:\.\d+)?%?|百分之[〇零一二三四五六七八九十百千两]+(?:点[〇零一二三四五六七八九十百千两]+)?")


def load_text(path: Path) -> str:
    if path and path.exists():
        return path.read_text(encoding="utf-8").strip()
    # 兜底样例：真实识别稿（out/test_lesson.raw.txt）
    p = BASE / "out" / "test_lesson.raw.txt"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    raise SystemExit("找不到语料，请用 -i 指定")


def find_model() -> Path:
    cands = sorted(MODEL_DIR.rglob("*Q4_K_M*.gguf"))
    if not cands:
        cands = sorted(MODEL_DIR.rglob("*.gguf"))
    if not cands:
        raise SystemExit(f"没找到 gguf 模型，请先下载到 {MODEL_DIR}")
    return cands[0]


def rule_clean(text: str) -> tuple:
    """逐句跑 Cleaner —— clean() 是单句 API，整段丢进去会抹掉句界，
    句首/句尾规则全部失效（实测：13 行被并成一句）。"""
    import clean_transcript as CT
    cfg = CT.load_rules(BASE / "rules" / "filler_words.json")
    corr = CT.load_corrections(BASE / "terms" / "corrections.txt")
    c = CT.Cleaner(cfg, corr)          # itn / termfix 默认开
    outs, changes = [], []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        o, ch, _ = c.clean(ln)
        if o.strip():
            outs.append(o)
            changes.extend(ch)
    return "\n".join(outs), changes, []


def llm_clean(text: str, llm) -> tuple:
    t0 = time.time()
    resp = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": PROMPT.format(text=text) + "\n/no_think"},
        ],
        temperature=0.2, top_p=0.9, max_tokens=1200,
    )
    out = resp["choices"][0]["message"]["content"].strip()
    # Qwen3 可能仍输出思考段，剥掉
    out = re.sub(r"<think.*?</think\s*>", "", out, flags=re.S).strip()
    out = re.sub(r"^```[a-z]*\n|```$", "", out).strip()
    return out, time.time() - t0


def coverage(src: str, out: str) -> tuple:
    """按句检查原文是否被覆盖到。返回 (覆盖率, 丢失的句子)。

    LLM 顺滑时最常见也最危险的失败不是改错字，而是**整句漏掉**
    （实测：C 方案把结尾「那今天就到这里，谢谢大家」直接丢了，
    而术语/数字/长度四项硬红线全部通过，查不出来）。
    """
    from rapidfuzz import fuzz
    sents = [s.strip() for s in re.split(r"[。！？\n]", src) if len(s.strip()) >= 6]
    miss = []
    for s in sents:
        if fuzz.partial_ratio(s, out) < 70:
            miss.append(s)
    cov = 1 - len(miss) / max(1, len(sents))
    return round(cov, 2), miss


def audit(src: str, out: str) -> dict:
    """自动红线检查。返回 {项: (是否通过, 说明)}"""
    res = {}

    lost = [t for t in MUST_KEEP_TERMS if t in src and t not in out]
    res["术语保留"] = (not lost, "全部保留" if not lost else "丢失/改写：" + "、".join(lost))

    src_c, out_c = CLAUSE_RE.findall(src), CLAUSE_RE.findall(out)
    res["条款号保留"] = (sorted(src_c) == sorted(out_c),
                       f"{len(src_c)} → {len(out_c)}")

    src_n, out_n = NUM_RE.findall(src), NUM_RE.findall(out)
    res["数字保留"] = (sorted(src_n) == sorted(out_n),
                      f"{src_n} → {out_n}" if src_n != out_n else f"{len(src_n)} 处一致")

    cov, miss = coverage(src, out)
    res["语义保留"] = (cov >= 0.6,
                      f"{cov*100:.0f}%" + ("（改写较大：" + "；".join(m[:12] for m in miss[:2]) + "）"
                                           if miss else ""))

    # 末句保留：LLM 最容易把结尾当客套话删掉（实测 C 方案丢了「那今天就到这里，谢谢大家」）
    sents = [s.strip() for s in re.split(r"[。！？\n]", src) if len(s.strip()) >= 6]
    if sents:
        from rapidfuzz import fuzz
        last = sents[-1]
        res["末句保留"] = (fuzz.partial_ratio(last, out) >= 60,
                          "保留" if fuzz.partial_ratio(last, out) >= 60
                          else f"末句疑似被删：{last[:16]}")

    # 长度暴涨通常意味着模型在"自由发挥"
    ratio = len(out) / max(1, len(src))
    res["长度合理"] = (0.6 <= ratio <= 1.25, f"{len(src)} → {len(out)} 字（{ratio:.2f}×）")

    # 残留口语
    fillers = ["呃", "嗯", "那个", "这个", "就是说", "然后呢", "好吧"]
    left = [f for f in fillers if f in out]
    res["残留口语"] = (not left, "无" if not left else "仍有：" + "、".join(left))
    return res


def render(title, text):
    return f"## {title}\n\n{text}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", default="")
    ap.add_argument("--max-chars", type=int, default=0, help="截断长度（调试用）")
    a = ap.parse_args()

    src = load_text(Path(a.input) if a.input else None)
    if a.max_chars:
        src = src[:a.max_chars]

    print(f"语料 {len(src)} 字\n")

    # A 规则清理
    t0 = time.time()
    a_text, a_chg, a_warn = rule_clean(src)
    a_dt = time.time() - t0
    print(f"[A] 规则清理完成 {a_dt*1000:.0f} ms，{len(a_text)} 字")

    model = find_model()
    print(f"[LLM] 模型 {model.name} ({model.stat().st_size/1048576:.0f} MB)")
    from llama_cpp import Llama
    llm = Llama(model_path=str(model), n_ctx=4096, n_threads=8, verbose=False)

    # B LLM 直接整理原文
    print("[B] LLM 直接整理原文…")
    b_text, b_dt = llm_clean(src, llm)
    print(f"    完成 {b_dt:.1f}s，{len(b_text)} 字")

    # C 规则 + LLM
    print("[C] 规则清理后再 LLM…")
    c_text, c_dt = llm_clean(a_text, llm)
    print(f"    完成 {c_dt:.1f}s，{len(c_text)} 字")

    rows = [("A 规则清理", a_text, a_dt), ("B LLM 直接", b_text, b_dt),
            ("C 规则+LLM", c_text, c_dt)]

    md = [f"# C 方案 PoC 对比（{len(src)} 字真实识别稿）", "",
          f"模型：`{model.name}` · 本地 CPU · 讲稿层，不涉及字幕", "",
          "## 原文（ASR 识别，未处理）", "", src, "", "---", ""]
    for name, text, dt in rows:
        md.append(render(f"{name}（{len(text)} 字，{dt*1000:.0f} ms）", text))

    md.append("---\n\n## 自动红线检查\n")
    md.append("硬红线：术语 / 条款号 / 数字 / 末句 / 长度。"
              "「语义保留」与「口语残留」是参考项——LLM 会重组句子，"
              "覆盖率低不必然等于丢内容，但低于 60% 就要逐句人工核对。\n")
    md.append("| 处理 | 术语保留 | 条款号 | 数字 | 末句 | 长度 | 语义保留（参考） | 口语残留（参考） |")
    md.append("|---|---|---|---|---|---|---|---|")
    for name, text, _ in rows:
        r = audit(src, text)
        cells = []
        for k in ("术语保留", "条款号保留", "数字保留", "末句保留", "长度合理"):
            good, note = r[k]
            cells.append(("✅ " if good else "❌ ") + note)
        for k in ("语义保留", "残留口语"):
            good, note = r[k]
            cells.append(("✅ " if good else "⚠️ ") + note)
        md.append(f"| {name} | " + " | ".join(cells) + " |")

    out_path = BASE / "samples" / "llm_poc_对比.md"
    out_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\n对比报告：{out_path}")

    # 控制台也打一份关键结论
    print("\n=== 自动检查 ===")
    for name, text, _ in rows:
        r = audit(src, text)
        hard = [k for k in ("术语保留", "条款号保留", "数字保留", "末句保留", "长度合理")
                if not r[k][0]]
        soft = [] if r["残留口语"][0] else ["口语残留"]
        print(f"  {name:12s} 硬红线：{'全部通过' if not hard else '未过 ' + '、'.join(hard)}"
              + (f"　参考：{'、'.join(soft)}仍在" if soft else ""))
        for k in hard:
            print(f"      {k}: {r[k][1]}")


if __name__ == "__main__":
    main()
