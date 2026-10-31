# QuantPilot AI — 外汇 EA 云端平台（开发中）

EA(MT5) → 本地 Agent → NATS JetStream → 云端 FastAPI → PostgreSQL 的完整数据链路。

**OBSERVE_ONLY**：EA 不含任何交易执行代码（无 OrderSend / OrderCheck / CTrade），
仅上报终端/账户身份、心跳快照、品种规格、持仓/挂单/成交、日历事件与已收盘 K 线
（M15/H1 基线，PRD 22.2）。新账户默认 `OBSERVE_ONLY` 状态。云端命令通道已通
（下发/确认/幂等/对账），但 EA 收到后仅回执（`OBSERVE_ONLY_ACK`），不执行任何
交易；风险增加型命令在下发前过云端风控闸门。云端策略引擎消费收盘 bar 产生**建议
性信号**（PRD 7.1：策略不直接下单），信号→命令接线在后续切片实现。

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

## 目录结构

```
packages/contracts/   Pydantic 数据契约 + NATS subject + 共享常量
agent/                本地节点：终端发现 + HTTP 接收(8787) + JetStream 发布
services/api/         FastAPI：JetStream 消费入库 + 查询路由 + 命令通道 + 风控闸门
                     （risk.py 闸门 / account_config.py 账户配置模型）
frontend/             Web 仪表盘（Vue3 + Element Plus + Pinia + Lightweight Charts）
mt5/                  MQL5 源码（Experts/QuantPilotEA + Include/QuantPilot）
infra/postgres/       Alembic 迁移（0001–0010，含 risk / 策略引擎表）
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
.venv\Scripts\python scripts\sim_ea.py       # 合成 EA 事件（注册/心跳/规格/持仓/命令/风控闸门/策略实例/收盘bar/信号）
.venv\Scripts\python scripts\e2e_verify.py   # 全链路验收：API 端点 + 21 张表落库 + 风控/信号不变量
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

- **真实数据面板**：KPI 带（余额/净值/浮动盈亏/保证金等 ← 最新快照）、持仓明细
  （EA 实时同步，`GET /api/v1/accounts/{id}/positions`，每 10s 随心跳刷新）、
  Equity & Balance Graph（快照历史）、Market Watch（品种发现 + 实时点差）、
  Performance（成交统计/日盈亏）、Risk Management（`risk_profiles` 配置 + 停止状态）、
  News Filter（日历事件）、Permission Signal / Trading Status / Account Info、
  侧栏 System Status；每 30s 自动刷新。
- **示例数据面板**：Profit Monitor、Filtering / Expert Settings 等——对应数据链路
  （策略引擎/命令下发 UI）在后续切片实现。

## 命令通道与风控 API（本阶段）

- `POST /api/v1/commands`：创建命令（command_id 幂等；OPEN_MARKET / PLACE_PENDING
  先过云端风控闸门，REJECT 直接落终态 `REJECTED`，响应附 `risk_decision`）
- `GET /api/v1/commands/{id}`：查询状态（QUEUED / DELIVERED / ACKED / EXPIRED /
  UNKNOWN / REJECTED）
- `GET|PUT /api/v1/accounts/{id}/config`：配置读取/更新（filters / expert / risk 三段，
  版本号单调递增，PUT 即全量替换）
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
bar 投影；毒 bar（OHLC 越界）跳过并发 `ea.bars_dropped` 防无限重投。**信号仅建议，
永不直接下单**（PRD 7.1）；信号→聚合→风控→命令接线在后续切片。

EA 侧 v1.5.0 新增输入：`BarTimeframes`（默认 `M15,H1`，PRD 22.2）、
`BarHistoryCount`（默认 500，首次上线回填历史 bar 数）。

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
