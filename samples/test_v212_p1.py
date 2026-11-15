# -*- coding: utf-8 -*-
"""v2.12-p1 专项：批量添加课件「上传成功但一个都入不了队」的回归。

现场（2026-11-15，ego 浏览器抓真实响应）：
  /api/upload × 2 → 200，文件都落盘；
  /api/tasks/enqueue → 200 但 created=[]，
  skipped[].reason = 「未知模型：local-qwen3（可选：paraformer、sensevoice、qwen3-asr）」；
  前端却把 skipped 一律显示成「跳过 2 个（重复 / 已完成）」，真因被藏起来。

根因：#model 下拉存的是识别源 ID（local-qwen3…），单文件转写用
PROVIDER_MODEL 映射成模型 key，批量通道的 taskOptions() 却把 ID 原样
当 model 传，且 provider 传空串。所有批量入口（批量添加课件…、拖多文件、
本机弹窗 / 剪贴板零拷贝导入）共用它，因此全军覆没。

本脚本纯离线，不碰服务、不建任务，可在识别任务运行中安全执行。
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import transcribe as T  # noqa: E402

PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def main():
    web = (BASE / "web/index.html").read_text(encoding="utf-8")
    srv = (BASE / "server.py").read_text(encoding="utf-8")

    print("[后端模型契约]")
    ok("T.MODELS 不含识别源 ID local-qwen3", "local-qwen3" not in T.MODELS,
       list(T.MODELS))
    ok("T.MODELS 含 qwen3-asr / paraformer",
       "qwen3-asr" in T.MODELS and "paraformer" in T.MODELS, list(T.MODELS))

    print("[前端映射表]")
    m = re.search(r"const PROVIDER_MODEL = \{(.*?)\};", web, re.S)
    ok("PROVIDER_MODEL 映射表存在", bool(m))
    pairs = dict(re.findall(r"'([^']+)'\s*:\s*'([^']+)'", m.group(1))) if m else {}
    ok("四个识别源都有映射", set(pairs) == {
        "local-qwen3", "local-paraformer", "tencent-cloud", "volcengine-doubao"}, pairs)
    ok("映射值全部是后端认识的模型 key",
       bool(pairs) and all(v in T.MODELS for v in pairs.values()), pairs)
    ok("local-qwen3 → qwen3-asr", pairs.get("local-qwen3") == "qwen3-asr")
    ok("local-paraformer → paraformer", pairs.get("local-paraformer") == "paraformer")

    print("[taskOptions 修复]")
    body = re.search(r"function taskOptions\(\)\{(.*?)\n\}", web, re.S)
    t = body.group(1) if body else ""
    ok("taskOptions 用 PROVIDER_MODEL 映射", "PROVIDER_MODEL[pid]" in t, t[:200])
    ok("taskOptions 不再把下拉值直接当 model",
       "model: $('#model') ? $('#model').value" not in t)
    ok("taskOptions 显式传 provider id", re.search(r"provider:\s*pid", t) is not None)
    ok("taskOptions 透传云端整理稿开关", "use_script_track" in t)
    ok("单文件转写仍用同一张映射表",
       "PROVIDER_MODEL[$('#model').value] || 'qwen3-asr'" in web)

    print("[批量按钮只选 1 个文件也入队]")
    ok("batchPick 标记已声明", "let batchPick = false;" in web)
    ok("批量按钮点击会置标记",
       re.search(r"btnBatchAdd[\s\S]{0,160}batchPick\s*=\s*true", web) is not None)
    ok("onchange 按来源分流", "if(fromBatch || files.length > 1) batchAdd(files);" in web)
    ok("虚线框点击入口已随元素移除", "drop.onclick =" not in web)
    ok("重新选择会清标记", "$('#fcReselect').onclick = ()=>{ batchPick = false;" in web)
    ok("文件对话框取消会清标记",
       "fileInput.addEventListener('cancel', ()=>{ batchPick = false; });" in web)

    print("[失败原因不再被伪装成重复]")
    ok("不再硬编码「重复 / 已完成」", "跳过 ${skip} 个（重复 / 已完成）" not in web)
    ok("汇总 skipped 的 reason", "map(x => x.reason)" in web)
    ok("区分硬错误", "const hard = reasons.filter" in web)
    ok("硬错误用 err 样式", "hard.length ? 'err' : 'done'" in web)
    ok("控制台输出被拒原因", "console.error('批量入队被拒：'" in web)

    print("[服务端默认值未回归]")
    ok("DEFAULT_TASK_OPTS 仍是 qwen3-asr",
       re.search(r'DEFAULT_TASK_OPTS\s*=\s*\{\s*"model":\s*"qwen3-asr"', srv) is not None)
    ok("入队仍逐条返回 reason", '"reason": err' in srv)

    print("[v2.12-p3 面板精简 / p4 删除虚线框]")
    ok("虚线框 DOM 已删除", '<div id="drop">' not in web)
    ok("拖放框 CSS 已删除", "#drop{" not in web and "#drop:hover" not in web)
    ok("页面不再出现「把音视频拖到这里」", "把音视频拖到这里" not in web)
    ok("JS 不再引用 drop 元素（否则整段脚本会崩）",
       "const drop = $('#drop')" not in web and "drop.ondrop" not in web
       and "function pathsFromDrop" not in web)
    ok("fileInput 保留（按钮还要用）",
       'id="fileInput"' in web and "const fileInput = $('#fileInput')" in web)
    ok("零拷贝两个入口仍在", "btnNativePick" in web and "btnClipImport" in web)
    ok("importPaths 仍在（弹窗 / 剪贴板共用）", "async function importPaths(" in web)
    rows = re.findall(r'<div class="qrow">([\s\S]*?)</div>', web)
    first = rows[0] if rows else ""
    ok("批量添加与暂停队列合并进同一行",
       "btnBatchAdd" in first and "btnQueuePause" in first, first[:120])
    ok("定时 / 重试控件也在同一行",
       "queueAt" in first and "queueRetry" in first)
    ok("「一次可多选…」提示文字已删除", "一次可多选" not in web)
    ok("文件卡仍照常显示", "$('#fileCard').hidden = false" in web)
    ok("自动载回逻辑仍保留", "async function restoreLastTask()" in web)
    manual = (BASE / "使用说明书.md").read_text(encoding="utf-8")
    quick = (BASE / "网课转字幕.txt").read_text(encoding="utf-8-sig")
    ok("说明书不再写含糊的「上方虚线框」", "上方虚线框" not in manual)
    ok("说明书改按按钮指路", "点「批量导入本机课件…（拖放 / 多选）」选一节课" in manual)
    ok("速查不再提左上角拖放框",
       "页面左上角写着「把音视频拖到这里」" not in quick)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
