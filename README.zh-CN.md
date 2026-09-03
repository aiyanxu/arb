# entropy-arb

**[English documentation / 英文文档 → README.md](README.md)**

开源双交易所永续合约套利机器人。两条腿均可配置——任意一条腿都可以是以下
五个交易所之一（两腿不能相同）；**base** 腿是溢价的分子，**hedge** 腿是分母
（`base_venue` 缺省为 `entropy`）：

| venue | 交易所 | 计价货币 | 吃单费 | 协议 |
|---|---|---|---|---|
| `entropy` | Hyperliquid 上的 Entropy | USDC | 0 bps | HL l2Book（dex `io`） |
| `lighter` | Lighter 主网 | USDC | 0 bps | zkLighter ws（增量订单簿，异步结算） |
| `lighter-rh` | Lighter Robinhood 链 | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book，IOC 同步结算 |
| `aster` | Aster DEX V3 合约 | USDT | ~4.5 bps | Aster fapi ws（top-20 快照），IOC 同步结算 |

> **推荐链接** —— 通过以下链接注册即可支持本项目：
> - Entropy — Tier 4 推荐，100% 返佣：<https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood 链：<https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz（Hyperliquid）：<https://app.hyperliquid.xyz/join/QUANTGUY>

当同一品种在一边贵、另一边便宜时，机器人同时在贵的一边卖出、便宜的一边买入
（均为吃单），持有 delta 中性仓位，等溢价回归后反向平仓。所有交易决策使用的
价格都来自**将要实际成交的那个交易所的真实订单簿**——Hyperliquid 的盘口来自
官方 websocket（`wss://api.hyperliquid.xyz/ws`），Lighter 的盘口来自 Lighter
官方 websocket。

机器人运行期间（即使没有密钥、没有开策略）会自动把两边盘口记录成**分钟级
DuckDB 数据**，配套的分析工具可以直接把这些数据变成策略所需的三个核心参数。

## 信号逻辑

整个信号就是 `config.yaml` 里三个数字，由你根据采集的数据自己设定——
**仅对当前 (base, hedge) 组合有效**：更换任一腿后数字即失效，需重新采集分析：

```
premium_bps =（base 腿价格 / hedge 腿价格 − 1）× 10 000

                          ┌──────────────  卖出 base + 买入 hedge
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   溢价的长期中枢
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  买入 base + 卖出 hedge
```

- `midline_bps` —— 溢价的常态水平。跨所溢价几乎从不以零为中心（预言机不同、
  计价货币不同、新上市溢价等），零中心的带只会朝一个方向开仓、打满仓位上限、
  永远无法平仓。请实际测量溢价所在的位置，然后填入。
- `upper_bps` / `lower_bps` —— 中枢上下两侧的入场带宽。

两个方向的门槛都作用于**可实际成交的价格**（base 买一 对 hedge 卖一，
反之亦然），并且是**扣除双边吃单手续费之后的净门槛**——引擎会在阈值之上
另行叠加手续费。因此一次完整往返扣费后**净赚 ≥ upper + lower bps**，这是
结构上保证的。

有一点必须理解：当 `midline_bps: 5` 时，买入 base 腿的门槛是
`lower − midline`，可能为**负数**。这是有意为之——如果 base 腿长期贵 5 bps，
那么在溢价为 0 时买入它，相对其自身均衡水平就是便宜了 5 bps，这笔交易正是
此前在 `midline + upper` 处卖出的获利平仓。这同时意味着**中枢填错就是亏钱
策略**：若真实溢价中枢是 0 而你填了 5，机器人会整天以公允价买入 base 腿。
先测量、再交易——数据采集器和分析工具就是为此而生。

## 快速开始

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -e .          # 数据采集只需要这些

