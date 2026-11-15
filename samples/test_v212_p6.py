# -*- coding: utf-8 -*-
"""v2.12-p6 专项：字幕编辑页「点重新清理后字幕消失」回归。

现场（2026-11-15，ego 浏览器抓真实响应）：
  任务栏「打开」载入历史任务 → state.fileId = null
  → fd.append('file_id', '') → /api/reclean 返回 422
    {"detail":[{"type":"missing","loc":["body","file_id"],"msg":"Field required"}]}
  → 前端只判断 d.error（422 没有这个字段）
  → state.cues = undefined → drawCues() 抛 TypeError
  → #cueList 被清空、状态永远卡在「重新清理中…」，看起来就是字幕没了。

实测三种请求：不带 file_id → 422；file_id='' → 422；file_id='x' → 200。
后端 reclean() 根本没用到 file_id，是多余的必填约束。

两层修复：
  ① server.py 把 file_id 改成 Form("") 可选（下次重启生效）；
  ② web/index.html 传非空兜底值，让当前未重启的服务也能立刻通过，
     并且任何异常响应 / 网络错误都保留原字幕、显示真实原因；
     drawCues() 再加一层数组守卫，杜绝「全空且无提示」。

本脚本纯离线、不碰服务与队列，可在识别任务运行中安全执行。
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

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

    print("[后端：file_id 不再是无用的必填项]")
    # /api/transcribe 的 file_id 是必需的（要靠它找媒体文件），
    # 所以只在 reclean 这一段里断言，别误伤别的端点。
    rc = srv.split('@app.post("/api/reclean")', 1)[-1].split("@app.", 1)[0]
    ok("reclean 的 file_id 改为可选 Form(\"\")",
       'file_id: str = Form("")' in rc)
    ok("reclean 里不再有 file_id 的 Form(...) 必填",
       "file_id: str = Form(...)" not in rc)
    ok("其它端点（transcribe）的 file_id 仍必填，未被误改",
       'async def transcribe(file_id: str = Form(...),' in srv)
    ok("cues_json 仍是必填（没有它就无从清理）",
       "cues_json: str = Form(...)" in rc)
    ok("仍返回 cues 数组", 'return {"cues": out' in rc)
    ok("修复原因写进文档字符串", "v2.12-p6" in rc and "422" in rc)

    print("[前端：请求带上非空 file_id]")
    ok("file_id 有 taskId / stem 兜底",
       "state.fileId || state.taskId || state.stem || 'reclean'" in web)
    ok("不再直接传空串 file_id",
       "fd.append('file_id', state.fileId || '')" not in web)
    ok("仍从原始字幕重跑", "JSON.stringify(state.raw)" in web)

    print("[前端：异常响应绝不清空字幕]")
    ok("检查 HTTP 状态 / error / cues 是否为数组",
       "if(!r.ok || d.error || !Array.isArray(d.cues))" in web)
    ok("JSON 解析失败也兜住", "r.json().catch(()=>({}))" in web)
    ok("422 的 detail 数组能取出 msg",
       "Array.isArray(d.detail)" in web and "x.msg" in web)
    ok("失败时明确告知字幕保持原样", "字幕保持原样" in web)
    ok("失败时状态标红", "setStatus('重新清理失败：' + why, 'err')" in web)
    ok("失败分支直接 return，不往下改 state.cues",
       re.search(r"if\(!r\.ok \|\| d\.error \|\| !Array\.isArray\(d\.cues\)\)\{"
                 r"[\s\S]{0,600}?return;\s*\}", web) is not None)
    check_at = web.find("if(!r.ok || d.error || !Array.isArray(d.cues))")
    assign_at = web.find("state.cues = d.cues; drawCues()")
    ok("state.cues 赋值在校验之后", 0 < check_at < assign_at,
       f"check={check_at} assign={assign_at}")
    ok("旧的「只判断 d.error」已移除",
       "if(d.error){ setStatus(d.error,'err'); return; }" not in web)

    print("[前端：成功路径与兜底]")
    ok("diffs 用数组守卫", "Array.isArray(d.diffs) ? d.diffs : []" in web)
    ok("suggestions 用空数组兜底", "renderTips(d.suggestions || [])" in web)
    ok("changed 不会显示 undefined", "d.changed || 0" in web)
    ok("整个请求包在 try/catch 里",
       "}catch(e){" in web and "重新清理失败：' + why" in web)
    ok("成功才写历史快照", web.find("snapshot('重新清理')") > assign_at)

    print("[drawCues 纵深防御]")
    ok("非数组时不再直接读 length",
       re.search(r"function drawCues\(\)\{[\s\S]{0,700}?if\(!Array\.isArray\(state\.cues\)\)",
                 web) is not None)
    ok("异常时显示可恢复提示", "字幕数据异常，刷新页面或从任务里重新打开即可恢复" in web)
    ok("异常时留控制台告警", "drawCues：字幕数据不是数组" in web)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
