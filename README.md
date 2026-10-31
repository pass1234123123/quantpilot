# QuantPilot AI — 外汇 EA 云端平台（开发中）

EA(MT5) → 本地 Agent → NATS JetStream → 云端 FastAPI → PostgreSQL 的完整数据链路。

**OBSERVE_ONLY**：EA 不含任何交易执行代码（无 OrderSend / OrderCheck / CTrade），
仅上报终端/账户身份、心跳快照、品种规格、持仓/挂单/成交、日历事件与已收盘 K 线
（M15/H1 基线，PRD 22.2）。新账户默认 `OBSERVE_ONLY` 状态。云端命令通道已通
（下发/确认/幂等/对账），但 EA 收到后仅回执（`OBSERVE_ONLY_ACK`），不执行任何
交易；风险增加型命令在下发前过云端风控闸门。云端策略引擎消费收盘 bar 产生**建议
性信号**（PRD 7.1：策略不直接下单）；显式开启账户级自动生成后，信号可经聚合、
Decimal 风险预算和同一风控闸门生成持久命令，但 EA 仍只回执、不执行。

## 已完成切片

- 只读接入：注册/心跳/规格/持仓/挂单/成交/日历事件落库 + 查询 API + Web 仪表盘
- 品种报价：每账户每品种最新 bid/ask/点差投影（`symbol_quotes`）
- 云端配置：`risk_profiles` 配置下发（EA 侧仅日志，不解析执行）
- 命令通道：`commands` 状态机（QUEUED→DELIVERED→ACKED / EXPIRED / UNKNOWN / REJECTED）、
  command_id 幂等、UNKNOWN 对账（迟到 ack / EA recently-acked 回显，永不盲重发）
- 风控引擎（执行侧）：创建时闸门按 PRD 9.2 顺序检查；拒绝即落终态 REJECTED（永不投递）
  + `risk_decisions` 审计；全局/账户级 STOP_NEW_RISK / LOCKED 持久状态（PRD 3.3/9.5）
- 策略引擎（PRD 7）：EA v1.5.0 按周期上报已收盘 bar（`symbol_bars` 投影，回填/增量/
  重同步幂等）→ 无状态窗口重放评估 → `strategy_signals`（边沿触发，UNIQUE 去重，
  doc 7.5）；首批 3 策略 ma_cross / rsi_momentum / trend_following；坏 bar 跳过并
  发 `ea.bars_dropped` 防重投死循环；实例绑定固定版本 + config_revision 只增（doc 7.6）
- AI 分析引擎（PRD 8）：统计特征版 Market Regime（ATR/波动率比、Wilder ADX、
  EMA20/50 → TREND_UP/TREND_DOWN/RANGE/HIGH_VOL/UNKNOWN）+ Strategy Evaluator
  适用评分；`ai_analyses` 落库严格 doc 8.4 结构；input_hash 幂等（重投不重复）；
  首版仅 OFF / ADVISORY 模式（8.5），分析仅展示、永不改变交易许可（8.1）
- 策略自动化闭环（PRD 3.2/9.3）：同 source bar 同向信号聚合为一条命令，最强信号
  提供交易价位；反向信号记冲突且不建单；Decimal 风险预算按 volume_step 向下取整；
  确定性 command_id 保证 JetStream 重放幂等；AI 仅作为风险审计上下文，不参与许可决策
- 历史回测（PRD 15）：复用实时策略实现与指标窗口，从 `symbol_bars` 读取历史数据；
  当前 bar 收盘生成信号、下一根 bar 开盘全量成交；固定策略版本/config_revision、
  数据 hash 与成本模型，持久化幂等任务和完整指标报告，不创建任何交易命令

## 目录结构

