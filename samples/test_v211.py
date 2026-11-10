# -*- coding: utf-8 -*-
"""v2.11 专项：本机路径批量零拷贝导入（拖放弹窗 / 文件对话框 / 剪贴板）。

背景：浏览器网页拿不到拖入文件的本地绝对路径（File 对象只有 name/size/type），
所以「拖到网页就零拷贝」做不到。改由本机独立进程弹窗或读剪贴板拿完整路径，
再回写网页走 /api/link/batch 批量 NTFS 硬链接入队。

设 ASR_TEST_NATIVE_UI=1 会额外做一次真实弹窗冒烟（自动 WM_CLOSE 关闭）。
"""
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "samples"))

import apibase          # noqa: E402
import native_io        # noqa: E402

PASS = FAIL = 0
TMP = Path("D:/temp/asr_native_test")


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def req(path, payload=None, method=None):
    url = apibase.base() + path
    try:
        if payload is None and not method:
            with urllib.request.urlopen(url, timeout=15) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        data = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
        q = urllib.request.Request(url, data=data, method=method or "POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(q, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {}


def pure_functions():
    print("[native_io 纯函数]")
    bs, nul = chr(92), chr(0)
    names = ["D:" + bs + "课程" + bs + "甲.mp4", "D:" + bs + "课程" + bs + "乙.mp3"]
    body = (nul.join(names) + nul + nul).encode("utf-16-le")
    raw = struct.pack("<IiiII", 20, 0, 0, 0, 1) + body
    ok("CF_HDROP 多文件解析", native_io.parse_dropfiles(raw) == names,
       native_io.parse_dropfiles(raw))
    ok("DROPFILES 太短不崩", native_io.parse_dropfiles(b"x") == [])
    ok("对话框单选", native_io.parse_multiselect("D:" + bs + "a.mp4" + nul + nul + "x")
       == ["D:" + bs + "a.mp4"])
    ok("对话框多选拼回目录", native_io.parse_multiselect(
        "D:" + bs + "课" + nul + "a.mp4" + nul + "b.mp4" + nul + nul)
       == ["D:" + bs + "课" + bs + "a.mp4", "D:" + bs + "课" + bs + "b.mp4"])
    got = native_io.clean_paths(["file:///D:/x%20y.mp4", '"D:/x y.mp4"'],
                                must_exist=False)
    ok("file:// 转换 + 斜杠归一化去重", len(got) == 1, got)
    ok("剪贴板原文引号可去", native_io.clean_paths(['"D:/a.mp4"'], must_exist=False)
       == ["D:/a.mp4"])


def stdout_parsing():
    print("[子进程输出解析]")
    ok("JSON 数组", native_io.parse_stdout(b'["a.mp4","b.mp4"]') == ["a.mp4", "b.mp4"])
    ok("单元素被压成标量", native_io.parse_stdout(b'"a.mp4"') == ["a.mp4"])
    ok("前面夹了 PowerShell 噪声", native_io.parse_stdout(
        b'Loading\r\n["a.mp4"]\r\n') == ["a.mp4"])
    ok("UTF-8 BOM", native_io.parse_stdout(b'\xef\xbb\xbf["a.mp4"]') == ["a.mp4"])
    ok("空输出", native_io.parse_stdout(b"") == [])
    ok("取消输出空数组", native_io.parse_stdout(b"[]") == [])


def cli_fake():
    print("[CLI 测试后门]")
    env = os.environ.copy()
    env["ASR_NATIVE_FAKE"] = json.dumps(["D:/a.mp4", "D:/b.mp3"], ensure_ascii=False)
    r = subprocess.run([sys.executable, str(BASE / "native_io.py"), "--clipboard"],
                       capture_output=True, env=env, timeout=30)
    try:
        d = json.loads(r.stdout.decode("utf-8").strip())
    except Exception:
        d = {}
    ok("fake 模式返回预设路径", d.get("paths") == ["D:/a.mp4", "D:/b.mp3"], d)
    ok("fake 模式无 canceled", d.get("canceled") is False, d)


def server_api():
    print("[服务端接口]")
    st, h = req("/api/health")
    if st != 200:
        print("  SKIP 服务未启动，接口部分跳过")
        return
    ok("服务版本 2.11", h.get("version") == "2.11", h.get("version"))

    TMP.mkdir(parents=True, exist_ok=True)
    good = []
    for n in ("甲课.mp4", "乙课.mp3"):
        p = TMP / n
        p.write_bytes(b"fake-media-for-link-test")
        good.append(str(p).replace("\\", "/"))
    created = []
    try:
        st, d = req("/api/link/batch", {"paths": good + [str(TMP / "不存在.mp4").replace("\\", "/")]})
        ok("批量登记返回 200", st == 200, (st, d))
        ok("成功 2 个", len(d.get("items", [])) == 2, d.get("items"))
        ok("失败 1 个且逐条给原因", len(d.get("errors", [])) == 1
           and "不存在" in d["errors"][0]["error"], d.get("errors"))
        ok("同盘走硬链接零拷贝", d.get("linked") == 2 and d.get("copied") == 0, d)
        ok("file_ids 与 items 对齐", len(d.get("file_ids", [])) == 2, d.get("file_ids"))
        created = [x["path"] for x in d.get("items", [])]

        st, d = req("/api/link/batch", {"paths": "not-a-list"})
        ok("非数组返回 400", st == 400, (st, d))
        st, d = req("/api/link/batch", {"paths": ["x"] * 301})
        ok("超过 300 个返回 400", st == 400, (st, d))
        st, d = req("/api/link/batch", {"paths": []})
        ok("空数组不报错", st == 200 and d.get("items") == [], (st, d))
        st, d = req("/api/link", {"path": good[0]})
        ok("单路径旧接口仍兼容", st == 200 and d.get("file_id"), (st, d))
        if st == 200:
            created.append(d.get("path"))
        st, d = req("/api/native/clipboard")
        ok("剪贴板接口可用", st == 200 and isinstance(d.get("paths"), list), (st, d))
    finally:
        for p in created:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        for p in TMP.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            TMP.rmdir()
        except OSError:
            pass


def source_contract():
    print("[源码契约]")
    srv = (BASE / "server.py").read_text(encoding="utf-8")
    web = (BASE / "web" / "index.html").read_text(encoding="utf-8")
    ps1 = (BASE / "native_dropbox.ps1").read_text(encoding="utf-8-sig")
    ok("批量登记接口", '@app.post("/api/link/batch")' in srv)
    ok("弹窗接口", '@app.post("/api/native/pick")' in srv)
    ok("剪贴板接口", '@app.get("/api/native/clipboard")' in srv)
    ok("同一时刻只允许一个弹窗", "_NATIVE_LOCK" in srv)
    ok("PowerShell 失败可回退", '"--pick"' in srv and "native_io.py" in srv)
    ok("前端批量导入按钮", 'id="btnNativePick"' in web)
    ok("前端剪贴板按钮", 'id="btnClipImport"' in web)
    ok("前端批量登记", "function importPaths" in web and "/api/link/batch" in web)
    ok("前端拖入路径探测", "function pathsFromDrop" in web)
    ok("两条通道共用入队", "function enqueueIds" in web)
    ok("手动填路径仍保留", 'id="linkPath"' in web and 'id="btnLink"' in web)
    ok("弹窗支持拖放", "Add_DragDrop" in ps1 and "FileDrop" in ps1)
    ok("弹窗支持浏览多选", "OpenFileDialog" in ps1 and "Multiselect" in ps1)
    ok("弹窗支持拖文件夹递归", "AllDirectories" in ps1)
    ok("弹窗输出 JSON 数组", "ConvertTo-Json" in ps1)


def ui_smoke():
    print("[本机弹窗冒烟（可选）]")
    if os.environ.get("ASR_TEST_NATIVE_UI") != "1":
        print("  SKIP 设 ASR_TEST_NATIVE_UI=1 才会弹窗（自动关闭）")
        return
    if os.name != "nt":
        print("  SKIP 非 Windows")
        return
    import ctypes
    from ctypes import wintypes as w
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
    user32.FindWindowW.restype = w.HWND
    user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
         "-File", str(BASE / "native_dropbox.ps1")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    hwnd = 0
    for _ in range(60):
        hwnd = user32.FindWindowW(None, "批量导入本机课件")
        if hwnd:
            break
        time.sleep(0.2)
    ok("拖放窗口能创建", bool(hwnd))
    if hwnd:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)      # WM_CLOSE
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    ok("取消后输出空数组", native_io.parse_stdout(out) == [], out[:200])
    ok("进程正常退出", proc.returncode == 0, (proc.returncode, err[:200]))

    # 再走一次服务端接口：验证 _native_paths → PowerShell → parse_stdout → HTTP
    import threading
    result = {}

    def call():
        try:
            st, d = req("/api/native/pick", {}, method="POST")
            result.update(st=st, d=d)
        except Exception as exc:
            result["err"] = str(exc)

    t = threading.Thread(target=call, daemon=True)
    t.start()
    hwnd = 0
    for _ in range(60):
        hwnd = user32.FindWindowW(None, "批量导入本机课件")
        if hwnd:
            break
        time.sleep(0.2)
    ok("服务端能拉起弹窗", bool(hwnd))
    if hwnd:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)
    t.join(timeout=45)
    ok("服务端接口返回空路径列表",
       result.get("st") == 200 and result.get("d", {}).get("paths") == [], result)

    # 模拟「拖了 2 个文件进弹窗」：生成一份跳过 ShowDialog 的副本，
    # 直接调 Add-Path，验证过滤 / 去重 / 中文路径 JSON 输出。
    TMP.mkdir(parents=True, exist_ok=True)
    a, b = TMP / "拖入甲.mp4", TMP / "拖入乙.txt"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    src = (BASE / "native_dropbox.ps1").read_text(encoding="utf-8-sig")
    inject = ("Add-Path '" + str(a).replace("\\", "/") + "'\n"
              "Add-Path '" + str(a).replace("\\", "/") + "'\n"      # 重复拖入应去重
              "Add-Path '" + str(b).replace("\\", "/") + "'\n"      # 非音视频应忽略
              "$script:result = @()\n"
              "foreach ($x in $list.Items) { $script:result += $x }")
    probe = BASE / "samples" / "_dropbox_probe.ps1"
    probe.write_text(src.replace("[void]$form.ShowDialog()", inject),
                     encoding="utf-8-sig")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
             "-File", str(probe)],
            capture_output=True, timeout=60)
        paths = native_io.parse_stdout(r.stdout)
        ok("拖入文件被识别并去重", len(paths) == 1, (paths, r.stdout[:200], r.stderr[:200]))
        ok("中文路径完整回写", bool(paths) and "拖入甲.mp4" in paths[0], paths)
        ok("非音视频被过滤", all(".txt" not in p for p in paths), paths)
    finally:
        probe.unlink(missing_ok=True)
        for p in (a, b):
            p.unlink(missing_ok=True)
        try:
            TMP.rmdir()
        except OSError:
            pass


def main():
    print("=" * 58)
    pure_functions()
    stdout_parsing()
    cli_fake()
    server_api()
    source_contract()
    ui_smoke()
    print("=" * 58)
    print(f"{PASS}/{PASS + FAIL} 通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
