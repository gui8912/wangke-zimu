# -*- coding: utf-8 -*-
"""【新增 v2.11】本机路径获取：原生多选文件对话框 + 剪贴板文件列表。

背景 / 为什么需要这一层
------------------------
浏览器出于安全，**网页永远拿不到拖入文件的本地绝对路径**：File 对象只有
name / size / type / lastModified，webkitRelativePath 也只是相对路径。
所以「拖到网页就直接零拷贝」在纯 Web 方案里做不到（Chrome / Edge / Firefox
一致）。项目里 /api/upload 只能把 HTTP 流落成 media/ 副本，几百 MB 的课
白拷一份。

绕开办法：让本机服务进程自己拿路径，再交给 /api/link 走 NTFS 硬链接。
提供两条通道，都支持**一次多个文件**、都不用手工填地址：

  1. --pick      弹出 Windows 原生多选文件对话框；用户既能多选，也能直接把
                 文件从资源管理器**拖进这个对话框**（通用对话框自带拖放）。
  2. --clipboard 读剪贴板：资源管理器里选中 N 个文件按 Ctrl+C，剪贴板里是
                 CF_HDROP（DROPFILES 结构），能拿到全部完整路径。也兼容
                 「复制为路径」得到的纯文本 / file:/// URL。

实现说明：只用标准库 ctypes 调 Win32 API，不引入 tkinter（本环境没装）
或 pywin32。server.py 用独立子进程调用本脚本，弹窗卡住也不会拖住服务。

测试：ASR_NATIVE_FAKE='["D:/a.mp4"]' 可跳过真实对话框 / 剪贴板，
仅供 samples/test_v211.py 用，生产环境不设置该变量。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
from ctypes import wintypes as w
from pathlib import Path
from urllib.parse import unquote, urlparse

IS_WIN = os.name == "nt"

# 与 server.ALLOWED_MEDIA_EXT 保持一致；这里只用于对话框的文件类型过滤
MEDIA_EXT = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
             ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".wma")

# GetOpenFileNameW flags
OFN_HIDEREADONLY = 0x00000004
OFN_PATHMUSTEXIST = 0x00000800
OFN_FILEMUSTEXIST = 0x00001000
OFN_EXPLORER = 0x00080000
OFN_ALLOWMULTISELECT = 0x00000200


# ---------------------------------------------------------------- 通用清洗
def clean_paths(paths, must_exist: bool = True) -> list:
    """去引号 / 去 file:// / 去重 / 过滤掉不存在的路径（可关）。"""
    out, seen = [], set()
    for item in paths or []:
        p = str(item or "").strip().strip('"').strip("'")
        if p.lower().startswith("file://"):
            u = urlparse(p)
            p = unquote(u.path)
            if re.match(r"^/[A-Za-z]:", p):
                p = p[1:]
            p = p.replace("/", "\\")
        # 同一路径可能一处写 D:\x、一处写 D:/x，按统一斜杠的键去重
        key = p.replace("/", "\\").lower()
        if not p or key in seen:
            continue
        seen.add(key)
        if not must_exist:
            out.append(p)
            continue
        try:
            q = Path(p)
            if q.is_file():
                out.append(str(q))
        except (OSError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- 多选对话框
def _OPENFILENAMEW():
    """结构体延迟构造：非 Windows 平台不 import wintypes 的具体字段也能用。"""
    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", w.DWORD),
            ("hwndOwner", w.HWND),
            ("hInstance", w.HINSTANCE),
            ("lpstrFilter", w.LPCWSTR),
            ("lpstrCustomFilter", w.LPWSTR),
            ("nMaxCustFilter", w.DWORD),
            ("nFilterIndex", w.DWORD),
            ("lpstrFile", w.LPWSTR),
            ("nMaxFile", w.DWORD),
            ("lpstrFileTitle", w.LPWSTR),
            ("nMaxFileTitle", w.DWORD),
            ("lpstrInitialDir", w.LPCWSTR),
            ("lpstrTitle", w.LPCWSTR),
            ("Flags", w.DWORD),
            ("nFileOffset", w.WORD),
            ("nFileExtension", w.WORD),
            ("lpstrDefExt", w.LPCWSTR),
            ("lCustData", w.LPARAM),
            ("lpfnHook", w.LPVOID),
            ("lpTemplateName", w.LPCWSTR),
            ("pvReserved", w.LPVOID),
            ("dwReserved", w.DWORD),
            ("FlagsEx", w.DWORD),
        ]
    return OPENFILENAMEW


def parse_multiselect(raw: str) -> list:
    """GetOpenFileNameW 多选返回：目录\\0文件1\\0文件2\\0\\0；单选：完整路径\\0。

    抽成纯函数方便单测，不必真的弹窗。
    """
    parts = []
    for p in (raw or "").split("\x00"):
        if not p:
            break
        parts.append(p)
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0]]
    folder = parts[0]
    return [os.path.join(folder, name) for name in parts[1:]]


