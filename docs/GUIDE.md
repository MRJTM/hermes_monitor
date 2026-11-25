# Hermes 行为树监控 · 使用与接入指南

目录：`/mlx_devbox/users/caozhijie.07/bot/hermes-monitor/`
网页：`https://ids4xd8t.merlin-devbox.tiktok-row.org`（`mlx export` 导出的 9010 端口，默认仅你本人可访问）

```
hermes-monitor/
├── backend/server.py       网页服务 + 采集 + 上报接口 + MCP 接口（纯标准库）
├── front/index.html      前端页面
├── backend/mcp_server.py   MCP stdio 代理（给 Hermes 用）
├── start.sh        启动/重启 + mlx export
├── data/.token          写接口口令（自动生成，权限 600，不要外传）
├── reports.json    Hermes 主动上报的条目（自动维护，保留 7 天）
└── names.json      飞书应用名缓存（roi-老李 等）
```

---

## 1. 两种数据来源，怎么选

| | 自动采集（pull） | 主动上报（push） |
|---|---|---|
| 谁来更新 | 网页服务自己读 Hermes 的 `state.db`、系统进程、Claude 的转录 | Hermes 自己调 MCP 工具 |
| 要不要 Hermes 配合 | **不要**，现在就在工作 | 要，得让它“记得”调 |
| 优点 | 不会漏、不会假（崩了也看得出） | 任务名/进展/“Claude 在干嘛”有明确语义 |
| 缺点 | 只能从命令里猜任务含义 | Hermes 忘了调就没有；崩了会残留“进行中” |

**建议两个一起开**：自动采集是兜底的真相，上报用来补语义。两者合并显示，上报的节点带 📡。
上报为“进行中”的条目如果超过 30 分钟没有更新（`ttl_s` 可改），会自动变成“被中断”，所以 Hermes 崩了也不会永远挂着。

---

## 2. 让 Hermes 接入（每个 Hermes 做一次）

### 2.1 注册 MCP

每个 Hermes 实例各执行一条，**`HERMES_HOME` 要换成它自己的目录**。命令末尾的 `--args` 必须放最后。

```bash
# roi-老李（hermes2）
echo Y | hermes2 mcp add hermes-monitor \
  --env HERMES_HOME=/mlx_devbox/users/caozhijie.07/bot/hermes2 \
  --command python3 --args -I /mlx_devbox/users/caozhijie.07/bot/hermes-monitor/backend/mcp_server.py

# roi-小西（hermes3）
echo Y | hermes3 mcp add hermes-monitor \
  --env HERMES_HOME=/mlx_devbox/users/caozhijie.07/bot/hermes3 \
  --command python3 --args -I /mlx_devbox/users/caozhijie.07/bot/hermes-monitor/backend/mcp_server.py

# 小助手1（hermes）：没有单独的命令包装，直接用 HERMES_HOME 指定
echo Y | HERMES_HOME=/mlx_devbox/users/caozhijie.07/bot/hermes hermes mcp add hermes-monitor \
  --env HERMES_HOME=/mlx_devbox/users/caozhijie.07/bot/hermes \
  --command python3 --args -I /mlx_devbox/users/caozhijie.07/bot/hermes-monitor/backend/mcp_server.py
```

- `--env HERMES_HOME=...` 的作用：代理靠它知道“我是哪个 Hermes”，Hermes 调工具时就**不用再填 `hermes` 参数**。
- 它会往该实例的 `config.yaml` 里写入一段 `mcp_servers.hermes-monitor`。
- 验证：`hermes2 mcp test hermes-monitor`，应显示 `Found 9 tool(s)`。
- 让正在运行的会话马上生效：在飞书里给它发 `/reload-mcp`，或者开一个新会话。

> 这些命令我已经在一份临时配置上试过，能成功添加并连通；**没有改动你三个 Hermes 的真实配置**，需要你执行（或让我代跑）。

### 2.2 告诉 Hermes “什么时候该报”

MCP 只是给了它工具，它不会自己知道该什么时候用。把下面这段加到每个 Hermes 的 `SOUL.md`（路径 `<HERMES_HOME>/SOUL.md`）末尾，重启或新会话后生效：

```markdown
## 状态汇报（监控网页）

你有一组 `hermes-monitor` 工具，用来把你正在做的事同步到监控网页，让用户随时能看到进度。规则：

1. 收到需要**多步骤或超过一分钟**的请求时，先调 `task_start`（title 用一句话概括，≤40 字），记住返回的 task_id。
   一句话就能答完的闲聊不要报。
2. 每派出一个 Claude Code（`claude -p ...`、tmux 里的 claude 都算），在启动前调 `claude_start`
   （带上 task_id、title=它负责什么），记住 leaf_id。
3. 长任务中，每隔几分钟或每完成一个阶段调一次 `task_update`（note=当前进展一句话）；
   Claude 在跑时用 `claude_update`（doing=它此刻在做什么）。这同时是心跳，30 分钟不更新会被标为“被中断”。
4. Claude 结束后立刻 `claude_finish`（失败要带 error）；全部做完后 `task_finish`（summary=结果一两句话）。
   失败/放弃时 status 用 `failed` 或 `interrupted`，不要不了了之。
5. 这些调用只是汇报，**失败了就忽略，不要因此中断或重试真正的任务**；也不要把它们的返回内容念给用户听。
```

