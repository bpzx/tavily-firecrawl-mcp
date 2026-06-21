# tavily-firecrawl-mcp

一个用于网页抓取和搜索的 [MCP](https://modelcontextprotocol.io)(Model Context Protocol)服务器。

- **`web_fetch`** —— 把网页正文提取为 Markdown(或纯文本)。以 **Tavily Extract** 为主用,失败或返回空时**自动回退**到 **Firecrawl Scrape**。
- **`web_search`** —— 用 **Tavily Search** 搜索网页(返回标题、URL、摘要,可选合成答案)。无备用。

API key 从环境变量读取。未配置 key 的 provider 会被直接跳过,不会向它发起任何请求。

## 适用场景

- 抓取单个网页的正文内容(博客、文档、新闻等)。
- 当主用服务(Tavily)不可用时,自动用备用服务(Firecrawl)兜底,提高可用性。
- 用 Tavily 做联网搜索,获取最新信息。

## 环境要求

- Python ≥ 3.11
- [Tavily](https://tavily.com) API key(`TAVILY_API_KEY`)
- 可选:[Firecrawl](https://firecrawl.dev) API key(`FIRECRAWL_API_KEY`),用于抓取回退。不配则 `web_fetch` 仅用 Tavily。

## 运行方式

本服务用 Python 实现,推荐用 **uvx**(uv 的命令运行器)运行 —— 无需全局安装、自动隔离环境。

### 方式一:uvx(推荐)

无需 `pip install`,直接运行。两种来源:

```bash
# 已发布到 PyPI 时
uvx tavily-firecrawl-mcp

# 或者直接从 git 仓库运行
uvx --from "git+https://github.com/bpx/tavily-firecrawl-mcp" tavily-firecrawl-mcp
```

> uvx 会根据 `pyproject.toml` 的 `[project.scripts]` 入口点(`tavily-firecrawl-mcp = tavily_firecrawl_mcp.server:main`)找到并运行服务。首次运行会自动创建隔离环境并安装依赖。

### 方式二:本地 pip 安装

在本目录下:

```bash
pip install -e .
```

然后即可通过命令 `tavily-firecrawl-mcp` 或 `python -m tavily_firecrawl_mcp` 启动。

### 调试 / 检查

用官方 [MCP Inspector](https://github.com/modelcontextprotocol/inspector) 可视化调试:

```bash
npx @modelcontextprotocol/inspector uvx tavily-firecrawl-mcp
```

## 配置 API key

在环境(或 MCP 客户端加载的 `.env`)中设置:

```bash
export TAVILY_API_KEY=tvly-xxxxxxxxxxxx
export FIRECRAWL_API_KEY=fc-xxxxxxxxxxxx   # 可选
```

仓库内附 `.env.example`。

### 出站代理(可选)

当 Tavily / Firecrawl 的 API 端点在你的网络下无法直连或不稳定(例如国内网络),可通过 `WEB_FETCH_PROXY` 指定本地代理:

```bash
export WEB_FETCH_PROXY=http://127.0.0.1:7890
```

设置 `WEB_FETCH_PROXY` 后它优先级最高,并禁用 httpx 的环境变量回退;未设置时,httpx 仍会读取标准的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 环境变量。

## 工具

### `web_fetch(url, ...)`

| 参数                | 类型                    | 默认值       | 说明                                              |
| ------------------- | ----------------------- | ------------ | ------------------------------------------------- |
| `url`               | `str`                   | 必填         | 要抓取的页面 URL。                                |
| `query`             | `str \| None`           | `None`       | Tavily 会按该意图对提取内容重排。                 |
| `format`            | `"markdown" \| "text"`  | `"markdown"` | 输出格式。                                        |
| `extract_depth`     | `"basic" \| "advanced"` | `"basic"`    | Tavily 提取深度。                                 |
| `only_main_content` | `bool`                  | `True`       | 仅 Firecrawl 生效 —— 去掉导航/广告/页脚。         |
| `timeout`           | `int`                   | `30`         | 单次请求超时,单位秒。                            |

返回 `{ ok, url, provider, content, format, used_fallback, failed_providers, metadata }`。
当结果由 Firecrawl 在 Tavily 失败后产出时,`used_fallback` 为 `true`。

### `web_search(query, ...)`

| 参数             | 类型                    | 默认值     | 说明                          |
| ---------------- | ----------------------- | ---------- | ----------------------------- |
| `query`          | `str`                   | 必填       | 搜索词。                      |
| `max_results`    | `int`                   | `5`        | 结果数量(1–20)。             |
| `search_depth`   | `"basic" \| "advanced"` | `"basic"`  | advanced 更慢但更深入。       |
| `topic`          | `"general" \| "news"`   | `"general"`| 切换为新闻搜索。              |
| `include_answer` | `bool`                  | `False`    | 是否同时返回合成答案。        |

返回 `{ ok, query, answer, results: [{title, url, content, score}] }`。

## 回退机制说明

`web_fetch` 按顺序遍历 provider 链:**Tavily → Firecrawl**。

1. 若 Tavily 已配置且返回非空内容 → 直接返回。
2. 若 Tavily 因*可重试*错误失败(auth、限流、5xx、网络、空内容)且 Firecrawl 已配置 → 尝试 Firecrawl,标记 `used_fallback: true`,并把 Tavily 的失败记录写入 `failed_providers`。
3. *不可重试*错误(如 404)会立即抛出,不尝试备用。
4. 所有 provider 都失败或都未配置时,工具返回 `ok: false` 及结构化的 `failures` 列表,便于调用方处理。

未配置 key 的 provider 不会出现在链中,因此只配一个 key 即可实现 Tavily-only 或 Firecrawl-only。

## 客户端配置

### ZCode(`~/.zcode/mcp.json` 或项目内 `.zcode/mcp.json`)

> 注意:ZCode 的配置文件格式与 Claude Desktop **不同** —— 顶层的 key 直接就是每个 server 的名字,**没有 `mcpServers` 外层包裹**,且每个 server 对象里需要带 `"type": "stdio"`。

```json
{
  "tavily-firecrawl": {
    "type": "stdio",
    "command": "uvx",
    "args": ["tavily-firecrawl-mcp"],
    "env": {
      "TAVILY_API_KEY": "tvly-xxxxxxxxxxxx",
      "FIRECRAWL_API_KEY": "fc-xxxxxxxxxxxx",
      "WEB_FETCH_PROXY": "http://127.0.0.1:7890"
    }
  }
}
```

> 【可选】从 git 运行:
> ```json
> "args": ["--from", "git+https://github.com/bpx/tavily-firecrawl-mcp", "tavily-firecrawl-mcp"]
> ```

### Claude Desktop / Claude Code(`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "tavily-firecrawl": {
      "command": "uvx",
      "args": ["tavily-firecrawl-mcp"],
      "env": {
        "TAVILY_API_KEY": "tvly-xxxxxxxxxxxx",
        "FIRECRAWL_API_KEY": "fc-xxxxxxxxxxxx",
        "WEB_FETCH_PROXY": "http://127.0.0.1:7890"
      }
    }
  }
}
```

### 通用 stdio 客户端

```json
{
  "command": "uvx",
  "args": ["tavily-firecrawl-mcp"]
}
```

确保服务端继承的环境中已导出 `TAVILY_API_KEY`(以及可选的 `FIRECRAWL_API_KEY` / `WEB_FETCH_PROXY`)。

## 开发

```bash
pip install -e ".[dev]"
pytest
```

## 许可证

MIT
