#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude-export-reader：把 Claude.ai 导出的 conversations.json
还原为人类阅读友好的 TXT 和 HTML5 文件。

用法:
    python3 claude_export_reader.py <导出目录或 conversations.json 路径>
    python3 claude_export_reader.py <输入> -o <输出目录> --tz Asia/Shanghai

输出（默认写到输入同目录）:
    Claude对话记录.txt   纯文本阅读版
    Claude对话记录.html  单文件网页版（离线可开，含目录/筛选/折叠）

无第三方依赖，Python 3.9+（标准库 zoneinfo）。
"""
import argparse
import json
import os
import re
import sys
import html as htmlmod
from datetime import datetime
from zoneinfo import ZoneInfo

# 时区与显示标签在 main() 里按 --tz 参数赋值
TZ = ZoneInfo("Asia/Shanghai")
TZ_LABEL = "北京时间"

# TXT 中各类次要内容的截断上限（字符数）；HTML 中放宽
TXT_ATTACH_CAP = 800
TXT_TOOL_RESULT_CAP = 1200
TXT_FILE_CONTENT_CAP = 4000
HTML_TOOL_RESULT_CAP = 50000

TOOL_NAMES_ZH = {
    "web_search": "联网搜索",
    "web_fetch": "网页抓取",
    "view": "查看文件",
    "create_file": "创建文件",
    "present_files": "展示文件",
    "bash_tool": "执行命令",
    "artifacts": "Artifact 创建",
    "ask_user_input_v0": "请求用户输入",
    "launch_extended_search_task": "深入研究任务",
    "Claude in Chrome:update_plan": "Chrome 插件 · 更新计划",
}


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        return None


def fmt_ts(s, fmt="%Y-%m-%d %H:%M"):
    dt = parse_ts(s)
    return dt.strftime(fmt) if dt else "(时间未知)"


def tool_label(name):
    if not name:
        return "工具调用"
    zh = TOOL_NAMES_ZH.get(name)
    return f"{name}（{zh}）" if zh else name


def conv_title(conv):
    """对话标题单行化（个别标题内嵌换行，会破坏目录排版）。"""
    name = conv.get("name") or "(未命名对话)"
    return re.sub(r"\s+", " ", name).strip()


# ---------------------------------------------------------------------------
# 对话主线还原：从最新的叶子消息沿 parent 链回溯；编辑产生的旧分支单独返回
# ---------------------------------------------------------------------------

def order_messages(conv):
    msgs = conv.get("chat_messages") or []
    if not msgs:
        return [], []
    by_uuid = {m["uuid"]: m for m in msgs}
    has_child = {m["parent_message_uuid"] for m in msgs}
    leaves = [m for m in msgs if m["uuid"] not in has_child]
    if not leaves:  # 数据异常（成环），退化为按时间排序
        return sorted(msgs, key=lambda m: m["created_at"] or ""), []
    main_leaf = max(leaves, key=lambda m: m["created_at"] or "")
    path, cur, guard = [], main_leaf, 0
    while cur is not None and guard <= len(msgs):
        path.append(cur)
        cur = by_uuid.get(cur["parent_message_uuid"])
        guard += 1
    path.reverse()
    in_path = {m["uuid"] for m in path}
    orphans = sorted((m for m in msgs if m["uuid"] not in in_path),
                     key=lambda m: m["created_at"] or "")
    return path, orphans


# ---------------------------------------------------------------------------
# 消息内容归一化：把 content 块整理成 (kind, payload) 序列，tool_result 并入 tool_use
# ---------------------------------------------------------------------------

def normalize_message(msg):
    segs = []
    pending_tools = {}  # tool_use_id -> seg payload
    for blk in msg.get("content") or []:
        t = blk.get("type")
        if t == "text":
            text = (blk.get("text") or "").strip()
            if text:
                urls, seen = [], set()
                for cit in blk.get("citations") or []:
                    url = (cit.get("details") or {}).get("url")
                    if url and url not in seen:
                        seen.add(url)
                        urls.append(url)
                segs.append(("text", {"text": text, "citations": urls}))
        elif t == "thinking":
            think = (blk.get("thinking") or "").strip()
            if think:
                segs.append(("thinking", {"text": think}))
        elif t == "tool_use":
            payload = {"name": blk.get("name"), "input": blk.get("input"), "result": None}
            segs.append(("tool", payload))
            if blk.get("id"):
                pending_tools[blk["id"]] = payload
        elif t == "tool_result":
            payload = pending_tools.get(blk.get("tool_use_id"))
            if payload is not None:
                payload["result"] = blk
            else:  # 找不到对应调用，单独成段
                segs.append(("tool", {"name": blk.get("name"), "input": None, "result": blk}))
        # token_budget 等其他类型跳过
    for att in msg.get("attachments") or []:
        segs.append(("attachment", att))
    for f in msg.get("files") or []:
        segs.append(("file", f))
    return segs


def prettify_result_text(s):
    """工具结果若是 JSON 字符串，还原成人类可读文本：
    - bash_tool 风格 {returncode, stdout, stderr} → 直接展开 stdout/stderr（换行复原）
    - 其余 JSON → ensure_ascii=False 重排（修复 \\uXXXX 中文转义）"""
    t = s.strip()
    if t[:1] not in "{[":
        return s
    try:
        obj = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return s
    if isinstance(obj, dict) and "stdout" in obj:
        parts = []
        rc = obj.get("returncode")
        if rc not in (None, 0):
            parts.append(f"[returncode: {rc}]")
        stdout = (obj.get("stdout") or "").rstrip()
        stderr = (obj.get("stderr") or "").rstrip()
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append("[stderr]\n" + stderr)
        return "\n".join(parts) or "(无输出)"
    return json.dumps(obj, ensure_ascii=False, indent=2)


def tool_result_items(result_blk):
    """把 tool_result 的 content 拆成 ('link', title, url) / ('text', s) 列表。"""
    items = []
    content = result_blk.get("content")
    if isinstance(content, str):
        if content.strip():
            items.append(("text", prettify_result_text(content)))
        return items
    for it in content or []:
        if not isinstance(it, dict):
            items.append(("text", str(it)))
            continue
        if it.get("type") == "knowledge":
            title = (it.get("title") or "").strip() or it.get("url") or "(无标题)"
            items.append(("link", title, it.get("url") or ""))
        elif it.get("type") == "text":
            txt = (it.get("text") or "").strip()
            if txt:
                items.append(("text", prettify_result_text(txt)))
        else:
            items.append(("text", json.dumps(it, ensure_ascii=False)[:500]))
    return items


def compact_input(tool_input):
    """工具入参里的短字段拼成一行；长字段（如文件内容）单独返回。"""
    shorts, longs = [], []
    if isinstance(tool_input, dict):
        for k, v in tool_input.items():
            if k == "md_citations":  # 内部引用元数据，只保留来源链接
                urls, seen = [], set()
                try:
                    arr = v if isinstance(v, list) else json.loads(v)
                    for cit in arr:
                        u = ((cit or {}).get("details") or {}).get("url")
                        if u and u not in seen:
                            seen.add(u)
                            urls.append(u)
                except (TypeError, ValueError, AttributeError):
                    pass
                if urls:
                    longs.append(("引用来源", "\n".join(urls)))
                continue
            sv = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            if len(sv) > 200:
                longs.append((k, sv))
            else:
                shorts.append(f"{k}: {sv}")
    elif tool_input is not None:
        shorts.append(json.dumps(tool_input, ensure_ascii=False)[:200])
    return "  ".join(shorts), longs


TXT_TRUNC_NOTE = "……（内容过长已截断，完整内容见 HTML 版或原始 JSON）"
HTML_TRUNC_NOTE = "……（内容过长已截断，完整内容见原始 JSON）"


def truncate(s, cap, note=TXT_TRUNC_NOTE):
    return s if len(s) <= cap else s[:cap].rstrip() + "\n" + note


# ===========================================================================
# TXT 输出
# ===========================================================================

H_BAR = "═" * 78
S_BAR = "─" * 78


def indent(text, pad="    "):
    return "\n".join(pad + ln for ln in text.splitlines())


def render_txt_message(msg, user_name):
    who = f"👤 {user_name}" if msg["sender"] == "human" else "🤖 Claude"
    lines = [f"◆ {who}　{fmt_ts(msg.get('created_at'))}", ""]
    for kind, payload in normalize_message(msg):
        if kind == "text":
            lines.append(payload["text"])
            if payload["citations"]:
                lines.append("")
                lines.append("　〔引用来源〕")
                lines.extend(f"　　- {u}" for u in payload["citations"])
            lines.append("")
        elif kind == "thinking":
            lines.append("　【思考过程】")
            lines.append(indent(payload["text"], "　　"))
            lines.append("")
        elif kind == "tool":
            name = payload.get("name")
            shorts, longs = compact_input(payload.get("input"))
            head = f"　【工具 · {tool_label(name)}】"
            if shorts:
                head += " " + shorts
            lines.append(head)
            for k, sv in longs:
                cap = TXT_FILE_CONTENT_CAP if name in ("create_file", "artifacts") else TXT_TOOL_RESULT_CAP
                lines.append(f"　　▸ {k}:")
                lines.append(indent(truncate(sv, cap), "　　　"))
            result = payload.get("result")
            if result is not None:
                if result.get("is_error"):
                    lines.append("　　▸ ⚠️ 本次调用失败，以下为错误信息：")
                items = tool_result_items(result)
                links = [it for it in items if it[0] == "link"]
                texts = [it[1] for it in items if it[0] == "text"]
                if links:
                    lines.append(f"　　▸ 返回 {len(links)} 条结果：")
                    lines.extend(f"　　　- {t} — {u}" for _, t, u in links)
                if texts and name != "web_search":  # 搜索正文太长，TXT 只留链接
                    lines.append("　　▸ 结果：")
                    lines.append(indent(truncate("\n".join(texts), TXT_TOOL_RESULT_CAP), "　　　"))
            lines.append("")
        elif kind == "attachment":
            fname = payload.get("file_name") or "(未命名附件)"
            ftype = payload.get("file_type") or "?"
            size = payload.get("file_size") or 0
            lines.append(f"　【附件】{fname}（{ftype}，{size:,} 字节）")
            extracted = (payload.get("extracted_content") or "").strip()
            if extracted:
                lines.append(indent(truncate(extracted, TXT_ATTACH_CAP), "　　"))
            lines.append("")
        elif kind == "file":
            fname = payload.get("file_name") or payload.get("file_uuid") or "?"
            lines.append(f"　【上传文件】{fname}（文件本体未包含在导出数据中）")
            lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def date_span(convs):
    dates = [c.get("created_at") for c in convs if c.get("created_at")]
    if not dates:
        return "(无)"
    return f"{fmt_ts(min(dates), '%Y-%m-%d')} ～ {fmt_ts(max(dates), '%Y-%m-%d')}"


def account_line(user_name, user_email):
    return f"{user_name} <{user_email}>" if user_email else user_name


def render_txt(convs, user_name, user_email, src_name):
    n_msgs = sum(len(c.get("chat_messages") or []) for c in convs)
    out = [H_BAR,
           "Claude 对话记录（完整存档 · 阅读版）",
           H_BAR,
           f"账户　　：{account_line(user_name, user_email)}",
           f"对话数量：{len(convs)} 个，共 {n_msgs} 条消息",
           f"时间范围：{date_span(convs)}（{TZ_LABEL}，按对话创建时间）",
           f"排列顺序：按创建时间从新到旧",
           f"生成来源：{src_name}",
           "说明　　：搜索结果只保留标题与链接；附件与长输出在 TXT 中有截断，",
           "          完整内容请查看同目录的 HTML 版本或原始 JSON。",
           H_BAR, "", "目　录", ""]
    for i, c in enumerate(convs, 1):
        out.append(f"{i:>4}. {conv_title(c)}　[{fmt_ts(c.get('created_at'), '%Y-%m-%d')} · {len(c.get('chat_messages') or [])} 条]")
    out.append("")
    for i, c in enumerate(convs, 1):
        main, orphans = order_messages(c)
        out += ["", H_BAR,
                f"【{i}/{len(convs)}】{conv_title(c)}",
                f"创建：{fmt_ts(c.get('created_at'))}　最后更新：{fmt_ts(c.get('updated_at'))}　消息：{len(c.get('chat_messages') or [])} 条",
                H_BAR, ""]
        if not main and not orphans:
            out.append("（此对话没有任何消息）")
        for j, m in enumerate(main):
            if j:
                out.append(S_BAR)
            out.append(render_txt_message(m, user_name))
            out.append("")
        if orphans:
            out += [S_BAR,
                    f"✂️ 以下 {len(orphans)} 条消息属于编辑前的旧分支（已被重新编辑覆盖）：", ""]
            for m in orphans:
                out.append(render_txt_message(m, user_name))
                out.append("")
    out.append(H_BAR)
    out.append("—— 全文完 ——")
    return "\n".join(out)


# ===========================================================================
# 自带的轻量 Markdown → HTML 渲染器（无第三方依赖）
# ===========================================================================

def esc(s):
    return htmlmod.escape(s, quote=False)


_RE_CODE = re.compile(r"`([^`\n]+)`")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_ITAL = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
_RE_STRIKE = re.compile(r"~~(.+?)~~")
_RE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_RE_AUTO = re.compile(r"(?<![\"'>=\]])(https?://[^\s<>\"')\]，。；！？]+)")


_RE_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~])")


def md_inline(s):
    s = esc(s)
    holes = []

    def stash(m):
        holes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(holes) - 1}\x00"

    def stash_escaped(m):  # \* \_ 等反斜杠转义：按字面字符输出
        holes.append(m.group(1))
        return f"\x00{len(holes) - 1}\x00"

    s = _RE_CODE.sub(stash, s)
    s = _RE_ESCAPE.sub(stash_escaped, s)
    s = _RE_LINK.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', s)
    s = _RE_AUTO.sub(lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>', s)
    s = _RE_BOLD.sub(r"<strong>\1</strong>", s)
    s = _RE_ITAL.sub(r"<em>\1</em>", s)
    s = _RE_STRIKE.sub(r"<del>\1</del>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], s)


def render_list(items):
    """items: [(depth, ordered, text, value)] → 合法嵌套的 ol/ul HTML。
    有序项带 value 属性，被空行拆分的编号列表在浏览器中仍按原编号显示；
    子列表包在父 <li> 内部，符合 HTML5 规范。"""
    html, stack = [], []  # stack: 当前仍打开的列表标签
    for depth, ordered, text, value in items:
        tag = "ol" if ordered else "ul"
        depth = min(depth, len(stack))  # 不允许一次跳多级
        while len(stack) > depth + 1:
            html.append(f"</li></{stack.pop()}>")
        if len(stack) == depth + 1 and stack[-1] != tag:
            html.append(f"</li></{stack.pop()}>")
        if len(stack) == depth + 1:
            html.append("</li>")
        while len(stack) < depth + 1:
            html.append(f"<{tag}>")
            stack.append(tag)
        vattr = f' value="{value}"' if ordered and value is not None else ""
        html.append(f"<li{vattr}>{md_inline(text)}")
    while stack:
        html.append(f"</li></{stack.pop()}>")
    return "".join(html)


_RE_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_RE_HR = re.compile(r"^\s{0,3}([-*_])\s*(\1\s*){2,}$")
_RE_LIST = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)、])\s+(.*)$")
_RE_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_RE_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")


def md_to_html(text):
    lines = text.replace("\r\n", "\n").split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        if ln.lstrip().startswith("```"):
            i += 1
            code = []
            while i < n and not lines[i].lstrip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{esc(chr(10).join(code))}</code></pre>")
            continue
        m = _RE_HEAD.match(ln)
        if m:
            lvl = min(len(m.group(1)) + 2, 6)  # 降两级，避免与页面标题冲突
            out.append(f"<h{lvl}>{md_inline(m.group(2).strip(' #'))}</h{lvl}>")
            i += 1
            continue
        if _RE_HR.match(ln):
            out.append("<hr>")
            i += 1
            continue
        if _RE_QUOTE.match(ln):
            block = []
            while i < n and _RE_QUOTE.match(lines[i]):
                block.append(_RE_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append(f"<blockquote>{md_to_html(chr(10).join(block))}</blockquote>")
            continue
        if "|" in ln and i + 1 < n and _RE_TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            rows = [r + [""] * (len(header) - len(r)) for r in rows]  # 短行补齐空单元格
            thead = "".join(f"<th>{md_inline(c)}</th>" for c in header)
            tbody = "".join("<tr>" + "".join(f"<td>{md_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
            continue
        m = _RE_LIST.match(ln)
        if m:
            items = []
            while i < n:
                m = _RE_LIST.match(lines[i])
                if not m:
                    break
                depth = len(m.group(1).replace("\t", "  ")) // 2
                marker = m.group(2)
                ordered = marker[0].isdigit()
                value = int(re.match(r"\d+", marker).group()) if ordered else None
                items.append((depth, ordered, m.group(3), value))
                i += 1
            out.append(render_list(items))
            continue
        para = []
        while i < n and lines[i].strip() and not (
                lines[i].lstrip().startswith("```") or _RE_HEAD.match(lines[i])
                or _RE_LIST.match(lines[i]) or _RE_QUOTE.match(lines[i]) or _RE_HR.match(lines[i])):
            para.append(lines[i])
            i += 1
        out.append(f"<p>{md_inline(chr(10).join(para)).replace(chr(10), '<br>')}</p>")
    return "\n".join(out)


# ===========================================================================
# HTML 输出
# ===========================================================================

CSS = """
:root{--bg:#f5f3ee;--card:#fff;--ink:#2a2520;--muted:#8a8378;--accent:#bd5d3a;
--user-bg:#eef3f8;--user-edge:#5a87b0;--ai-bg:#fff;--ai-edge:#bd5d3a;--line:#e4dfd5;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);line-height:1.75;font-size:15px}
#layout{display:flex;min-height:100vh}
#sidebar{width:320px;flex:none;background:#2a2520;color:#d8d2c6;position:sticky;top:0;height:100vh;
display:flex;flex-direction:column;padding:0}
#sidebar h1{font-size:16px;margin:0;padding:18px 16px 6px;color:#fff}
#sidebar .meta{font-size:12px;color:#9a9384;padding:0 16px 10px;border-bottom:1px solid #443d33}
#filter{margin:10px 12px;padding:8px 10px;border-radius:8px;border:1px solid #55503f;background:#1d1915;
color:#eee;font-size:13px;outline:none}
#toc{overflow-y:auto;flex:1;padding:0 8px 16px}
#toc a{display:block;padding:7px 10px;border-radius:8px;color:#cfc8ba;text-decoration:none;font-size:13px;
margin:2px 0;line-height:1.4}
#toc a:hover{background:#3b342b;color:#fff}
#toc a .d{display:block;font-size:11px;color:#857d6e}
#main{flex:1;min-width:0;padding:28px 40px 80px;max-width:980px;margin:0 auto}
.banner{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 26px;margin-bottom:30px}
.banner h1{margin:0 0 8px;font-size:22px}
.banner p{margin:4px 0;color:var(--muted);font-size:13px}
.conv{margin-bottom:48px}
.conv-head{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--accent);
border-radius:12px;padding:14px 20px;margin-bottom:18px}
.conv-head h2{margin:0 0 4px;font-size:18px}
.conv-head .t{font-size:12.5px;color:var(--muted)}
.msg{border-radius:12px;padding:14px 20px;margin:14px 0;border:1px solid var(--line);overflow-wrap:break-word}
.msg.human{background:var(--user-bg);border-left:4px solid var(--user-edge);margin-left:48px}
.msg.assistant{background:var(--ai-bg);border-left:4px solid var(--ai-edge);margin-right:48px}
.msg .who{font-weight:600;font-size:13px;margin-bottom:6px}
.msg.human .who{color:#3d6389}
.msg .who .ts{font-weight:400;color:var(--muted);margin-left:8px;font-size:12px}
.msg p{margin:8px 0}
.msg pre{background:#2a2520;color:#e8e2d4;padding:12px 14px;border-radius:8px;overflow-x:auto;
font-size:13px;line-height:1.55}
.msg code{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:13px}
.msg p code,.msg li code{background:#efe9dd;padding:1px 5px;border-radius:4px}
.msg table{border-collapse:collapse;margin:10px 0;font-size:13.5px;display:block;overflow-x:auto}
.msg th,.msg td{border:1px solid var(--line);padding:6px 10px;text-align:left}
.msg th{background:#f1ece2}
.msg blockquote{border-left:3px solid #c9c2b2;margin:8px 0;padding:2px 14px;color:#6b6457}
.msg a{color:#9c4a26}
details{margin:10px 0;border:1px dashed #d8d2c4;border-radius:9px;background:#faf8f2;font-size:13.5px}
details summary{cursor:pointer;padding:8px 14px;color:#7a7263;user-select:none;font-size:13px}
details[open] summary{border-bottom:1px dashed #e0dacc}
details .body{padding:10px 16px;max-height:480px;overflow-y:auto}
details.thinking{background:#f4f1fa;border-color:#cfc4e8}
details.thinking summary{color:#6b5a9e}
details .body pre{white-space:pre-wrap}
.cites{font-size:12.5px;color:var(--muted);margin-top:8px;padding-top:6px;border-top:1px dotted var(--line)}
.cites a{color:#8a6a4f;margin-right:4px}
.links li{margin:3px 0;font-size:13px}
.branch{border-top:2px dashed #cdb9a4;margin-top:26px;padding-top:10px}
.branch>.tag{display:inline-block;background:#f3e4d2;color:#8a5a2a;font-size:12.5px;border-radius:6px;
padding:3px 12px;margin-bottom:6px}
.attach-head{font-size:13px;color:#5c7350}
.toolbar{position:fixed;right:22px;bottom:22px;display:flex;gap:8px;z-index:9}
.toolbar button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:8px 12px;cursor:pointer;
font-size:12.5px;color:#555;box-shadow:0 2px 8px rgba(0,0,0,.08)}
.toolbar button:hover{background:#f5f0e6}
@media(max-width:860px){#sidebar{display:none}#main{padding:16px}
.msg.human{margin-left:12px}.msg.assistant{margin-right:12px}}
@media print{#sidebar,.toolbar{display:none}}
"""

JS = """
document.getElementById('filter').addEventListener('input',function(){
  var q=this.value.trim().toLowerCase();
  document.querySelectorAll('#toc a').forEach(function(a){
    a.style.display=(!q||a.textContent.toLowerCase().indexOf(q)>=0)?'':'none';});
});
function setAll(open){document.querySelectorAll('#main details').forEach(function(d){d.open=open;});}
"""


def html_tool_seg(payload):
    name = payload.get("name")
    shorts, longs = compact_input(payload.get("input"))
    title = f"🔧 {esc(tool_label(name))}"
    if shorts:
        title += f" <span style='font-weight:400'>· {esc(shorts[:160])}</span>"
    body = []
    for k, sv in longs:
        body.append(f"<div><strong>{esc(k)}：</strong></div><pre><code>{esc(truncate(sv, HTML_TOOL_RESULT_CAP, HTML_TRUNC_NOTE))}</code></pre>")
    result = payload.get("result")
    n_links = 0
    failed = False
    if result is not None:
        failed = bool(result.get("is_error"))
        if failed:
            body.append("<div style='color:#b3402a;font-weight:600'>⚠️ 本次调用失败，以下为错误信息：</div>")
        items = tool_result_items(result)
        links = [it for it in items if it[0] == "link"]
        texts = [it[1] for it in items if it[0] == "text"]
        n_links = len(links)
        if links:
            lis = "".join(f'<li><a href="{esc(u)}" target="_blank" rel="noopener">{esc(t)}</a></li>'
                          for _, t, u in links)
            body.append(f"<ul class='links'>{lis}</ul>")
        if texts and name != "web_search":
            body.append(f"<pre>{esc(truncate(chr(10).join(texts), HTML_TOOL_RESULT_CAP, HTML_TRUNC_NOTE))}</pre>")
    if failed:
        title += " <span style='color:#b3402a'>⚠️ 调用失败</span>"
    elif n_links:
        title += f" <span style='font-weight:400'>→ {n_links} 条结果</span>"
    if not body:
        return f"<details><summary>{title}</summary><div class='body'>（无返回内容）</div></details>"
    return f"<details><summary>{title}</summary><div class='body'>{''.join(body)}</div></details>"


def html_message(msg, user_name):
    cls = "human" if msg["sender"] == "human" else "assistant"
    who = f"👤 {esc(user_name)}" if cls == "human" else "🤖 Claude"
    parts = [f'<div class="msg {cls}"><div class="who">{who}'
             f'<span class="ts">{fmt_ts(msg.get("created_at"))}</span></div>']
    for kind, payload in normalize_message(msg):
        if kind == "text":
            parts.append(md_to_html(payload["text"]))
            if payload["citations"]:
                links = " ".join(
                    f'<a href="{esc(u)}" target="_blank" rel="noopener">[{i}]</a>'
                    for i, u in enumerate(payload["citations"], 1))
                parts.append(f'<div class="cites">引用来源：{links}</div>')
        elif kind == "thinking":
            parts.append(f'<details class="thinking"><summary>💭 思考过程</summary>'
                         f'<div class="body">{md_to_html(payload["text"])}</div></details>')
        elif kind == "tool":
            parts.append(html_tool_seg(payload))
        elif kind == "attachment":
            fname = payload.get("file_name") or "(未命名附件)"
            ftype = payload.get("file_type") or "?"
            size = payload.get("file_size") or 0
            extracted = (payload.get("extracted_content") or "").strip()
            inner = f"<pre>{esc(extracted)}</pre>" if extracted else "（无提取文本）"
            parts.append(f'<details><summary class="attach-head">📎 附件：{esc(fname)}'
                         f'（{esc(str(ftype))}，{size:,} 字节，{len(extracted):,} 字符）</summary>'
                         f'<div class="body">{inner}</div></details>')
        elif kind == "file":
            fname = payload.get("file_name") or payload.get("file_uuid") or "?"
            parts.append(f'<div class="attach-head">🖼️ 上传文件：{esc(fname)}（文件本体未包含在导出数据中）</div>')
    parts.append("</div>")
    return "".join(parts)


def render_html(convs, user_name, user_email, src_name):
    n_msgs = sum(len(c.get("chat_messages") or []) for c in convs)
    toc, body = [], []
    for i, c in enumerate(convs, 1):
        name = conv_title(c)
        cid = f"conv-{i}"
        n = len(c.get("chat_messages") or [])
        toc.append(f'<a href="#{cid}">{i}. {esc(name)}'
                   f'<span class="d">{fmt_ts(c.get("created_at"), "%Y-%m-%d %H:%M")} · {n} 条消息</span></a>')
        main, orphans = order_messages(c)
        sec = [f'<section class="conv" id="{cid}">',
               f'<div class="conv-head"><h2>{i}. {esc(name)}</h2>'
               f'<div class="t">创建 {fmt_ts(c.get("created_at"))} ｜ 最后更新 {fmt_ts(c.get("updated_at"))} ｜ {n} 条消息</div></div>']
        if not main and not orphans:
            sec.append('<p style="color:#8a8378">（此对话没有任何消息）</p>')
        sec += [html_message(m, user_name) for m in main]
        if orphans:
            sec.append(f'<div class="branch"><span class="tag">✂️ 编辑前的旧分支（{len(orphans)} 条，已被重新编辑覆盖）</span>')
            sec += [html_message(m, user_name) for m in orphans]
            sec.append("</div>")
        sec.append("</section>")
        body.append("".join(sec))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude 对话记录 · {esc(user_name)}</title>
<style>{CSS}</style>
</head>
<body>
<div id="layout">
<nav id="sidebar">
<h1>Claude 对话记录</h1>
<div class="meta">{esc(account_line(user_name, user_email))}<br>
{len(convs)} 个对话 · {n_msgs} 条消息<br>
{date_span(convs)}（{TZ_LABEL}）</div>
<input id="filter" type="search" placeholder="🔍 筛选对话标题…">
<div id="toc">{"".join(toc)}</div>
</nav>
<main id="main">
<div class="banner">
<h1>Claude 对话记录（完整存档）</h1>
<p>账户：{esc(account_line(user_name, user_email))} ｜ 共 {len(convs)} 个对话、{n_msgs} 条消息 ｜ 按创建时间从新到旧排列</p>
<p>生成来源：{esc(src_name)} ｜ 思考过程、工具调用、附件均已收录，点击虚线框可展开；联网搜索结果保留标题与链接。</p>
</div>
{"".join(body)}
</main>
</div>
<div class="toolbar">
<button onclick="setAll(true)">全部展开</button>
<button onclick="setAll(false)">全部收起</button>
<button onclick="window.scrollTo({{top:0,behavior:'smooth'}})">回顶部</button>
</div>
<script>{JS}</script>
</body>
</html>"""


# ===========================================================================

def main():
    global TZ, TZ_LABEL
    ap = argparse.ArgumentParser(
        prog="claude_export_reader",
        description="把 Claude.ai 导出的 conversations.json 还原为人类阅读友好的 TXT 和 HTML5 文件。")
    ap.add_argument("input", help="导出目录（含 conversations.json）或 conversations.json 文件路径")
    ap.add_argument("-o", "--outdir", help="输出目录（默认与输入文件同目录）")
    ap.add_argument("--basename", default="Claude对话记录",
                    help="输出文件名，不含扩展名（默认：Claude对话记录）")
    ap.add_argument("--tz", default="Asia/Shanghai",
                    help="时间显示时区，IANA 名称（默认：Asia/Shanghai）")
    args = ap.parse_args()

    try:
        TZ = ZoneInfo(args.tz)
    except Exception:
        sys.exit(f"无效的时区名称: {args.tz}（示例: Asia/Shanghai, America/New_York, UTC）")
    TZ_LABEL = "北京时间" if args.tz == "Asia/Shanghai" else args.tz

    src = args.input
    if os.path.isdir(src):
        src = os.path.join(src, "conversations.json")
    if not os.path.isfile(src):
        sys.exit(f"找不到文件: {src}")
    src_dir = os.path.dirname(os.path.abspath(src))
    outdir = args.outdir or src_dir
    os.makedirs(outdir, exist_ok=True)

    try:
        with open(src, encoding="utf-8") as f:
            convs = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"JSON 解析失败: {src}（{e}）")
    if not isinstance(convs, list):
        sys.exit("conversations.json 顶层应为对话数组，文件格式不符。")

    # 账户信息（仅用于页眉展示）来自导出包里的 users.json，可缺省
    user_name, user_email = "用户", ""
    upath = os.path.join(src_dir, "users.json")
    if os.path.exists(upath):
        with open(upath, encoding="utf-8") as f:
            users = json.load(f)
        if users:
            user_name = users[0].get("full_name") or "用户"
            user_email = users[0].get("email_address") or ""
    convs.sort(key=lambda c: c.get("created_at") or "", reverse=True)

    txt = render_txt(convs, user_name, user_email, os.path.basename(src))
    txt_path = os.path.join(outdir, args.basename + ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt)

    html_doc = render_html(convs, user_name, user_email, os.path.basename(src))
    html_path = os.path.join(outdir, args.basename + ".html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print(f"TXT : {txt_path}  ({os.path.getsize(txt_path):,} 字节)")
    print(f"HTML: {html_path}  ({os.path.getsize(html_path):,} 字节)")


if __name__ == "__main__":
    main()
