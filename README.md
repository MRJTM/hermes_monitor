# Hermes Monitor · 多 Agent 行为树监控

把多个 Hermes（飞书 AI 助手）实例正在做的事，实时画成一棵可点击的树：

```
Hermes 实例（根）
└── 任务状态（进行中 / 被中断 / 已完成）
    └── 对话任务（用户请求、进展、耗时）
        └── 工人叶子（Claude Code / opencode / Antigravity）
```

- 零第三方依赖，只用 Python 标准库（≥ 3.9）和一张单文件前端页面。
- **自动采集**：只读各实例的 `state.db`、`/proc`、Claude/opencode/Antigravity 的本地转录，不需要改动 Hermes。
- **主动上报**：通过 MCP 工具让 Hermes 自己汇报任务名、进展和结果，和自动采集合并展示。
- **后台 Agent**：用任意 OpenAI 兼容大模型自动把冗长的原始指令总结成「任务名 + 期望产出」，并定期生成全局巡检摘要。
- 深色 / 浅色主题，支持按时间窗口过滤、只看进行中、点击节点看详情。

## 目录结构

```
hermes_monitor/
├── front/index.html        单文件前端（内联 CSS/JS，轮询 /api/snapshot 渲染）
├── backend/server.py       HTTP 服务 + 采集 + 上报接口 + MCP 接口
├── backend/mcp_server.py   MCP stdio 薄代理（把 Hermes 的调用转发给 server.py）
├── start.sh                启动 / 重启（可选自动 mlx export 公网链接）
├── .env.example            配置模板（复制成 .env 后填写）
├── .env                    你的真实配置与密钥（已在 .gitignore，不会上传）
├── data/                   运行期数据：token、名称缓存、AI 摘要缓存、上报记录（不入库）
└── docs/GUIDE.md           更详细的接入说明（MCP 注册、上报规范、排错）
```

## 快速开始（在一台新机器上）

1. 准备 Python 3.9+，克隆仓库：

   ```bash
   git clone https://github.com/MRJTM/hermes_monitor.git
   cd hermes_monitor
   ```

2. 生成配置：

   ```bash
   cp .env.example .env
   ```

   至少改这几项：
   - `HERMES_BOT_ROOT`：Hermes 实例根目录（其下每个子目录是一个实例，需含 `state.db`）
   - `LLM_URL` / `LLM_MODEL` / `LLM_KEY`：后台总结 Agent 用的大模型
   - `HOST`：本机看填 `127.0.0.1`；要公网导出填 `::`（IPv6）

3. 启动：

   ```bash
   ./start.sh
   ```

   打开 `http://127.0.0.1:9010/` 即可。脚本会先停掉占用该端口的旧 `server.py`，再后台启动；检测到 `mlx` 时会顺带导出一个公网 URL。

4. 想换端口就改 `.env` 里的 `PORT`（默认 9010，避免和其它项目冲突）。

> 不用 `start.sh` 也可以直接 `python3 -I backend/server.py`；服务启动时会自动读取仓库根目录的 `.env`。

## 配置项（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `9010` | HTTP 端口 |
| `HOST` | `127.0.0.1` | 监听地址；公网导出用 `::` 或 `0.0.0.0` |
| `WINDOW_HOURS` | `24` | 采集多久以内的任务 |
| `HERMES_BOT_ROOT` | `~/bot` | Hermes 实例根目录 |
| `HERMES_WRAPPER_DIR` | `~/.local/bin` | claude / opencode / antigravity 的 wrapper 目录 |
| `CLAUDE_HOMES` | `~/.claude:~/.claude_real:~/.claude_personal` | Claude Code 转录目录，冒号分隔 |
| `MONITOR_DATA_DIR` | `data` | 运行数据目录（相对路径按仓库根解析） |
| `LLM_URL` | — | OpenAI 兼容的 `.../v1/chat/completions` |
| `LLM_MODEL` | — | 模型名（若模型会输出思考内容，建议用 nothink 变体） |
| `LLM_KEY` | — | 模型 API Key，**只放 .env** |
| `TITLE_S` | `60` | 任务/工人摘要的后台刷新周期（秒） |
| `INSPECT_S` | `600` | 全局巡检摘要周期（秒） |
| `MONITOR_URL` | `http://127.0.0.1:9010` | MCP 代理要访问的网页服务地址 |
| `MONITOR_TOKEN` | 空 | 写接口口令；留空则读 `data/.token`（首次启动自动生成） |

大模型不可用时服务不会挂：自动采集照常工作，AI 摘要和巡检会降级（页面上会显示错误信息），恢复后自动补上。

## 数据是怎么来的

### 自动采集（pull，只读）

