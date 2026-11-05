#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
药品监管网课转字幕 · 图形界面
==============================
双击 启动界面.bat，或运行：python gui.py
浏览器会自动打开 http://127.0.0.1:7860
"""

import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import gradio as gr  # noqa: E402

import transcribe as T  # noqa: E402

MODEL_CHOICES = [
    ("Qwen3-ASR-1.7B · 精度优先（1 小时约 44 分钟）", "qwen3-asr"),
    ("Paraformer · 速度优先（1 小时约 4.5 分钟），支持术语热词", "paraformer"),
]

CSS = """
.gradio-container {max-width: 1240px !important; margin: 0 auto;}
#hd h1 {font-size: 22px; margin-bottom: 2px;}
#hd p {color: #6b7280; margin-top: 0; font-size: 13px;}
#logbox textarea {font-family: ui-monospace, Consolas, monospace; font-size: 12px;}
"""


def run(file_path, model_key, do_clean, language):
    logs = []

    def log(msg):
        logs.append(str(msg))

    if not file_path:
        return ("请先上传音频或视频文件。", "", "", "", "", "", None, None)

    try:
        src = Path(file_path)
        log(f"文件：{src.name}（{src.stat().st_size / 1048576:.1f} MB）")
        name = dict((v, k) for k, v in MODEL_CHOICES).get(model_key, model_key)
        log(f"引擎：{name}")
        r = T.run_pipeline(src, model_key, bool(do_clean),
                           language=language, log=log)
        log("全部完成，可在下方标签页查看或下载。")
        return (
            "\n".join(logs),
            r["raw_srt_text"],
            r["doc_text"],
            r["clean_srt_text"] or "（未启用口语清理）",
            r["clean_doc_text"] or "（未启用口语清理）",
            r["report_text"] or "（未启用口语清理）",
            str(r["clean_srt_path"] or r["srt_path"]),
            str(r["clean_txt_path"] or r["txt_path"]),
        )
    except Exception as exc:
        logs.append(f"处理失败：{exc}")
        logs.append(traceback.format_exc())
        return ("\n".join(logs), "", "", "", "", "", None, None)


with gr.Blocks(theme=gr.themes.Soft(), css=CSS, title="药品监管网课转字幕") as demo:
    with gr.Column(elem_id="hd"):
        gr.Markdown(
            "# 药品监管网课 · 语音转字幕\n"
            "纯本地运行，音频不出本机 · 适配药品与医疗器械监督管理术语 · "
            "自动清理口语词"
        )

    with gr.Row():
        with gr.Column(scale=3):
            audio_in = gr.File(
                label="上传音频 / 视频",
                file_types=["audio", "video"],
                type="filepath",
            )
            with gr.Row():
                model_in = gr.Dropdown(
                    choices=MODEL_CHOICES,
                    value="qwen3-asr",
                    label="识别引擎",
                    scale=2,
                )
                lang_in = gr.Dropdown(
                    choices=[("中文", "Chinese"), ("自动检测", "auto")],
                    value="Chinese",
                    label="语种",
                    scale=1,
                )
            clean_in = gr.Checkbox(
                value=True,
                label="自动清理口语词（呃、那个、就是说…）并校正专业术语错字",
            )
            run_btn = gr.Button("开始转写", variant="primary", size="lg")

        with gr.Column(scale=2):
            log_out = gr.Textbox(
                label="运行日志",
                lines=9,
                max_lines=9,
                elem_id="logbox",
                placeholder="等待开始…",
            )

    with gr.Tabs():
        with gr.Tab("字幕（原始）"):
            srt_out = gr.Textbox(label="SRT 字幕", lines=16,
                                 placeholder="转写完成后在这里显示")
        with gr.Tab("文档（原始）"):
            doc_out = gr.Textbox(label="纯文本", lines=16)
        with gr.Tab("字幕（已清理）"):
            csrt_out = gr.Textbox(label="清理后 SRT", lines=16)
        with gr.Tab("文档（已清理）"):
            cdoc_out = gr.Textbox(label="清理后正文", lines=16)
        with gr.Tab("变更报告"):
            gr.Markdown("逐条列出删了什么、改了什么，便于人工复核。")
            rep_out = gr.Textbox(label="报告", lines=16)

    with gr.Row():
        srt_file = gr.File(label="下载字幕")
        txt_file = gr.File(label="下载文稿")

    run_btn.click(
        run,
        inputs=[audio_in, model_in, clean_in, lang_in],
        outputs=[log_out, srt_out, doc_out, csrt_out, cdoc_out, rep_out,
                 srt_file, txt_file],
    )

    gr.Markdown(
        "模型存放于 `D:\\AI\\models`。首次使用某引擎需下载模型"
        "（Paraformer 约 2 GB / Qwen3-ASR 约 4.5 GB），之后完全离线。"
    )


if __name__ == "__main__":
    demo.queue().launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
        show_error=True,
    )
