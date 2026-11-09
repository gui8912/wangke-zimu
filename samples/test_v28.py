# -*- coding: utf-8 -*-
"""v2.8 专项：红线分级（阻断 / 待复核 / 参考）、数字写法归一化、术语表第二轮补遗。

背景：用户实测「跑了二十分钟，最后按钮是灰的，采用不了」——旧实现五项
全部 hard，任一不过就禁用采用，等于把「提示」当成了「否决」。v2.8 的分工：
  block：只有严重截断 / 整篇复述才拦；
  warn ：术语、条款号、数字、末句、长度，提示后可二次确认采用；
  info ：覆盖率、残留口语，只作参考。
此外，模型的等值写法（第一百二十条 ↔ 第120条、百分之九十八点五 ↔ 98.5%）
不该再被当成「改了数字」，否则长稿几乎必然误报。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import llm_polish as LP          # noqa: E402
import termfix as TF             # noqa: E402
import clean_transcript as CT    # noqa: E402

PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


SRC = ("那么我们说，嗯，这个两品一械的监管，啊，主要依据是药品管理法第一百二十条。\n"
       "就是说，二零二六年我们检查了三百五十家零售药店，合格率百分之九十八点五。\n"
       "那今天就到这里，谢谢大家。")
GOOD = ("两品一械的监管主要依据是药品管理法第一百二十条。\n"
        "二零二六年共检查三百五十家零售药店，合格率达到百分之九十八点五。\n"
        "那今天就到这里，谢谢大家。")


def levels():
    print("[红线分级]")

    a = LP.audit(SRC, GOOD)
    ok("好稿：无阻断", not LP.blocking(a), LP.blocking(a))
    ok("好稿：无待复核", not LP.warnings(a), LP.warnings(a))
    ok("五项关键项仍在（hard=True）",
       sum(1 for v in a.values() if v["hard"]) == 5,
       [k for k, v in a.items() if v["hard"]])
    ok("每项都带 level", all(v.get("level") in ("block", "warn", "info")
                          for v in a.values()))

    # 等值写法：不该误报
    same = GOOD.replace("第一百二十条", "第120条").replace("百分之九十八点五", "98.5%")
    a = LP.audit(SRC, same)
    ok("条款号中/阿拉伯等价", a["条款号保留"]["pass"], a["条款号保留"]["note"])
    ok("数字中/阿拉伯等价", a["数字保留"]["pass"], a["数字保留"]["note"])
    ok("等值写法整体不告警", not LP.warnings(a), LP.warnings(a))

    # 真改数字：待复核，不否决
    a = LP.audit(SRC, GOOD.replace("百分之九十八点五", "百分之九十五点八"))
    ok("改数字 → 待复核", any("数字" in x for x in LP.warnings(a)), LP.warnings(a))
    ok("改数字 → 不阻断", not LP.blocking(a), LP.blocking(a))

    # 丢末句：待复核 + 可补回
    a = LP.audit(SRC, "\n".join(GOOD.splitlines()[:-1]))
    ok("丢末句 → 待复核", any(x.startswith("末句保留") for x in LP.warnings(a)))
    ok("丢末句 → 不阻断", not LP.blocking(a))

    # 严重截断：必须阻断
    a = LP.audit(SRC, "两品一械的监管主要依据是药品管理法。")
    ok("严重截断 → 阻断", any("长度" in x for x in LP.blocking(a)), LP.blocking(a))
    ok("阻断时 level=block", a["长度合理"]["level"] == "block")

    # 整篇复述（长度暴涨）：阻断
    a = LP.audit(SRC, GOOD + "\n" + "以上内容仅供参考，请各单位抓好落实。" * 60)
    ok("整篇复述/暴涨 → 阻断", any("长度" in x for x in LP.blocking(a)))

    # 术语大面积丢失：阻断（说明模型基本没按原文写）
    many = "、".join(["医疗器械生产质量管理规范", "药品经营质量管理规范",
                     "药品生产质量管理规范", "化妆品监督管理条例"])
    a = LP.audit("依据" + many + "开展检查。", "依据有关规定开展检查。")
    ok("术语几乎全丢 → 阻断", any("术语" in x for x in LP.blocking(a)), LP.blocking(a))

    # 参考项永不参与阻断
    a = LP.audit(SRC, GOOD.replace("各位同仁", "各位同仁这个"))
    ok("残留口语只是参考", a["残留口语（参考）"]["level"] == "info")
    ok("参考项不进 blocking", not any("残留口语" in x for x in LP.blocking(a)))
    ok("参考项也不进 warnings", not any("残留口语" in x for x in LP.warnings(a)))


def numbers():
    print("[数字 / 条款归一化]")
    cases = [
        ("第一百二十条", "第120条", "第三条", "第3条"),
        ("百分之九十八点五", "98.5%", "百分之五十", "50%"),
        ("二零二六年", "2026年", "二〇二五年", "2025年"),
    ]
    for a1, b1, a2, b2 in cases:
        ok(f"{a1} ≡ {b1}", LP._norm_text(a1) == LP._norm_text(b1),
           (LP._norm_text(a1), LP._norm_text(b1)))
        ok(f"{a2} ≡ {b2}", LP._norm_text(a2) == LP._norm_text(b2),
           (LP._norm_text(a2), LP._norm_text(b2)))
    ok("不同数字不相等", LP._norm_text("百分之九十八点五") != LP._norm_text("百分之九十五点八"))
    ok("无关正文不被改写", LP._norm_text("我们要注意储存条件") == "我们要注意储存条件")


def glossary():
    print("[术语表：第二轮补遗]")
    raw = [ln.split("|")[0].strip() for ln in
           (BASE / "terms" / "hotwords.txt").read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.startswith("#")]
    ok("热词规模 400–520", 400 <= len(raw) <= 520, len(raw))
    ok("热词无重复", len(raw) == len(set(raw)))

    for must in ("质量受权人", "药品生产质量管理规范附录无菌药品",
                 "药品经营质量管理规范附录冷藏冷冻药品的储存与运输管理",
                 "药品经营质量管理规范附录药品零售连锁管理",
                 "药品管理法", "随货同行单", "票账货相符",
                 "物料平衡", "数据完整性", "污染控制策略",
                 "无菌工艺模拟试验", "药品记录与数据管理要求",
                 "医疗器械注册质量管理体系核查指南", "化妆品安全评估技术导则"):
        ok(f"含「{must}」", must in raw)

    aliases = dict(TF.load_aliases(BASE / "terms" / "aliases.txt"))
    for short, full in (("QP", "质量受权人"), ("OOS", "检验结果超标"),
                        ("OOT", "检验结果超趋势"), ("SOP", "标准操作规程"),
                        ("QA", "质量保证"), ("QC", "质量控制")):
        ok(f"{short}=>{full}", aliases.get(short) == full)

    changes, _ = TF.expand_aliases("出现 OOS 要按 SOP 处理", TF.load_aliases(
        BASE / "terms" / "aliases.txt"))
    joined = "".join(changes)
    ok("OOS/SOP 能展开", "检验结果超标" in joined and "标准操作规程" in joined, joined)


def corrections():
    print("[错词表：按域内概率重排]")
    corr = dict(CT.load_corrections(BASE / "terms" / "corrections.txt"))
    for bad, good in (("影片", "饮片"), ("印片", "饮片"), ("驻藏", "贮藏"),
                      ("抽氧", "抽样"), ("去雾剂", "气雾剂")):
        ok(f"{bad}=>{good} 已启用", corr.get(bad) == good)
    for bad in ("备安", "处于药品", "泡制", "炮炙", "列要"):
        ok(f"{bad} 仍停用（域内可能是正常词）", bad not in corr)

    cleaner = CT.Cleaner(CT.load_rules(BASE / "rules" / "filler_words.json"),
                         CT.load_corrections(BASE / "terms" / "corrections.txt"))
    out, _, _ = cleaner.clean("中药材要按规定的条件驻藏，本次抽氧三个品种。")
    ok("上下文替换生效", "贮藏" in out and "抽样" in out, out)


def main():
    print("=" * 58)
    levels()
    numbers()
    glossary()
    corrections()
    print("=" * 58)
    print(f"{PASS}/{PASS + FAIL} 通过")
    if FAIL:
        print(f"失败 {FAIL} 项")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
