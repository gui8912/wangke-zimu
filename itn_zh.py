# -*- coding: utf-8 -*-
"""受限 ITN（逆文本规整）：把明确计量语境里的中文数字转成阿拉伯数字。

为什么不用现成的全量 ITN（wetext / cn2an.transform 整段替换）：

1. 全量 ITN 会把「第一百二十条」转成「第120条」、《医疗器械监督管理条例》
   「第五条第二款」转成「第5条第2款」——法规条款号在监管材料里必须保持原样；
2. 也会把「四不两直」「双随机一公开」「四个最严」「三品一械」「十四五」
   这类含中文数字的固定表达改坏；
3. wetext 依赖 pynini，Windows 上需要 OpenFst 本地编译，装不上。

所以这里用**白名单**：只有命中下面 5 类明确计量模式才转，其余一律不动。
宁可少转，不可错转 —— 与原稿保护、不确定不改的既有原则一致。

对外只有一个函数：
    normalize(text, min_qty=10) -> (新文本, 变更说明列表)

依赖 cn2an（纯 Python，无编译依赖）。缺失时降级为原样返回 + 一条警告，
绝不让整条清洗链路或服务启动失败。
"""

from __future__ import annotations

import re

try:
    import cn2an as _cn2an
    AVAILABLE = True
except Exception:            # pragma: no cover - 环境缺包时走降级分支
    _cn2an = None
    AVAILABLE = False

# 中文数字字符集（含〇 U+3007 与零；含万亿，否则「三亿元」只会匹配到「三」）
CN = "〇零一二三四五六七八九十百千万亿两"