cp config.example.yaml config.yaml       # 策略配置（阈值、规模、风控）
cp .env.example .env                     # 密钥——交易必填
```

交易市场现在就在 `config.yaml` 里：`symbol`（两个交易所共同交易的品种）、
`base_venue` 与 `hedge_venue`（各四选一：`entropy`、`lighter`、`lighter-rh`、
`tradexyz`；两腿不能相同；`base_venue` 缺省为 `entropy`）。单次启动可用
`--symbol` / `--base` / `--hedge` 覆盖配置文件。

本机器人**没有模拟盘**——要么采集数据（`--record-only`），要么实盘交易。
请用采集的数据和最小的仓位上限来验证策略，而不是模拟成交。

**第一步：先采集数据**（不需要任何密钥）：

```bash
entropy-arb --record-only                  # 交易市场来自 config.yaml
entropy-arb --record-only --symbol SNDK --base entropy --hedge lighter-rh
entropy-arb --record-only --symbol SNDK --base lighter --hedge entropy
```

至少运行几个小时（最好一整天——溢价存在日内规律），数据写入
`logs/minutes.duckdb`（DuckDB 数据库，每个 (symbol, base, hedge) 组合一张表）。

**第二步：分析数据、设定阈值：**

```bash
python3 tools/analyze.py
```

它会输出溢价分布、各档带宽的历史触发频率，以及可直接粘贴进
`config.yaml` 的 `thresholds:` 配置块。

**第三步：实盘** —— 填写 `.env`，安装签名 SDK，仓位上限从刚好满足
交易所最小名义的水平开始：

```bash
pip install -e ".[live]"
entropy-arb          # 或带覆盖参数：--symbol SNDK --base entropy --hedge lighter-rh
```

不带 `--record-only` 运行时，只要两边行情就绪且溢价越过带宽，就会立即
发送真实订单。

**仪表盘。** 在终端运行时会显示实时 Rich 仪表盘：两边盘口（含数据龄/点差）、
持仓与上限、账户权益与本次会话盈亏、两个方向的可成交溢价对比完整门槛
（已含手续费与库存加价，● 表示已武装）、数据采集进度、最近成交，以及日志
尾部（完整日志写入 `logging.file`，默认 `logs/engine.log`）。`--record-only`
模式同样可用。加 `--cn` 参数可使仪表盘全部以中文显示。`--no-dashboard`
可切换为纯日志输出（nohup/systemd 等非终端环境会自动退回纯日志），也可
设置 `logging.dashboard: false`。

## Docker 部署

镜像内不含任何密钥与配置——`config.yaml`、`symbol_map.yaml`、`.env` 都在
运行时挂载（已由 `.dockerignore` 排除）。

构建两种镜像：

```bash
docker build --target record-only -t entropy-arb:record-only .   # 仅基础依赖
docker build --target live       -t entropy-arb:live .           # + 签名 SDK
```

仅采集数据（不需要密钥）：

```bash
mkdir -p logs
docker run --rm \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./symbol_map.yaml:/app/symbol_map.yaml:ro \
  -v ./logs:/app/logs \
  entropy-arb:record-only
```

实盘交易（无终端仪表盘、崩溃自动重启）：

```bash
docker run -d --name entropy-arb \
  --restart unless-stopped --init --stop-grace-period 30s \
  --env-file .env \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./symbol_map.yaml:/app/symbol_map.yaml:ro \
  -v ./logs:/app/logs \
  entropy-arb:live --no-dashboard
