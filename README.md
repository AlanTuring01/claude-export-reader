# claude-export-reader

把 [Claude.ai](https://claude.ai) 导出的 `conversations.json` 还原成**人类阅读友好**的 TXT 和单文件 HTML5 存档。

单个 Python 脚本，零第三方依赖，离线运行——你的对话数据不会离开本机。

> **English**: Convert your Claude.ai data export (`conversations.json`) into a human-readable
> plain-text archive and a self-contained single-file HTML5 viewer (sidebar TOC with live filter,
> collapsible thinking/tool-call/attachment blocks, Markdown rendering, edit-branch restoration).
> Single Python script, zero dependencies, fully offline. The UI text is Chinese.

## 效果

- **`Claude对话记录.html`** — 单文件网页，双击即可离线打开：
  - 左侧深色目录栏，支持按标题即时筛选、点击跳转
  - 用户 / Claude 消息卡片，带本地时区时间戳
  - 内置轻量 Markdown 渲染（表格、代码块、嵌套列表、链接、转义字符）
  - 思考过程（💭）、工具调用（🔧）、附件（📎）折叠收纳，可一键全部展开/收起
  - 失败的工具调用标红 ⚠️，正文引用来源以 [1][2] 链接列出
- **`Claude对话记录.txt`** — 纯文本阅读版：卷首目录 + 全部对话，方便全文检索、喂给其他程序或打印

## 用法

```bash
# 输入为导出目录（含 conversations.json，users.json 可选）
python3 claude_export_reader.py /path/to/your-export-folder

# 或直接指定 JSON 文件，并自定义输出目录 / 文件名 / 时区
python3 claude_export_reader.py conversations.json -o ./out --basename my-archive --tz America/New_York
```

要求 Python ≥ 3.9（使用标准库 `zoneinfo`），无需安装任何包。

先用自带样例试跑：

```bash
cd examples
cp sample-conversations.json conversations.json
cp sample-users.json users.json
python3 ../claude_export_reader.py .
```

> 如何拿到导出数据：Claude.ai → Settings → Privacy → Export data，邮件里会收到包含 `conversations.json` 和 `users.json` 的压缩包。

## 还原规则与取舍

“忠实还原”与“阅读友好”有冲突时，本工具的取舍如下（均可在源码顶部的常量里调整）：

| 内容 | TXT | HTML |
|------|-----|------|
| 用户消息 / Claude 正文 | 全文 | 全文 + Markdown 渲染 |
| 思考过程（thinking） | 全文，缩进标注 | 全文，默认折叠 |
| 联网搜索结果 | 仅标题 + 链接 | 仅标题 + 链接（原始转储动辄数百万字符，会淹没正文） |
| 其他工具输出 | 截断至 1200 字符 | 截断至 50000 字符 |
| 附件提取文本 | 截断至 800 字符 | 全文，默认折叠 |
| Claude 创建的文件内容 | 截断至 4000 字符 | 代码块（超 50000 字符截断） |

其他还原细节：

- **编辑分支**：在 Claude.ai 里编辑过的消息会在导出数据中留下旧分支。脚本沿 `parent_message_uuid` 链从最新叶子回溯出主线，被覆盖的旧消息单独归入「✂️ 编辑前的旧分支」区段，不丢内容。
- **工具结果反序列化**：`bash_tool` 等工具的结果是 JSON 字符串（中文还被转义成 `\uXXXX`），脚本会解析还原成可读文本。
- **时间**：全部转为 `--tz` 指定时区（默认 Asia/Shanghai）。
- 排序：对话按创建时间从新到旧；空标题显示为「(未命名对话)」。

## 隐私提醒

- 生成的 TXT / HTML **包含你的全部对话内容和账户信息**（页眉会显示 `users.json` 里的姓名和邮箱）。分享或公开前请自行确认。
- 本仓库的 `.gitignore` 已默认排除 `conversations.json`、`users.json` 和生成的存档文件，避免误提交真实数据。

## 兼容性

针对 2026 年 6 月时的 Claude.ai 导出格式编写，已覆盖的内容块类型：`text`（含 citations）、`thinking`、`tool_use`、`tool_result`、`token_budget`，以及消息级的 `attachments` / `files`。Anthropic 若调整导出格式，未识别的块会被静默跳过——升级前建议先在小样本上核对。

## License

[MIT](LICENSE)
