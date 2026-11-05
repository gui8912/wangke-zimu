"""用 edge-tts 生成一段模拟药监讲课音频，用于端到端测试。
文本刻意保留口语词，术语用正确写法 —— 看 ASR 会不会听错、清洗能不能救回来。
"""
import asyncio
import sys
from pathlib import Path

TEXT = (
    "呃，各位同仁大家好。那么今天我们来讲一下两品一械的这个监管要求。"
    "嗯，那个，我们来看第一个部分，就是药品经营质量管理规范的基本框架。"
    "呃这个，大家在日常检查中间呢，一定要注意那个批号的追溯问题。"
    "就是说，我们发现很多企业在冷链药品的温湿度监测上是不到位的。"
    "啊，这里要特别强调的是，药品上市许可持有人不得委托无资质的企业储运药品。"
    "飞行检查的时候，我们会重点看记录是不是完整。"
    "假药和劣药的界定标准，大家一定要搞清楚。"
    "嗯，就是说呢，无证经营和挂靠经营，走票过票，这些都属于严重违法行为。"
    "禁止出租出借证照，这一条是红线。"
    "我们再看一致性评价这块，国家是有一个明确的时间表的。"
    "中药饮片的炮制，也是有规范的。"
    "医疗器械唯一标识，也就是UDI，这个大家要熟悉。"
    "好吧，那今天就到这里，谢谢大家。"
)

VOICE = "zh-CN-YunxiNeural"
OUT = Path(__file__).resolve().parent / "test_lesson.mp3"


async def main():
    import edge_tts
    tts = edge_tts.Communicate(TEXT, VOICE, rate="-5%")
    await tts.save(str(OUT))
    print(f"已生成：{OUT}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"生成失败：{e}", file=sys.stderr)
        sys.exit(1)