```

或使用 docker compose（推荐——见 `docker-compose.yml`）：

```bash
cp config.example.yaml config.yaml && cp .env.example .env   # 两个都填好
docker compose up -d --build                    # 实盘
docker compose logs -f                          # 控制台日志
docker compose --profile record up -d --build   # 改为仅采集数据
docker compose down                             # SIGTERM -> 优雅停机
```

说明：

- 采集的数据落在宿主机 `./logs`（`minutes.duckdb`、`trades.csv`）。
  `--no-dashboard` 模式**没有** `logs/engine.log`——控制台输出直接进
  `docker logs` / `docker compose logs`。
- 不暴露任何端口（机器人只主动外连）。镜像默认命令是安全的
  `--record-only --no-dashboard`；实盘必须显式用 `command:` / 参数覆盖。
- Rich 仪表盘无法在 `docker logs` 中渲染，请用 `docker compose logs -f`，
  或在 Docker 外运行以使用仪表盘。
- 改动代码后需重新构建镜像。实盘部署建议按 `pyproject.toml` 中的注释
  固定 lighter-sdk 的 commit。
- Linux 宿主机上 `./logs` 需对 uid 1000 可写——在 compose 服务中加
  `user: "1000:1000"`（Docker Desktop 会自动映射）。
- 在容器内分析采集数据：

```bash
docker run --rm --entrypoint python -v ./logs:/app/logs \
  entropy-arb:record-only tools/analyze.py
```

## 数据采集与分析

采集器在所有模式下自动运行（`recorder.enabled: true`）：每秒采样一次两边
的真实盘口，每分钟写一行到 DuckDB 数据库（默认 `logs/minutes.duckdb`，
配置键 `recorder.db`）。每个 (symbol, base_venue, hedge_venue) 组合独立
一张表，表名 `minutes_<symbol>__<base>__<hedge>`（各段转小写、
`[a-z0-9_]` 之外的字符替换为 `_`，如 SNDK 的 entropy×lighter-rh 组合 →
`minutes_sndk__entropy__lighter_rh`）；多个组合共用同一个库文件时数据
完全隔离。所有分表共用下面的列结构：

| 列 | 含义 |
|---|---|
| `minute_ts`, `time_utc` | 分钟起点（epoch 秒 / ISO UTC） |
| `symbol`, `base_venue`, `hedge_venue` | 该行所属的组合（主键的一部分） |
| `base_bid/ask`, `hedge_bid/ask` | 该分钟最后一次有效盘口 |
| `premium_open/high/low/close/mean/std_bps` | base 腿相对 hedge 腿的中间价溢价 |
| `sell_edge_mean/max_bps` | 卖出 base 方向的可成交溢价（base 买一 / hedge 卖一 − 1） |
| `buy_edge_mean/max_bps` | 买入 base 方向的可成交溢价（hedge 买一 / base 卖一 − 1） |
| `samples` | 该分钟约 60 秒中两边盘口同时有效的秒数 |

采集的 edge 为费前口径；分析工具在统计触发频率前会先扣除 `--fees-bps`
（请传入**两边吃单费之和**——零费交易所默认 0.0，腿为 `tradexyz` 时
约为 1.0），因此其表格与建议值可直接填入配置。`--hours 24`
可只分析最近数据；溢价中枢会漂移，请定期重新分析并更新 `config.yaml`。
溢价是组合相对的——任一腿换了 venue 都要重新测量。

### 查询数据

数据库就是一个普通 DuckDB 文件，可直接查询：

```bash
duckdb logs/minutes.duckdb   # 或: python3 -m duckdb logs/minutes.duckdb
```

```sql
SELECT minute_ts, premium_close_bps, sell_edge_max_bps
FROM minutes_sndk__entropy__lighter_rh
WHERE symbol = 'SNDK' AND base_venue = 'entropy'
  AND hedge_venue = 'lighter-rh'