| 来源 | 用途 |
|---|---|
| `<实例>/state.db` 的 `sessions` / `messages` / 异步委派表 | 找出「谁派了哪个工人、原始指令、结果」 |
| `/proc` 进程表 + 命令行匹配 | 确认真实存活的工人进程，并识别 harness（claude / opencode / antigravity） |
| `~/.claude*/projects/*/*.jsonl` | 读 Claude Code 此刻在做什么 |
| opencode `~/.local/share/opencode/opencode.db` | 读 opencode 会话与消息 |
| Antigravity `~/.gemini/antigravity-cli/` | 读 Antigravity 对话与 transcript |
| 各实例 `gateway.pid` / gateway 日志 | 判断实例在线 / 忙 / 闲 |

浏览器只跟本机的 `server.py` 通信，密钥不会出现在前端。

### 主动上报（push，可选）

Hermes 通过 MCP 工具调用 `server.py` 的 `/mcp`；`backend/mcp_server.py` 是一个 stdio 薄代理，用 `HERMES_HOME` 自动识别实例名。工具：

| 工具 | 作用 |
|---|---|
| `task_start` | 开始处理用户请求：新建任务，返回 `task_id` |
| `task_update` | 更新进展 / 标题 / 状态（同时刷新心跳） |
| `task_finish` | 结束任务：done / failed / interrupted + 摘要 |
| `harness_start` | 派工人：新建叶子（claude / opencode / antigravity），返回 `leaf_id` |
| `harness_update` | 更新工人此刻在做什么 |
| `harness_finish` | 结束工人并附结果 / 错误 |
| `claude_start/update/finish` | 旧名兼容，等同 harness_* |
| `list` / `remove` / `clear` | 查看当前树、删除报错的条目、清空已结束的上报 |

注册到某个 Hermes（每个实例执行一次，`HERMES_HOME` 换成它自己的目录）：

```bash
echo Y | hermes2 mcp add hermes-monitor \
  --env HERMES_HOME=/path/to/hermes2 \
  --command python3 --args -I /path/to/hermes_monitor/backend/mcp_server.py
```

之后在飞书发 `/reload-mcp` 或开新会话生效。验证：`hermes2 mcp test hermes-monitor`。

也可以用 HTTP 直接上报（写接口需要 `data/.token`）：

```bash
T=$(cat data/.token)
curl -s -X POST http://127.0.0.1:9010/api/report \
  -H "X-Monitor-Token: $T" -H "Content-Type: application/json" \
  -d '{"op":"task_start","title":"修登录 bug","hermes":"hermes2"}'
```

上报的「进行中」条目超过 `ttl_s`（默认 30 分钟）没更新会自动转为「被中断」，所以 Hermes 崩了也不会永远挂着。

## 后台 Agent

两个后台线程复用同一个 LLM：

1. **摘要 Agent**：把任务原文 / 工人原始指令压缩成 ≤14 汉字的任务名和 ≤32 汉字的期望产出。缓存按内容哈希存在 `data/task_titles.json`、`data/leaf_notes.json`，原文变了才重新总结。页面上工人抽屉里**不展示原始指令**，只展示这份概览。
2. **巡检 Agent**：定期扫描所有实例的运行中任务，生成一段全局摘要显示在页头下方的「巡检摘要」面板。

## HTTP 接口

| 路径 | 说明 |
|---|---|
| `GET /` | 前端页面 |
| `GET /api/snapshot` | 当前完整快照（instances → tasks → leaves + inspect） |
| `POST /mcp` | MCP JSON-RPC（`initialize` / `tools/list` / `tools/call`） |
| `POST /api/report` | 上报接口（需要 `X-Monitor-Token`） |

## 常见问题

- **打开网页却是别的项目**：端口被占了。改 `.env` 的 `PORT`，`start.sh` 只会杀自己的 `server.py`。
- **页面空白 / 没有实例**：检查 `HERMES_BOT_ROOT` 是否正确、该目录下是否有含 `state.db` 的实例；看 `server.log`。
- **写接口 401**：口令不对；确认 `data/.token` 与请求头一致，且 MCP 代理用同一用户运行。
- **AI 概览一直「正在总结」**：看 `server.log` 里的 `title loop error` / `leaf llm error`，通常是 `LLM_URL` 或 `LLM_KEY` 不对。

## 安全

- `.env` 和 `data/` 已被 `.gitignore` 排除，不会上传；`data/.token` 创建时权限为 600。
- 只读采集本机文件，不修改 Hermes 的任何数据。
- 如需公网访问，建议用 `mlx export`（自带鉴权）或反向代理加认证，不要直接把端口裸奔到公网。