```
packages/contracts/   Pydantic 数据契约 + NATS subject + 共享常量
agent/                本地节点：终端发现 + HTTP 接收(8787) + JetStream 发布
services/api/         FastAPI：JetStream 消费入库 + 查询路由 + 命令通道 + 风控闸门
                     （signal_pipeline.py 聚合 / command_service.py 共用命令核心）
frontend/             Web 仪表盘（Vue3 + Element Plus + Pinia + Lightweight Charts）
mt5/                  MQL5 源码（Experts/QuantPilotEA + Include/QuantPilot）
infra/postgres/       Alembic 迁移（0001–0012，含 risk / 策略 / AI / 回测表）
scripts/              开发工具（隧道/冒烟/验收/部署）
```

## 快速启动（Windows）

前置：Python 3.12+；服务器上已就绪 PostgreSQL 16 与 NATS（JetStream）。

```powershell
python -m venv .venv
.venv\Scripts\pip install -e packages\contracts -e agent -e services\api `
    fastapi uvicorn nats-py "psycopg[binary,pool]" httpx alembic paramiko

copy .env.example .env      # 填入真实 QP_* 配置（.env 不入 Git）
```

按顺序启动（各自独立终端，或后台运行）：

```powershell
.venv\Scripts\python scripts\dev_pg_tunnel.py   # SSH 隧道 127.0.0.1:54322 -> 服务器 PG
.venv\Scripts\python -m quantpilot_api          # FastAPI  127.0.0.1:8000
.venv\Scripts\python -m quantpilot_agent         # Agent    127.0.0.1:8787
```

冒烟与验收：

```powershell
.venv\Scripts\python -m unittest discover -s services/api/tests -v  # 全部单元测试（含回测成交/成本/幂等）
.venv\Scripts\python scripts\sim_ea.py       # 合成 EA 事件 + 自动化/AI/回测及重放场景
.venv\Scripts\python scripts\e2e_verify.py   # 全链路验收：API + 25 张表 + 风控/信号/自动化/回测不变量
```

## Web 仪表盘（frontend/）

Vue3 + TypeScript + Vite + Element Plus + Pinia + TradingView Lightweight Charts。
深色交易终端风格（`docs/ui-reference.png`），通过 Vite dev proxy 访问 API（无 CORS 配置）。

```powershell
cd frontend
pnpm install
pnpm dev        # http://127.0.0.1:5173
pnpm build      # 类型检查 + 产线构建
```

- **实时链路面板**：KPI 带（余额/净值/浮动盈亏/保证金等 ← 最新快照）、持仓明细
  （EA 实时同步，`GET /api/v1/accounts/{id}/positions`，每 10s 随心跳刷新）、
  Equity & Balance Graph（快照历史）、Market Watch（品种发现 + 实时点差）、
  Performance（成交统计/日盈亏）、Risk Management（`risk_profiles` 配置 + 停止状态
  + `risk_decisions` 决定审计）、News Filter（日历事件）、Permission Signal /
  Trading Status / Account Info、侧栏 System Status；每 30s 自动刷新。
- **策略与命令面板**：策略引擎（策略目录 + 账户实例绑定/启停/参数修订，实例启停调
  `POST .../strategy-instances/{iid}/status`）、交易信号（`strategy_signals` 边沿触发
  流，方向/强度/参考价/SL/TP/触发原因）、命令通道（`commands` 状态机流 +
  `risk_decisions` 审计，REJECTED 永不投递）、回测研究（任务列表 + 点击行内联加载
  `GET /api/v1/backtests/{id}` 完整指标报告）；信号为建议性（PRD 7.1），EA 保持
  OBSERVE_ONLY。
- **配置面板**：Profit Monitor、Filtering / Expert Settings（`risk_profiles.config_json`
  filters/expert 段，云端版本号展示 + PUT 保存）；OBSERVE_ONLY 下仅存储与展示。

## 命令通道与风控 API（本阶段）

- `POST /api/v1/commands`：创建命令（command_id 幂等；OPEN_MARKET / PLACE_PENDING
  先过云端风控闸门，REJECT 直接落终态 `REJECTED`，响应附 `risk_decision`）
- `GET /api/v1/commands/{id}`：查询状态（QUEUED / DELIVERED / ACKED / EXPIRED /
  UNKNOWN / REJECTED）
- `GET|PUT /api/v1/accounts/{id}/config`：配置读取/更新（filters / expert / risk / ai /
  automation 五段，版本号单调递增，PUT 即全量替换）
- `GET /api/v1/risk/state`、`POST /api/v1/risk/stop`、`POST /api/v1/risk/resume`：
  全局/账户级停止状态（LOCKED 恢复需 `confirm_locked=true`，PRD 9.5）
- `GET /api/v1/risk/decisions`：闸门决定审计（account_id / command_id 过滤）

风控闸门检查顺序遵循 PRD 9.2（停止状态 → 快照新鲜度/权限 → 日亏/回撤/保证金/连亏
→ 仓位数/周末/日单数 → 规格/手数 → 强制止损/单笔风险估算）；无法可靠估算风险时拒绝
（PRD 9.3）。点差/即时报价类检查留给 EA 执行级（PRD 9.1 双层结构）。

## 策略引擎 API（本阶段）

- `GET /api/v1/strategies`：策略目录（code / 默认参数 / warmup 门槛）
- `GET|POST /api/v1/accounts/{id}/strategy-instances`：实例列表 / 绑定
  （账户+策略+品种+周期唯一，绑定固定 `strategy_version`）
- `GET /api/v1/accounts/{id}/strategy-instances/{iid}`：实例详情
- `POST /api/v1/accounts/{id}/strategy-instances/{iid}/status`：ENABLED / DISABLED
- `PUT /api/v1/accounts/{id}/strategy-instances/{iid}/config`：参数覆盖
  （`config_revision` 单调递增，历史版本不覆盖，doc 7.6）
- `GET /api/v1/accounts/{id}/signals`：信号查询（strategy_code / timeframe 过滤，
  返回 doc 7.4 标准结构）
- `GET /api/v1/accounts/{id}/bars`：收盘 bar 查询（oldest-first）

评估语义（PRD 7）：仅批次最新收盘 bar 触发；引擎无状态——从 `symbol_bars` 重放
指标窗口（warmup 不足跳过）；边沿触发 + `UNIQUE(instance, source_bar_time)` 去重
（doc 7.5），JetStream 重投不产生重复信号；单实例评估失败仅记 system event，不阻塞
bar 投影；毒 bar（OHLC 越界）跳过并发 `ea.bars_dropped` 防无限重投。策略引擎本身
不直接下单（PRD 7.1）；只有账户显式设置 `automation.command_generation=true` 且
`risk.per_trade_risk_fraction` 非零时，自动化 pipeline 才创建风险预算命令。

EA 侧 v1.5.0 新增输入：`BarTimeframes`（默认 `M15,H1`，PRD 22.2）、
`BarHistoryCount`（默认 500，首次上线回填历史 bar 数）。

## AI 分析引擎（本阶段）

- `GET /api/v1/accounts/{id}/ai/analyses`：分析查询（symbol / timeframe 过滤，
  doc 8.4 结构在 `output_json`，最新在前）
- `POST /api/v1/accounts/{id}/ai/analyze`：在最新收盘 bar 上手动触发一次分析
  （幂等：同特征快照返回已有行；账户 ai.mode=OFF 时返回 `ai_mode=OFF` 不执行）

首版 provider 为确定性统计分类器 `statistical-regime/regime-rules-v1`（无 LLM 调用，
成本记 0，doc 8.6 治理字段齐全：provider/model/prompt_version/input_hash/耗时/成本）。
触发时机与策略评估相同——收盘 bar 落库后（doc 8.6：LLM 不按 tick 调用；首版无 LLM
但保持同一节流节奏）。账户配置 `ai.mode`（`risk_profiles.config_json` 第四段，默认
`ADVISORY`，doc 8.5）：`OFF` = 完全不分析（AI 故障回退位），`ADVISORY` = 仅建议与审计。
分析失败仅记 system event（`ai.analysis_failed`），不阻塞 bar 投影。FILTER /
REGIME_ROUTING 模式待验证后启用。自动命令创建时，AI 的 analysis_id、regime、
signal_decision 等仅写入 `risk_decisions.risk_snapshot.ai` 供审计，绝不改变 ALLOW/REJECT。

### 策略自动化闭环

收盘 bar 落库后依次执行：策略评估 → AI ADVISORY 分析 → 同 bar 信号聚合 → Decimal
风险预算 sizing → 共用命令创建核心 → 云端风控闸门 → durable command。HTTP 与自动化
入口共用 `command_service`，TTL、幂等、风险拒绝和审计语义一致。

- 自动生成默认 OFF；开关与非零风险预算必须同时满足，sizing 失败记
  `signal.command_failed`，不创建命令。
- 同向多信号只建一单，强度最高者为 primary（signal_id 确定性打破平局），全部
  signal_id 写入风险快照；同 bar 反向信号记 `signal.conflict`，不建单。
- command_id 由账户/品种/周期/source bar/direction 确定性生成，重放返回原命令；
  REJECT 以 `REJECTED` 持久化且永不投递。EA 始终保持 OBSERVE_ONLY。

## 历史回测 API（本阶段）

- `POST /api/v1/backtests`：创建并同步执行回测；必须提供 UUID `Idempotency-Key` 请求头，
  body 包含 `strategy_instance_id`、带时区的 `range_start/range_end`、`initial_balance`、
  `volume` 和 `cost_model`。相同键与请求体重放直接返回原报告；相同键不同请求体返回 409。
- `GET /api/v1/backtests`：任务摘要列表（最新在前；`account_id` /
  `strategy_instance_id` / `limit` 过滤，上限 500）。
- `GET /api/v1/backtests/{id}`：查询任务状态、固定策略/参数快照、数据 hash、指标、
  交易明细、成本拆分与权益曲线。

回测复用实时策略类和相同 warmup 窗口，仅使用区间内的已收盘 bar。信号在当前 bar 收盘
产生，下一根 bar 开盘全量成交；单持仓且持仓期间忽略新入场，SL/TP 同根触发时保守地
止损优先，末尾按最后收盘价强制平仓。成本模型分项记录 spread、slippage、commission、
swap，全部资金计算使用 Decimal。报告包含净利润、最大回撤、Profit Factor、胜率、
Expectancy、Sharpe、Sortino、Recovery Factor、Exposure、持仓时间与交易数。

回测任务持久化于 `backtest_jobs/backtest_results`，记录策略版本、实例 config_revision、
请求 hash 和精确历史数据 hash。该路径不调用命令服务，不进入 Agent/EA 执行链。

数据库变更（如换库）：

```powershell
cd infra\postgres
..\..\.venv\Scripts\python -m alembic upgrade head
```

## 真实 EA 挂载（人工验收）

1. MT5 终端：工具 → 选项 → 智能交易 → 勾选"允许 WebRequest"，白名单添加
   `http://127.0.0.1:8787`（仅 loopback，Agent 拒绝非本地回环地址）。
2. `scripts/deploy_ea.py` 已将编译好的 `QuantPilotBridge.ex5` 部署到终端
   `MQL5\Experts\QuantPilotEA\`（0 errors 0 warnings）。
3. 在 MT5 中刷新导航器，将 QuantPilotBridge 拖到任意图表。
4. 输入参数 `AgentToken` 填 `.env` 里的 `QP_AGENT_TOKEN`（其余默认即可）。
5. 验证：`python scripts\e2e_verify.py`；或浏览器访问
   `http://127.0.0.1:8000/api/v1/accounts`，`status` 应为 `OBSERVE_ONLY`。

## 安全约定

- 密钥只放 `.env`（已被 .gitignore 排除）；服务器凭证目录 `.server/` 不入 Git。
- Agent 发布前将终端原始路径哈希化，云端只见 sha256（隐私，PRD 6.3）。
- 金额精度链路：JSON decimal 字符串 → PG NUMERIC → Python Decimal（PRD 13.1/21.3）。
- JetStream `Nats-Msg-Id = event_id` 服务端去重 + 消费端幂等 upsert，at-least-once 不产生重复数据。