ORDER BY minute_ts DESC LIMIT 20;
```

行以 `(symbol, base_venue, hedge_venue, minute_ts)` 为主键、用
`INSERT OR REPLACE` 写入，重启不会产生重复分钟。采集器在两次分钟写入之间
会释放数据库，因此机器人运行时也可以直接查询文件。

按组合分表改造之前采集的数据仍在旧结构表中（共享 `minutes` 表与
`minutes_<symbol>` 表），分析工具不会读取它们。请一次性迁移（先停止
机器人）——迁移行会全部盖上 `base_venue='entropy'` 章（历史上的 base 腿）：

```bash
python3 tools/migrate_per_symbol.py --db logs/minutes.duckdb [--drop-old]
```

脚本可重复执行；默认保留旧表，传入 `--drop-old` 可在迁移成功后删除。

旧 CSV（`logs/minutes.csv`）迁移：行里没有 venue 列，请按当时运行参数填入：

```sql
-- duckdb logs/minutes.duckdb，旧 CSV 放在原处
INSERT OR REPLACE INTO minutes_sndk__entropy__lighter_rh
SELECT minute_ts, time_utc, 'SNDK', 'entropy', 'lighter-rh',
       * EXCLUDE (minute_ts, time_utc)
FROM read_csv('logs/minutes.csv');
```

## 配置说明

策略与交易市场都在 `config.yaml`（严格校验——未知键名直接报错），密钥在
`.env`。完整的双语注释参考：[config.example.yaml](config.example.yaml)。
核心项：

| 键 | 含义 | 默认值 |
|---|---|---|
| `symbol` | 两条腿共同交易的品种 | — |
| `base_venue` | base 腿（溢价分子）：`entropy` / `lighter` / `lighter-rh` / `tradexyz` | `entropy` |
| `hedge_venue` | 对冲腿（溢价分母）：任意 ≠ `base_venue` 的 venue | — |
| `thresholds.midline_bps` | 溢价中枢（针对当前组合，必须实测！） | — |
| `thresholds.upper_bps` / `lower_bps` | 入场带宽（> 0） | — |
| `base.dex` / `hedge.dex` | Hyperliquid 系腿的 dex 名 | `io` / `xyz` |
| `*.taker_fee_bps` | 各腿吃单费（不得低于该 venue 默认值） | 按 venue |
| `*.max_position_usd` | 各腿持仓上限 | 1000 |
| `*.max_orders_per_min` | 各腿每分钟下单预算（滑动 60 秒） | 按 venue（120；Lighter 30） |
| `sizing.take_fraction` | 吃掉可套利深度的比例 | 0.5 |
| `sizing.max_order_notional_usd` | 单笔名义上限 | 500 |
| `inventory.scale_bps` / `floor_frac` | 库存阶梯（仓位超过上限的 `floor_frac` 后额外加价） | 10 / 0.5 |
| `execution.premium_persist_sec` | 信号需持续多久才触发 | 0.3 |
| `execution.*` | 滑点保护、超时、对账周期等 | 见配置文件 |
| `recorder.*` | 分钟数据采集器 | 开启，`logs/minutes.duckdb` |
| `logging.dashboard` / `logging.file` | 终端仪表盘；开启时日志写入文件 | 开启，`logs/engine.log` |

## 密钥配置（`.env`，仅实盘需要）

密钥按 **venue**（而非按腿）配置——无论该 venue 是 base 还是 hedge 腿，
都读自己的区块。

- **entropy / tradexyz（Hyperliquid）** —— 在
  <https://app.hyperliquid.xyz/API> 创建 API（agent）钱包。`HL_PRIVATE_KEY`
  填 **agent 钱包私钥**，`HL_ACCOUNT_ADDRESS` 填主账户地址。当两条腿都是
  Hyperliquid 系 venue（如 entropy×tradexyz，任意顺序）时默认共用该账户
  （内部自动共享 nonce 序列）；如需分开，设置
  `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`。注意给所交易的各 dex
  分别充入保证金。
- **lighter / lighter-rh** —— `LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`、
  `LIGHTER_API_PRIVATE_KEY`，必须注册在与该腿配置**相同的部署**上
  （主网与 Robinhood 链是两套独立的账户和密钥——参见
  [lighter-python](https://github.com/elliottech/lighter-python)）。
  当**两条腿**都是 Lighter 部署时，hedge 腿改读
  `LIGHTER_HEDGE_ACCOUNT_INDEX` / `LIGHTER_HEDGE_API_KEY_INDEX` /
  `LIGHTER_HEDGE_API_PRIVATE_KEY`（无回退——两个部署账户独立）。
- **aster** —— 在 <https://www.asterdex.com/en/api-wallet> 创建 Pro API 钱包
  （页面顶部切换到 "Pro API"）。`ASTER_PRIVATE_KEY` 填 **API (agent) 钱包**
  私钥；`ASTER_ACCOUNT_ADDRESS` 填**主钱包地址**——它参与每笔签名且无法由
  API 密钥推导，因此两项都必须填写。账户必须是**单向持仓**模式（双向持仓
  会在启动时被拒绝），并保持主机时钟 NTP 同步（签名请求携带微秒级 nonce）。

## 执行机制

- 两条腿**同时发出吃单**：Lighter 用带均价保护的市价单，在鉴权 websocket
  上异步确认成交；Hyperliquid 与 Aster 用 IOC 限价单同步结算（结果未知时
  轮询订单状态兜底）。
- **持续性闸门**（`premium_persist_sec`）：信号先"武装"，持续存在才触发，
  过滤单 tick 的假信号。
- **库存阶梯**：仓位超过上限的 `floor_frac` 后，同方向加仓需要线性递增的
  额外溢价，满仓时最高加 `scale_bps`。
- **净敞口对冲**：两腿成交不对等时立即用 reduce-only 单（带滑点保护）
  削减敞口，并每 `reconcile_sec` 与链上仓位对账。
- **故障隔离**：被限频的交易所短暂暂停；交易所不可达（如例行维护）时暂停
  交易并每 `venue_probe_sec` 探测直至恢复；连续 `max_consecutive_errors`
  次执行异常则整体停机。
- **仅实盘**：没有模拟成交模式。`--record-only` 是唯一无风险的运行方式，
  其余都是真金白银。

## 目录结构

```
entropy_arb/cli.py       CLI 入口 — entropy-arb / python -m entropy_arb
entropy_arb/__main__.py  python -m entropy_arb 启动器
entropy_arb/config.py    YAML + .env 配置契约与校验
entropy_arb/book.py      订单簿 + 含手续费的套利规模计算
entropy_arb/feeds.py     官方 HL ws + zkLighter ws + Aster ws 行情
entropy_arb/venue_hl.py  Hyperliquid dex 适配器（Entropy、tradexyz）
entropy_arb/venue_lighter.py  zkLighter 适配器（主网、Robinhood 链）
entropy_arb/venue_aster.py    Aster DEX V3 适配器（EIP-712 签名 REST）
entropy_arb/engine.py    双交易所策略主循环
entropy_arb/dashboard.py Rich 终端仪表盘
entropy_arb/recorder.py  分钟级盘口数据采集
tools/analyze.py         minutes.duckdb -> 阈值建议
tests/                   python3 -m pytest tests/
```

## 已知风险

- **中枢填错就是亏钱策略。** 溢价中枢会漂移，请定期重新测量并保持
  `config.yaml` 与市场同步。
- **USDG 基差**（`lighter-rh`）：对冲腿以 USDG 计价，持续溢价中有
  一部分是稳定币本身的基差；midline 吸收其水平，但 USDG 的*变动*是真实盈亏。
- **资金费**：两个交易所、两套独立的资金费率，持仓成本未建模——仓位上限
  请设小一些。
- **薄盘口**：Entropy 深度可能很小；`take_fraction` 与名义上限控制单笔规模，
  但部分成交后对冲腿的滑点是真实存在的。
- **交易时段**：股票类永续（如 SNDK）盘后各所预言机行为不同，建议加宽带宽
  或避开盘后。
- **单腿风险**：一条腿成交后另一条可能失败。机器人会自动对冲并对账，但
  仍需人工关注。

风险自负。本软件直接操作真实资金，本文档不构成任何投资建议。请从最小的
仓位上限开始。

## 开源协议

[MIT](LICENSE)
