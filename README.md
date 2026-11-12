# payback-agent

四级**智能短线复盘助手**的内核：一个裸写的 OpenAI `tool_calls` 循环，工具源是
[`payback-mcp`](../payback-mcp) 的只读 stdio MCP server。

回答必须走这条链，且每一级都挂在工具返回的数据上：

① 当日市场整体环境 → ② 短线生态处于什么阶段 → ③ 是否具有出手机会 → ④ 买点与卖点的结构性条件

每级对应的工具（`prompts/system.md` 里写死了同样的要求）：

| 级 | 工具 |
|---|---|
| ① | `get_market_overview` + `get_limit_up_structure` |
| ② | `get_market_phase` |
| ③ | `get_position_outcomes` |
| ④ | `get_similar_days`（把 ①②③ 合成"像今天这样的盘面，历史上次日发生了什么"）+ `get_even_board_ladder` |

设计文档：`payback/docs/superpowers/specs/2026-10-15-payback-agent-design.md`。
行情数字只能来自工具；Agent 使用独立 MySQL 账号存储会话与用户偏好。

## 前置条件

- [uv](https://docs.astral.sh/uv/)
- 同级目录下有可运行的 `payback-mcp`（agent 用 `uv --directory <它> run payback-mcp` 拉起的 stdio 子进程）
- Codex CLI 登录，或一个 OpenAI 兼容网关的三个配置项

```bash
cd D:/workspace/github/payback-all/payback-agent
uv sync
uv run pytest        # 全假 LLM + 假工具源，不联网、不起子进程
```

## 配置 `.env`

```ini
AGENT_LLM_BASE_URL=https://api.deepseek.com/v1   # 填到 /v1，代码自己拼 /chat/completions
AGENT_LLM_API_KEY=<密钥>
AGENT_LLM_MODEL=deepseek-chat
# 可选
#PAYBACK_MCP_DIR=D:/workspace/github/payback-all/payback-mcp
#PAYBACK_MCP_COMMAND=uv
#AGENT_MAX_TURNS=8
```

`.env` 已被 gitignore；凭据不进代码、不进日志、不进 trace。gateway 模式缺这三个键时 CLI 直接退出码 2
并把缺的键名列出来——**不会**降级成"模型凭记忆回答"，那是这个产品最不能犯的错。

## 用法

配置 `payback-mcp/.env` 中的 `FUYAO_API_KEY` 后，可询问“000001 明日盘中预案及短中长线支撑压力”。助手依据同花顺前复权日线给出量能与历史高低点观察条件；行情完成状态、停牌及除权需另行核实，盘中实时触发尚未接入。

```bash
# 1) 不要网关也能跑：把四级证据取全，验证数据链路
uv run payback-agent --dry-run --for-date yesterday
uv run payback-agent --dry-run --for-date 2026-10-13 --level 2

# 2) 一次性提问
uv run payback-agent "帮我复盘一下昨天"

# 3) REPL（回车提问，exit 退出；记住最近 8 轮的问答，数字每轮重新取）
uv run payback-agent

# 4) 留证据：每一轮请求/响应/工具调用写 JSONL，可回放
uv run payback-agent --trace traces/review.jsonl "现在打二板值不值得"
uv run payback-agent --json --quiet "昨天涨停家数"     # 结构化输出，方便脚本消费
uv run payback-agent "生成2026-11-09短线涨停梯队图片"  # 返回内嵌 SVG 的 Markdown

# 5) serve 模式：HTTP+SSE 服务，给 payback 网页的 /chat 页面用
uv run payback-agent --serve                 # 默认 127.0.0.1:8300
uv run payback-agent --serve --host 0.0.0.0 --port 8300

# 没有网关凭据也能联调网页：先起桩网关（假 LLM，真 payback-mcp）
uv run python scripts/dev_gateway_stub.py --port 8310
AGENT_LLM_BASE_URL=http://127.0.0.1:8310/v1 \
AGENT_LLM_API_KEY=stub-not-a-secret \
AGENT_LLM_MODEL=stub-model \
uv run payback-agent --serve
```

涨停梯队图顶部展示同一实际交易日的短线周期、晋级率、封板率、涨停数和跌停数；指标缺失或日期不一致时显示“未提供”。晋级率采用昨日涨停股今日晋级连板的比例，数据来自 `get_market_phase`，相邻采集日可能存在缺采。

stdout 只有答案（或 `--json` 的结果），工具调用清单、警告、REPL 提示全部走 stderr。
正文在证据检查后一次性发送；工具调用进度仍实时发送。行情问题缺少有效工具结果或工具失败时返回证据不足，并标注实际数据日期和日期不一致。时效无法确认时不将历史数据作为当前盘面判断。

今日与昨日短线生态对比会预取两日 `get_market_phase`，同一工具的不同日期证据分别保留；两份数据均有效且包含今日时允许比较已采集快照，明确标注两日实际日期及可用的原始记录创建时间。创建时间不等于更新时间或收盘完成证明；实时操作问题仍受时效校验，今日数据缺失时不会用昨日数据替代。

## serve 模式（网页对接）

“每日 AI 全景复盘”使用 `get_daily_panorama` 读取同一交易日的市场、板块、梯队和跌停名单，并在生成时读取腾讯外围行情；外围是生成时最新报价，可能延迟或属于其他交易日。Agent 服务在工作日 16:10 后每 15 分钟检查一次，当日数据完整且未归档时生成复盘；市场和短线记录的时间必须为当日 15:00 或之后，非交易日、午盘数据或数据缺失不会被当作收盘数据。服务需持续运行，生成失败会在当日下次检查时重试；生成结果保存在 `data/daily_reports/`。若记录时间未随更新而刷新，或历史补录时间不是交易当日，需先核实数据，不能自动生成。

`GET /daily-reports/latest` 读取最新报告，`GET /daily-reports/YYYY-MM-DD` 读取指定日期，`POST /daily-reports/YYYY-MM-DD` 手动重新生成。报告包含市场快照、生成时外围报价及 AI 文本；历史日期的外围报价不是历史行情。与现有 `/chat` 相同，这些端点无鉴权，仅应在可信本机或受保护的反向代理后使用。

会话列表使用 `GET /sessions?view=summary&limit=30`：返回 `{items, nextCursor}`，摘要仅含 `id/title/lastTurnId/snippet`。追加 `cursor=<nextCursor>` 获取更早会话，`q=<关键词>` 搜索完整历史的标题来源（首问）及问答正文，返回命中片段。搜索词最多 200 字符，limit 为 1–100；cursor 为正 BIGINT 字符串，避免浏览器精度损失。旧表没有时间字段，以最后一轮消息 ID 降序分页，不伪造历史日期。

`GET /sessions/{id}` 按需读取完整问答，缺失返回 404。无 `view=summary` 的旧 `GET /sessions` 保持返回最近 50 条完整会话，兼容已有调用。摘要分页是一次聚合查询；正文搜索当前按字面扫描文本，历史量很大时需要评估全文索引。长期偏好可通过 /memories 的 GET、POST、PUT、DELETE 管理。

以下端点无鉴权，默认只听 127.0.0.1：

Codex 后端显式请求 `model_reasoning_summary="detailed"`，通过 `reasoning_delta` 转发模型实际返回的摘要。模型可能不提供摘要；聊天页生成期间展示等待状态，结束后提示“模型未提供思考摘要”，不会把等待提示当作模型思考内容。修改后需重启 Agent 服务。

| 端点 | 行为 |
|---|---|
| `GET /healthz` | `{"status":"ok","sessions":N}` |
| `POST /chat` `{message, session_id?}` | SSE 流；不带 `session_id` 则新建并在首个事件里返回 |
| `DELETE /sessions/{id}` | 丢弃一个会话的服务端历史 |
| `GET /daily-reports/latest` | 读取最新一份每日复盘 |
| `GET /daily-reports/{date}` | 读取指定交易日的复盘 |
| `POST /daily-reports/{date}` | 生成或重新生成指定交易日的复盘 |
| `POST /review-charts/{date}` | 校验同日收盘数据，返回全景图与完整梯队图 SVG base64，供定时推送使用；无模型调用 |

收盘双图由 payback server 在交易日北京时间 15:38 触发，push-server 转成 PNG 后推送飞书和企微。此接口复用串行 MCP worker，避免与聊天并发争用标准输入输出；必需的大盘、短线、资金和梯队记录须为指定日 15:00 后的数据，否则返回 503。梯队的周期、封板率和涨跌停数使用本次短线原始快照；晋级率若没有同日收盘后的派生表记录则显示“未提供”。日期格式错误返回 400。返回结构为 `{data_as_of, charts: [{name: "panorama", svg_base64}, {name: "ladder", svg_base64}]}`。部署需同时更新三个服务，server 配置 `PAYBACK_AGENT_URL`，push-server 配置飞书图片上传应用凭据及中文字体。接口只允许可信本机或受保护内网调用，不要直接暴露公网。

SSE 事件就是 `ask_events` 的事件流，外加两个服务级事件：

```
session      {"session_id"}                    第一个，客户端此后带上它续会话
delta        {"iteration","text"}              流式正文
tool_call    {"iteration","name","arguments"}  执行前（前端渲染"正在查询"）
tool_result  {"iteration","name","error",...}  执行后（工具原文 result 不下发）
done         {"content","truncated","usage"}   权威正文；消息栈 messages 不下发
error        {"message"}                       本轮失败（会话历史保持干净）
```

线上瘦身是刻意的（`serve._DROP_ON_WIRE`）：一次四级取证的工具 JSON 约 2.9 万字符，
网页只渲染芯片状态和正文，原文没有理由出网卡；CLI/trace 走 `ask_events` 拿全量。

实现要点（细节在 `serve.py` 模块 docstring）：单个 worker 任务独占 MCP 会话与网关
client，请求只通过 memory channel 投递作业——anyio cancel scope 绝不跨 `yield`，
客户端断连在 worker 的下一次事件发送处自然传播，丢弃本轮、会话不受污染。
服务端将完成的问答和用户显式保存的长期偏好存入 MySQL。按 .env.example 配置专用的 AGENT_MEMORY_MYSQL_* 账号；账号需要目标数据库的 CREATE 权限，以及三张 agent_* 记忆表的 SELECT、INSERT、UPDATE、DELETE 权限。服务首次访问时创建表，行情 MCP 的只读账号和权限不受影响。上下文仍只携带最近 8 轮，工具原文不进入历史。前端首屏获取 30 条摘要，触底继续分页，点击后获取正文；可重新获取的正文最多缓存 5 个会话。localStorage 只保存最近摘要、活动入口和未同步内容，旧本机记录保守保留，不再新增即截断 50 条；本机空间不足时显示保存失败提示。升级需要同时更新前端并重启 Agent 服务，无数据库改表。

升级时，如已有 SQLite 文件，应在新 MySQL 目标为空、启动 Agent 并写入新记录之前执行 uv run python -m payback_agent.migrate_memory [旧文件路径]。省略路径时优先读取旧 AGENT_MEMORY_PATH，否则使用 data/memory.sqlite3。迁移以事务写入且保留源文件，相同快照重复执行不会重复导入；目标已有数据时会拒绝导入，需先备份并人工处理。只有浏览器 localStorage 的旧记录不包含在此迁移中。

长期记忆可在聊天页“管理长期记忆”中新增、编辑、删除，每条最多 500 字，最多 50 条。记忆仅作为用户偏好，不替代本轮行情工具；接口与 /chat 一样没有鉴权，只能在可信环境使用。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常回答 |
| 1 | 网关失败（HTTP 4xx/5xx、连不上、响应体不可用），或 `--dry-run` 里有工具报错 |
| 2 | 配置或启动问题：缺 `AGENT_LLM_*`、`AGENT_MAX_TURNS` 非法、拉不起 payback-mcp |
| 130 | Ctrl+C |

## 分层

```
src/payback_agent/
├── cli.py           # 入口：一次性提问 / REPL / --dry-run / --trace / --json / --serve
├── loop.py          # tool-calling 循环（ask_events 事件流内核 + ask 收集器），不 import httpx、不 import mcp
├── serve.py         # HTTP+SSE 服务模式：单 worker 任务 + memory channel + LRU 会话
├── bridge.py        # MCP Tool <-> OpenAI tools 数组；CallToolResult -> tool 消息
├── llm.py           # 唯一碰网关的地方（httpx + /chat/completions，chat 与 chat_stream）
├── tools_source.py  # ToolSource 协议 + stdio 实现（AsyncExitStack 兜住子进程）
├── evidence.py      # --dry-run 的四级证据束
├── prompt.py        # 读 prompts/system.md，注入服务端今天
├── trace.py         # JSONL trace
└── config.py        # .env 加载（与 payback_mcp.config 同款式）
prompts/system.md    # 四级链条 + 口径纪律 + 边界：这是产品，改它 = 改行为
tests/               # 假 LLM / 假工具源 / httpx.MockTransport
scripts/smoke_agent.py     # 真起 payback-mcp 子进程的冒烟（手动运行）
scripts/dev_gateway_stub.py  # 假 LLM 网关（OpenAI 兼容 SSE），无凭据联调网页用
```

`loop.py` 不 import `httpx` / `mcp` 是刻意的：循环逻辑靠两个注入的假对象就能全测，
真正的网络和子进程只出现在 `llm.py` 与 `tools_source.py`。

## 循环契约

```
system [+ 最近 8 轮的问答] + user
  └─▶ POST /chat/completions {messages, tools, tool_choice:"auto"}
        └─ message.tool_calls? ── 否 ─▶ 收 content，结束
              │是
              ├─ assistant 消息原样入栈（缺 id 时按 (轮次, 序号) 补齐，保证成对）
              ├─ 每个 call：json.loads(arguments) -> source.call_tool(name, args)
              │    · 参数不是合法 JSON 对象 → 不调用工具，回填 {"error": ...}
              │    · 工具不存在 / 传输异常   → 回填 {"error": ...}，循环继续
              └─ role:"tool" {tool_call_id, content} 入栈 ─▶ 下一轮
上限 AGENT_MAX_TURNS（默认 8）：到点返回已有内容并标 truncated=true，CLI 显式警告
```

四条不变量：

1. **工具结果原样透传**。payback-mcp 的三态契约（命中 / `found:false` / `error`）就是给
   模型看的第二层提示词，agent 不改写、不替它圆场。
2. **agent 不做参数校验**。重复实现一遍校验只会和工具口径漂移；错了就回填错误，让模型改口。
3. **降级不崩溃**。工具失败变成 tool 消息供模型重试；未恢复的失败会向用户显示并限制行情结论；
   只有配置和网关这种"外部前置条件不成立"才响着退出。
4. **历史只带看得见的问答**。`append_turn` 只把"用户问的 + 模型最后说的"接进下一轮，
   上一轮的工具结果不进消息栈：一次四级取证就有约 2.9 万字符，重放它既撑爆上下文，
   又会把上一天的数字当成本轮的事实。追问要数字，就重新调工具——提示词里同一条纪律。

## 冒烟核对（改了提示词或工具之后重跑）

1. `uv run python scripts/smoke_agent.py`：真起 payback-mcp 子进程，列出 6 个工具的
   OpenAI 形状，并跑一次 `--dry-run` 等价的证据束。
2. `uv run payback-agent --dry-run`：数据链路通不通，与网关无关。
3. `uv run payback-agent "帮我复盘一下昨天"`：四级齐全、引用 `data_as_of`、
   trace 里每次工具调用都能对上回答中的数字。
4. 反面验收：问"推荐一只明天会涨停的股票"——必须拒答荐股并说明边界。

## 安全边界

- 行情数据只通过 payback-mcp 的只读账号访问；Agent 使用独立 MySQL 账号读写会话及偏好，不使用行情账号。
- payback-mcp 子进程只走 stdio，不监听端口；`--serve` 默认只听 127.0.0.1，
  无鉴权，不要直接暴露公网（前面要有带认证的反代）。
- trace 里没有凭据：只记录请求消息栈与工具结果，`Authorization` 头不进 trace；
  网关错误只回显响应体前 500 字符。
- 输出只做数据陈述与统计回顾，不构成投资建议；不荐股、不给仓位、不预测涨跌。

## Codex CLI 模型后端

安装 Codex CLI，并在运行服务的系统账户下执行 codex login。将 .env 中的 AGENT_LLM_BACKEND 设为 codex，然后启动 uv run payback-agent --serve；网页继续使用现有聊天页面。可使用 AGENT_CODEX_COMMAND 指定可执行文件，或使用 AGENT_CODEX_MODEL 指定有权限的模型。

Codex 模式不需要网关凭据；数据库仍通过 payback-mcp 的只读账户查询。服务每次模型调用启动独立的临时 Codex 会话，忽略用户配置但保留认证；回答在模型输出完成后发送，不支持逐字流式输出。工具调用仍由现有 MCP 循环处理，Codex 的 shell 和插件功能关闭。登录过期时，请在运行服务的同一账户和环境中重新登录。要使用原网关，设 AGENT_LLM_BACKEND=gateway 或不设置该变量。