def pick_files() -> list:
    """弹原生多选对话框；返回完整路径列表，取消返回 []。"""
    if not IS_WIN:
        return []
    comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    cls = _OPENFILENAMEW()
    comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(cls)]
    comdlg32.GetOpenFileNameW.restype = w.BOOL

    # 尽力把对话框带到前台：后台服务进程默认不一定允许抢焦点。
    # 把本线程输入队列临时挂到前台线程上，弹完再拆开；失败也不影响功能。
    attached = False
    tid_fg = 0
    tid_me = 0
    try:
        user32.GetForegroundWindow.restype = w.HWND
        user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        user32.GetWindowThreadProcessId.restype = w.DWORD
        kernel32.GetCurrentThreadId.restype = w.DWORD
        user32.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
        user32.AttachThreadInput.restype = w.BOOL
        tid_fg = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        tid_me = kernel32.GetCurrentThreadId()
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
    except Exception:
        attached = False

    try:
        buf = ctypes.create_unicode_buffer(65536)
        ofn = cls()
        ofn.lStructSize = ctypes.sizeof(cls)
        ofn.hwndOwner = None
        ofn.lpstrFilter = ("音视频课件\0" + ";".join("*" + e for e in MEDIA_EXT)
                           + "\0所有文件\0*.*\0\0")
        ofn.lpstrFile = ctypes.cast(buf, w.LPWSTR)
        ofn.nMaxFile = len(buf)
        ofn.lpstrTitle = "选择本机课件（可多选，也可把文件直接拖进本对话框）"
        ofn.Flags = (OFN_EXPLORER | OFN_ALLOWMULTISELECT | OFN_FILEMUSTEXIST
                     | OFN_PATHMUSTEXIST | OFN_HIDEREADONLY)
        ok = comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
        if not ok:
            return []
        return clean_paths(parse_multiselect(buf[:]))
    finally:
        if attached:
            try:
                user32.AttachThreadInput(tid_me, tid_fg, False)
            except Exception:
                pass


# ---------------------------------------------------------------- 剪贴板
class _POINT(ctypes.Structure):
    _fields_ = [("x", w.LONG), ("y", w.LONG)]


class _DROPFILES(ctypes.Structure):
    _fields_ = [("pFiles", w.DWORD), ("pt", _POINT),
                ("fNC", w.BOOL), ("fWide", w.BOOL)]


def parse_dropfiles(raw: bytes) -> list:
    """解析 CF_HDROP 的 DROPFILES 结构；抽成纯函数便于单测。"""
    size = ctypes.sizeof(_DROPFILES)
    if not raw or len(raw) < size:
        return []
    head = _DROPFILES.from_buffer_copy(raw[:size])
    body = raw[head.pFiles:] if size <= head.pFiles <= len(raw) else raw[size:]
    enc = "utf-16-le" if head.fWide else "mbcs"
    try:
        text = body.decode(enc, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    out = []
    for p in text.split("\x00"):
        if not p:
            break
        out.append(p)
    return out


def clipboard_paths() -> list:
    """读剪贴板：优先 CF_HDROP（复制的文件），退回 CF_UNICODETEXT（复制的路径文本）。"""
    if not IS_WIN:
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CF_HDROP, CF_UNICODETEXT = 15, 13

    user32.OpenClipboard.argtypes = [w.HWND]
    user32.OpenClipboard.restype = w.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = w.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [w.UINT]
    user32.IsClipboardFormatAvailable.restype = w.BOOL
    user32.GetClipboardData.argtypes = [w.UINT]
    user32.GetClipboardData.restype = w.HGLOBAL
    kernel32.GlobalLock.argtypes = [w.HGLOBAL]
    kernel32.GlobalLock.restype = w.LPVOID
    kernel32.GlobalUnlock.argtypes = [w.HGLOBAL]
    kernel32.GlobalUnlock.restype = w.BOOL
    kernel32.GlobalSize.argtypes = [w.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.Sleep.argtypes = [w.DWORD]

    # 剪贴板常被别的程序短暂占用，重试几次
    opened = False
    for _ in range(5):
        if user32.OpenClipboard(None):
            opened = True
            break
        kernel32.Sleep(60)
    if not opened:
        return []

    try:
        found = []
        if user32.IsClipboardFormatAvailable(CF_HDROP):
            h = user32.GetClipboardData(CF_HDROP)
            if h:
                n = kernel32.GlobalSize(h)
                ptr = kernel32.GlobalLock(h)
                if ptr and n:
                    try:
                        found = parse_dropfiles(ctypes.string_at(ptr, n))
                    finally:
                        kernel32.GlobalUnlock(h)
        if not found and user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if h:
                ptr = kernel32.GlobalLock(h)
                if ptr:
                    try:
                        text = ctypes.wstring_at(ptr)
                    finally:
                        kernel32.GlobalUnlock(h)
                    for line in re.split(r"[\r\n]+", text or ""):
                        line = line.strip().strip('"').strip("'")
                        if line:
                            found.append(line)
        return clean_paths(found)
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- CLI
def parse_stdout(out: bytes) -> list:
    """解析子进程 stdout：兼容 PS1 单行 JSON 数组 / 单元素被压成标量 / 多余输出。

    抽到共用模块，服务端与测试都能直接验证，不必真弹窗。
    """
    txt = out.decode("utf-8-sig", errors="replace").strip() if isinstance(out, bytes) else str(out).strip()
    if not txt:
        return []
    for line in reversed(txt.splitlines()):
        line = line.strip()
        if not line or line[0] not in '[{"':
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, list):
            return [str(x) for x in d]
        if isinstance(d, dict):
            return [str(x) for x in (d.get("paths") or [])]
        if isinstance(d, str):
            return [d]
    return []


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--clipboard"
    fake = os.environ.get("ASR_NATIVE_FAKE")
    canceled = False
    if fake:
        paths = json.loads(fake)
    elif mode == "--pick":
        paths = pick_files()
        canceled = not paths
    else:
        paths = clipboard_paths()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps({"paths": clean_paths(paths, must_exist=not bool(fake)),
                      "canceled": canceled}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
