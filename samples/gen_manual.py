# -*- coding: utf-8 -*-
"""把 使用说明书.md 渲染成自包含 HTML，供首页「使用说明」弹窗直接打开。

用法：python samples/gen_manual.py
改完 使用说明书.md 后重跑一次即可。不引入第三方 markdown 依赖，
只处理本说明书实际用到的语法：标题、表格、代码块、列表、引用、粗体、行内代码。

产物是纯静态文件 web/说明书.html，FastAPI 的 /static 挂载直接就能读，
生成/更新都不需要重启服务（当前有识别任务在跑时也能安全更新）。
"""
import html
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "使用说明书.md"
DST = BASE / "web" / "说明书.html"

CSS = """
:root{--fg:#1f2328;--muted:#5b6572;--line:#dfe3e8;--bg:#fff;--soft:#f6f8fa;
      --accent:#1a5fb4;--accent-soft:#eaf2fc}
*{box-sizing:border-box}
body{margin:0;padding:28px 34px 60px;background:var(--bg);color:var(--fg);
     font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;padding-bottom:14px;border-bottom:2px solid var(--accent)}
h2{font-size:20px;margin:34px 0 12px;padding-left:11px;border-left:4px solid var(--accent)}
h3{font-size:16px;margin:22px 0 8px;color:var(--accent)}
p{margin:8px 0}
blockquote{margin:12px 0;padding:8px 14px;background:var(--accent-soft);
           border-left:3px solid var(--accent);color:#25436b;border-radius:0 6px 6px 0}
blockquote p{margin:2px 0}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}
th,td{border:1px solid var(--line);padding:8px 11px;text-align:left;vertical-align:top}
th{background:var(--soft);font-weight:600;white-space:nowrap}
tr:nth-child(even) td{background:#fcfdfe}
code{background:var(--soft);border:1px solid var(--line);border-radius:4px;
     padding:1px 5px;font:13px/1.5 Consolas,"Cascadia Mono",monospace}
pre{background:#f6f8fa;border:1px solid var(--line);border-radius:8px;
    padding:13px 16px;overflow:auto;margin:12px 0}
pre code{background:none;border:0;padding:0;font-size:13px;line-height:1.6}
ul,ol{margin:8px 0;padding-left:26px}
li{margin:3px 0}
hr{border:0;border-top:1px solid var(--line);margin:26px 0}
a{color:var(--accent)}
.toc{background:var(--soft);border:1px solid var(--line);border-radius:8px;
     padding:12px 20px;margin:18px 0 26px}
.toc b{display:block;margin-bottom:6px;color:var(--muted);font-size:13px}
.toc a{display:inline-block;margin:2px 16px 2px 0;font-size:14px;text-decoration:none}
.toc a:hover{text-decoration:underline}
.foot{margin-top:40px;padding-top:14px;border-top:1px solid var(--line);
      color:var(--muted);font-size:12px}
@media print{.toc{display:none}body{padding:0}}
"""


def inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    return s


def render(md: str):
    lines = md.replace("\r\n", "\n").split("\n")
    out, toc = [], []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i].rstrip()
        if not ln.strip():
            i += 1
            continue
        # 代码块
        if ln.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            continue
        # 表格：连续的 | 行，第二行是分隔行
        if ln.lstrip().startswith("|") and i + 1 < n and \
                re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>" +
                       "".join(f"<th>{inline(c)}</th>" for c in head) +
                       "</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue
        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl = min(len(m.group(1)), 4)
            title = inline(m.group(2))
            if lvl == 2:
                anchor = "s" + str(len(toc) + 1)
                toc.append((anchor, m.group(2)))
                out.append(f'<h2 id="{anchor}">{title}</h2>')
            else:
                out.append(f"<h{lvl}>{title}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^\s*(---+|\*\*\*+)\s*$", ln):
            out.append("<hr>")
            i += 1
            continue
        # 引用
        if ln.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(inline(lines[i].lstrip()[1:].strip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(buf) + "</blockquote>")
            continue
        # 列表
        if re.match(r"^\s*([-*]|\d+\.)\s+", ln):
            ordered = bool(re.match(r"^\s*\d+\.\s+", ln))
            tag = "ol" if ordered else "ul"
            buf = []
            while i < n and re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                buf.append(inline(re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i])))
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{x}</li>" for x in buf) + f"</{tag}>")
            continue
        # 普通段落（说明书一行一段，逐行输出更易读）
        out.append("<p>" + inline(ln.strip()) + "</p>")
        i += 1

    toc_html = '<nav class="toc"><b>目录</b>' + "".join(
        f'<a href="#{a}">{html.escape(t)}</a>' for a, t in toc) + "</nav>"
    # 目录插到第一个 h2 之前
    body = "\n".join(out)
    first = body.find("<h2")
    if first > 0:
        body = body[:first] + toc_html + "\n" + body[first:]
    return body


def main():
    md = SRC.read_text(encoding="utf-8")
    body = render(md)
    doc = ("<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
           "<meta charset=\"utf-8\">\n"
           "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
           "<title>网课转字幕 · 使用说明书</title>\n"
           f"<style>{CSS}</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
           + body +
           "\n<p class=\"foot\">本页由 samples/gen_manual.py 从 使用说明书.md 生成；"
           "修改说明书后重跑脚本即可。</p>\n</div>\n</body>\n</html>\n")
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(doc, encoding="utf-8", newline="\n")
    print(f"生成 {DST.relative_to(BASE)}  {len(doc)} 字符  目录 {body.count('<h2')} 节")


if __name__ == "__main__":
    main()