---

## 3. 工具一览（MCP 与 HTTP 通用）

| 工具 | 什么时候调 | 关键参数 | 返回 |
|---|---|---|---|
| `task_start` | 开始处理一个请求 | `title`；可选 `detail` `chat` `note` `ttl_s` | `task_id` |
| `task_update` | 阶段进展 / 心跳 | `task_id`，`note` | ok |
| `task_finish` | 任务结束 | `task_id`，`status`=done/failed/interrupted，`summary` | ok |
| `claude_start` | 派出一个 Claude Code | `title`，`task_id`；可选 `prompt` `cwd` | `leaf_id` |
| `claude_update` | Claude 运行中 | `leaf_id`，`doing` | ok |
| `claude_finish` | Claude 结束 | `leaf_id`，`status`，`result` / `error` | ok |
| `list` | 核对网页上现在是什么 | 可选 `hermes` `only_running` | 文字版的树 |
| `remove` | 撤销填错的上报 | `id`（task_id 或 leaf_id） | ok |
| `clear` | 清理 | 可选 `hermes` `only_finished`（默认 true） | 清掉几条 |

- `hermes` 参数可填实例目录名（`hermes2`）或飞书应用名（`roi-老李`）。走 MCP 代理时自动填。
- 同一个 `task_id` / `leaf_id` 重复 start 是幂等的（更新而不是新建），可以自己指定 id。
- `claude_start` 的 `task_id` 也可以填**自动识别出来的任务 id**（`list` 能看到），这样上报的 Claude 会挂到自动识别的任务下。
- 修改网页只能改“上报的”条目；自动识别的节点以真实数据为准，改不了。

---

## 4. 不走 MCP：直接调 HTTP（脚本、cron、别的 agent）

写接口需要口令：`X-Monitor-Token: $(cat data/.token)`（或 `Authorization: Bearer ...`）。读接口不需要。

```bash
cd /mlx_devbox/users/caozhijie.07/bot/hermes-monitor; T=$(cat data/.token)
post(){ curl -s -X POST http://127.0.0.1:9010/api/report -H "X-Monitor-Token: $T" -d "$1"; echo; }

post '{"op":"task_start","hermes":"roi-老李","title":"重跑 ROI 实验","task_id":"roi-1"}'
post '{"op":"claude_start","task_id":"roi-1","title":"CC 跑全量 benchmark","leaf_id":"cc-1"}'
post '{"op":"claude_update","leaf_id":"cc-1","doing":"python run.py 第 3/10 折"}'
post '{"op":"claude_finish","leaf_id":"cc-1","status":"done","result":"全部跑完"}'
post '{"op":"task_finish","task_id":"roi-1","status":"done","summary":"结果已写入文档"}'
```

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/snapshot` | GET | 整棵树的 JSON（页面每 3 秒拉一次） |
| `/api/reports` | GET | 当前所有上报条目 |
| `/api/tools` | GET | 工具清单与参数 |
| `/api/report` | POST | `{"op": 工具名, ...参数}`，需口令 |
| `/mcp` | POST | MCP（JSON-RPC 2.0）的 HTTP 入口，需口令（stdio 代理就是转发到这里；HTTP 方式直连我没有测过，推荐用上面的 stdio 注册） |
| `/healthz` | GET | 存活检查 |

通过公网链接（`mlx export`）调用时，要额外带 `X-JWT-Token`（见 `mlx export` 输出的提示）；Hermes 与网页在同一台机器上，直接用 `127.0.0.1:9010` 即可。

---

## 5. 运维

- 启动/重启：`./start.sh`（同时重新 `mlx export`）。开发机重启后需要手动再执行一次。
- 日志：`server.log`。
- 改口令：删掉 `data/.token` 再重启，然后更新各处用到它的地方（MCP 代理每次都读文件，不用改配置）。
- 窗口：默认采集最近 7 天（`WINDOW_HOURS`），网页上可以再缩小。
- 不接入也能用：不注册 MCP，页面照样靠自动采集工作，只是没有 📡 的语义信息。

## 6. 常见问题

- **Hermes 调工具报“监控网页服务不可用”**：网页服务没起，执行 `./start.sh`。
- **报 401**：口令不对，或 MCP 代理读不到 `data/.token`（两者要在同一用户下）。
- **网页上有重复的两个任务**：一个是自动识别的、一个是 Hermes 上报的，属于正常。想合并，让 Hermes 在 `claude_start` 里把 `task_id` 填成自动识别任务的 id，或者之后接受只看上报的那一个。
- **上报的任务一直“进行中”**：Hermes 没调 `task_finish`；超过 `ttl_s`（默认 30 分钟）会自动变成“被中断”，也可以手动 `remove`。
- **改了 SOUL.md 没效果**：系统提示词一般只在新会话开始时读取，已经在跑的长会话可能要重置（`/new`）才会用上新规则。