DIGIT = {"〇": "0", "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
         "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}

# ── 白名单模式 1：百分比 百分之九十八点五 → 98.5% ──
RE_PCT = re.compile(r"百分之([" + CN + r"]+)(?:点([" + CN + r"]+))?")

# ── 白名单模式 2：四位年份 二零二六年 → 2026年（限 1900–2100） ──
RE_YEAR4 = re.compile(r"(?<![" + CN + r"])([〇零一二三四五六七八九]{4})年")

# ── 白名单模式 3：月日 三月二十九号 → 3月29号 ──
RE_MD = re.compile(r"([一二三四五六七八九十]{1,3})月([一二三四五六七八九十]{1,3})([日号])")

# ── 白名单模式 4：数量词 + 单位（数值 ≥ min_qty 才转） ──
# 前置「第」一律不转：这是条款号保护（第一百二十条 / 第五条第二款）
UNITS = ("人|家|户|件|批|台|套|次|倍|吨|公斤|千克|克|平方米|立方米|公里|米|"
         "万元|亿元|元|天|小时|分钟|秒|岁|个|种|名|条|份|张|辆|栋|层|间|"
         "瓶|盒|箱|支|袋|包|片|粒")
RE_QTY = re.compile(r"(?<!第)([" + CN + r"]+)(?=[\s]?(?:" + UNITS + r"))")

# ── 白名单模式 5：文号 第739号（阿拉伯数字是正式写法，转） ──
RE_NUM_ORDER = re.compile(r"第([" + CN + r"]+)号")

# ── 保护：法规条款号整体遮蔽 ──
# 只用 (?<!第) 前缀断言是不够的：「第二十条」里「十」前面是「二」不是「第」，
# 会被当成数量词转成「第二10条」（实测复现）。所以先把整个条款号挖出来占位，
# 做完其余规整再原样放回。
RE_CLAUSE = re.compile(
    r"第[" + CN + r"]+(?:条|款|项|章|节|编|部分|目)")

_PH = "\x00{}\x00"          # 占位符：正文不会出现 \x00

YEAR_MIN, YEAR_MAX = 1900, 2100


def _num(s: str):
    """中文数字串 → 整数；解析失败返回 None（调用方据此放弃转换）"""
    if not _cn2an or not s:
        return None
    try:
        v = _cn2an.cn2an(s, "smart")
    except Exception:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and float(v).is_integer():
        return int(v)
    return None


def normalize(text: str, min_qty: int = 10) -> tuple:
    """受限数字规整。返回 (新文本, [变更说明...])。

    min_qty：数量词低于该值保留汉字（「三个人」不转，「350人」转）。
    中文书面语习惯保留个位数汉字，同时避免「一个/一些」这类虚化量词被改坏。
    """
    if not text:
        return text, []
    if not AVAILABLE:
        return text, ["数字规整已跳过：未安装 cn2an（pip install cn2an）"]

    log: list[str] = []

    # 0) 文号「第X号」先转阿拉伯 —— 正式文号本来就写作「第739号」
    def _order(m):
        v = _num(m.group(1))
        if v is None:
            return m.group(0)
        log.append(f"第{m.group(1)}号→第{v}号")
        return f"第{v}号"

    text = RE_NUM_ORDER.sub(_order, text)

    # 1) 遮蔽条款号（条/款/项/章/节/编/部分/目），做完其余规整再原样放回。
    #    这是「第二十条」不被拆成「第二10条」的唯一可靠办法。
    protected: list[str] = []

    def _hide(m):
        protected.append(m.group(0))
        return _PH.format(len(protected) - 1)

    text = RE_CLAUSE.sub(_hide, text)

    # 2) 百分比
    def _pct(m):
        n = _num(m.group(1))
        if n is None:
            return m.group(0)
        if m.group(2):
            f = _num(m.group(2))
            if f is None:
                return m.group(0)
            log.append(f"百分之{m.group(1)}点{m.group(2)}→{n}.{f}%")
            return f"{n}.{f}%"
        log.append(f"百分之{m.group(1)}→{n}%")
        return f"{n}%"

    text = RE_PCT.sub(_pct, text)

    # 2) 四位年份（逐位映射；范围外不动，防识别错字被"规整"成假年份）
    def _year(m):
        s = m.group(1)
        d = "".join(DIGIT[c] for c in s)
        if not (YEAR_MIN <= int(d) <= YEAR_MAX):
            return m.group(0)
        log.append(f"{s}年→{d}年")
        return d + "年"

    text = RE_YEAR4.sub(_year, text)

    # 3) 月日
    def _md(m):
        mo, dy = _num(m.group(1)), _num(m.group(2))
        if mo is None or dy is None or not (1 <= mo <= 12) or not (1 <= dy <= 31):
            return m.group(0)
        log.append(f"{m.group(1)}月{m.group(2)}{m.group(3)}→{mo}月{dy}{m.group(3)}")
        return f"{mo}月{dy}{m.group(3)}"

    text = RE_MD.sub(_md, text)

    # 4) 数量词 + 单位
    def _qty(m):
        s = m.group(1)
        # 「五十万」保留万/亿，不摊成 500000
        scale, core = "", s
        if s.endswith("万") and len(s) > 1:
            scale, core = "万", s[:-1]
        elif s.endswith("亿") and len(s) > 1:
            scale, core = "亿", s[:-1]
        v = _num(core)
        if v is None:
            return m.group(0)
        # 【修改 v1.4】阈值要按「实际量级」判断：三亿 的值是 3×1e8，不能因 core=3 被跳过
        mult = {"万": 10000, "亿": 100000000}.get(scale, 1)
        if v * mult < min_qty:
            return m.group(0)
        log.append(f"{s}→{v}{scale}")
        return f"{v}{scale}"

    text = RE_QTY.sub(_qty, text)

    # 6) 还原条款号：原样放回，一个字都不改
    for i, s in enumerate(protected):
        text = text.replace(_PH.format(i), s)

    return text, log


def main() -> None:
    """命令行自检：python itn_zh.py"""
    cases = [
        ("《药品管理法》第一百二十条规定，第五条第二款另有规定的除外。", None),
        ("四不两直、双随机一公开、四个最严、三品一械，十四五期间持续推进。", None),
        ("国务院令第739号《医疗器械监督管理条例》自二零二一年六月一日起施行。", None),
        ("合格率达到百分之九十八点五，比去年提高三个百分点。", None),
        ("二零二六年三月二十九号开展飞行检查，共检查三百五十家零售药店。", None),
        ("抽检一千二百三十四批次，不合格十二批次，货值金额五十万元。", None),
        ("发现缺陷项十八条，其中严重缺陷三条，要求七日内整改。", None),
    ]
    for src, _ in cases:
        out, log = normalize(src)
        print("原文:", src)
        print("结果:", out)
        print("变更:", "；".join(log) if log else "（无，按预期保护）")
        print("-" * 76)


if __name__ == "__main__":
    main()
