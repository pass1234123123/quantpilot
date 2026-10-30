# QuantPilot AI — 首切片（只读接入）

EA(MT5) → 本地 Agent → NATS JetStream → 云端 FastAPI → PostgreSQL 的只读数据链路。

**OBSERVE_ONLY**：本切片 EA 不含任何交易执行代码（无 OrderSend / OrderCheck / CTrade），
仅上报终端/账户身份、心跳快照与品种规格。新账户默认 `OBSERVE_ONLY` 状态。

## 目录结构

```
packages/contracts/   Pydantic 数据契约 + NATS subject + 共享常量
agent/                本地节点：终端发现 + HTTP 接收(8787) + JetStream 发布
services/api/         FastAPI：节点注册/心跳 + JetStream 消费者入库 + 查询路由
frontend/             Web 仪表盘（Vue3 + Element Plus + Pinia + Lightweight Charts）
mt5/                  MQL5 源码（Experts/QuantPilotEA + Include/QuantPilot）
infra/postgres/       Alembic 迁移（0001 建表 + 0002 broker_symbols.selected）
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
.venv\Scripts\python scripts\sim_ea.py       # 合成 EA 事件（401/注册/心跳/规格/命令）
.venv\Scripts\python scripts\e2e_verify.py   # 全链路验收：API 六端点 + 8 张表落库
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

- **真实数据面板**：KPI 带（余额/净值/保证金等 ← 最新快照）、Equity & Balance Graph
  （快照历史，`GET /api/v1/accounts/{id}/snapshots`）、Market Watch（品种发现）、
  Permission Signal / Trading Status / Account Info、侧栏 System Status；每 30s 自动刷新。
- **示例数据面板**：Trade Journal、Performance、Risk Management、Profit Monitor、
  Filtering / Expert Settings、News Filter 等标注"示例数据"——对应数据链路（成交/风控/命令通道）
  在后续切片实现。

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
