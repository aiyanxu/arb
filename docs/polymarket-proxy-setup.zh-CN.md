# Polymarket Perps 代理钱包 API 凭据生成指南

本文档说明如何在当前项目（entropy-arb）中生成并配置 Polymarket Perps 的
"API 密钥"。Polymarket Perps **没有传统意义的 API key**——它通过
**代理钱包（proxy wallet）委托**完成鉴权：你（OWNER）用主钱包签名一次
createProxy 仪式，把交易权限委托给一个新生成的 PROXY 钱包，API 返回一个
secret。之后本项目只需要这三项凭据：

| 凭据 | 环境变量 | 用途 |
|---|---|---|
| 代理钱包地址 | `POLYMARKET_PROXY_ADDRESS` | 标识代理凭据；私有 REST 请求的 `polymarket-proxy` 请求头 |
| 代理钱包私钥 | `POLYMARKET_PROXY_PRIVATE_KEY` | 签名每一笔交易操作（EIP-712 Op） |
| 代理 secret | `POLYMARKET_PROXY_SECRET` | 私有 REST / WebSocket 鉴权（`polymarket-secret` 请求头） |

**OWNER 私钥不进入项目、不写入 `.env`**——它只在生成凭据的那一刻使用一次。

代码入口：

- 生成工具：`tools/polymarket_make_proxy.py`
- 凭据读取：`entropy_arb/config.py:429`（`PolymarketCreds`）
- 签名与请求头：`entropy_arb/venue_polymarket.py:88`（`PolymarketAccount`）

---

## 1. 认证模型（30 秒版）

```
OWNER 钱包（你的 Polymarket 主账户）
   │  EIP-712 "CreateProxy" 签名（仅一次）
   ▼
POST /v1/account/proxy  ──►  API 返回 { "secret": "..." }
   │
   ▼
PROXY 钱包（脚本新生成的 EVM 密钥对，专用于交易签名）
   + secret（配合 proxy 地址做请求头鉴权）
   ──► 写入 .env，机器人用它们交易
```

关键性质：

- 交易签名用的是 **PROXY 私钥**，owner 私钥绝不参与日常签名，也不落盘。
- 凭据**默认有效期约 7 天**，到期后需重新运行生成脚本并**三项一起替换**。
- 每次运行脚本都会生成**全新的** proxy 密钥对；旧凭据在到期前依然有效
  （本项目/官方文档均未提供吊销接口），旧 proxy 私钥同样要当秘密保管。

---

## 2. 前置条件

1. **已有 Polymarket 账户**，且账户里有用于 Perps 的 pUSD 保证金
   （充值入金见官方文档 <https://docs.polymarket.com/perps/fund-your-account>）。
2. **能拿到 OWNER 钱包的 EVM 私钥**（即控制该 Polymarket 账户的签名者）：
   - 浏览器插件钱包：直接导出私钥即可；
   - 邮箱（Magic）登录的账户：需先在 Polymarket 网站设置中导出私钥。
3. **Python 环境已装 `[live]` 依赖**（`eth_account`、`aiohttp`；脚本还依赖
   `python-dotenv` 供 `.env` 加载——主程序必需）：

   ```bash
   .venv/bin/pip install -e '.[live]'
   ```

4. **主机时钟已 NTP 同步**。签名携带毫秒级时间戳 `ts`，时钟偏差过大会被拒。

---

## 3. 生成凭据（一次性）

### 3.1 准备 owner 私钥（推荐用环境变量，避免进 shell 历史）

```bash
read -s POLYMARKET_OWNER_PRIVATE_KEY && export POLYMARKET_OWNER_PRIVATE_KEY
# 粘贴私钥（0x 开头，回车确认），不要留 shell 历史
```

> 也可以直接 `--owner-key 0x...` 传参，但该值会留在 shell 历史里，不推荐。

### 3.2 运行生成脚本

```bash
cd /path/to/arb
.venv/bin/python tools/polymarket_make_proxy.py
```

脚本内部做的事（与官方文档一致，见 `tools/polymarket_make_proxy.py:47`）：

1. `Account.create()` 生成一个**全新的** PROXY EVM 密钥对（旧的作废不管）；
2. 用 OWNER 私钥对 EIP-712 `CreateProxy` 消息签名
   （domain：`{"name":"Polymarket","version":"1","chainId":137}`，
   message：`addr`=新代理地址、`exp`=过期毫秒时间戳、`salt`、`ts`）；
3. `POST https://api.perpetuals.polymarket.com/v1/account/proxy` 提交
   `{op:{type:"createProxy",args:{owner,proxy,expiry}}, sig, salt, ts, label}`；
4. 打印三项凭据与过期时间。

可用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--expiry-days` | `7` | 凭据有效期（天，可为小数） |
| `--label` | `entropy-arb` | 在 Polymarket 设置页显示的凭据标签 |
| `--api-url` | 生产网关 | 一般不动；测试环境才需要改 |

成功输出形如：

```
Proxy credential created — add these to .env:
POLYMARKET_PROXY_ADDRESS=0x1111...
POLYMARKET_PROXY_PRIVATE_KEY=0x2222...
POLYMARKET_PROXY_SECRET=xxxxxxxx

Expires 2026-09-11 11:17:00 UTC — re-run this script afterwards and replace
all three values together.
```

---

## 4. 写入 `.env`

把三个值填入项目根目录的 `.env`（`.env` 已在 `.gitignore` 中，切勿提交）：

```dotenv
# ===== venue: polymarket =====
POLYMARKET_PROXY_ADDRESS=0x1111...
POLYMARKET_PROXY_PRIVATE_KEY=0x2222...
POLYMARKET_PROXY_SECRET=xxxxxxxx
# expires: 2026-09-11 11:17 UTC   ← 建议把过期时间写成注释，便于轮换
```

三项**必须来自同一次运行**，缺一不可（`PolymarketCreds.complete` 会检查，
见 `entropy_arb/config.py:135`）。实盘启动时若缺项会直接报错：
`live trading needs credentials for both venues in .env`。

---

## 5. 验证凭据

### 5.1 请求头鉴权（最直接）

```bash
curl -s "https://api.perpetuals.polymarket.com/v1/account/portfolio" \
  -H "polymarket-proxy: 0x1111..." \
  -H "polymarket-secret: xxxxxxxx"
```

- 返回 `{"margin":{"total_account_value":...,"available_order_margin":...}, "positions":[...]}` → 凭据有效；
- 返回 401/403 或鉴权错误 → 三项值有误或已过期。

### 5.2 交易签名链路（可选冒烟测试)

用不存在的 client order id 发一次取消请求——服务端会因"订单不存在"报错
而**不是**签名/鉴权错误，即证明签名链路可用（思路同
`entropy_arb/venue_polymarket.py:482` 的注释）：

```python
# tools/polymarket_sign_smoke.py
import asyncio, os, aiohttp
from dotenv import load_dotenv
from entropy_arb.venue_polymarket import PolymarketAccount, _compact_cancel

API = "https://api.perpetuals.polymarket.com"

async def main():
    load_dotenv()
    acct = PolymarketAccount(os.environ["POLYMARKET_PROXY_ADDRESS"],
                             os.environ["POLYMARKET_PROXY_SECRET"],
                             os.environ["POLYMARKET_PROXY_PRIVATE_KEY"])
    coid = "f" * 32                      # 不存在的 coid，仅验证签名链路
    sig, salt, ts = acct.sign_op(_compact_cancel(coid))
    body = {"op": {"type": "cancelOrdersCOID", "args": [coid]},
            "sig": sig, "salt": salt, "ts": ts}
    async with aiohttp.ClientSession() as s:
        async with s.delete(API + "/v1/trade/orders-coid", json=body) as r:
            print("HTTP", r.status, await r.text())

asyncio.run(main())
```

预期：HTTP 200 且错误信息指向订单/参数；若出现 signature/auth 类错误，
说明三项凭据不配套或已过期。

---

## 6. 接入 config.yaml 并运行

1. 把其中一条腿设为 polymarket（两腿不能相同）：

   ```yaml
   symbol: BTC            # Polymarket 侧会解析为 BTC-USD 永续
   base_venue: entropy
   hedge_venue: polymarket
   ```

2. 腿级参数（`hedge:` 或 `base:` 区块）：

   ```yaml
   hedge:
     taker_fee_bps: 4.0        # 不得低于 venue 默认 4 bps（config.py:388 强制）
     max_position_usd: 200     # 首次实盘建议从很小的仓位上限开始
   ```

3. 先用 `--record-only` 验证行情连通（该模式不需要任何密钥）：

   ```bash
   .venv/bin/entropy-arb --record-only --base entropy --hedge polymarket --symbol BTC
   ```

4. 小仓位实盘：

   ```bash
   .venv/bin/entropy-arb --base entropy --hedge polymarket --symbol BTC
   ```

---

## 7. 到期轮换（约每周一次的例行操作）

1. 到期前（`.env` 注释里记过时间）重新运行第 3 节的脚本；
2. 把输出的**三项新值连同过期时间注释**一起替换 `.env` 中的旧值；
3. 重启机器人。旧凭据在旧过期时间之前仍然有效，若机器人还在跑，先停再换。

建议加一个日历提醒或 cron 提醒。**只换三项中的一项会导致鉴权/签名不一致而全部失效**。

---

## 8. 安全要点

- OWNER 私钥只在生成时用一次：用 `read -s` 注入环境变量，用完
  `unset POLYMARKET_OWNER_PRIVATE_KEY`；绝不写入任何文件。
- PROXY 私钥能直接交易，等同资金权限：只存在 `.env`（已 gitignore），
  不得提交、不得进日志、不得进聊天记录。
- 每次轮换都会产生一个新 proxy 密钥对，**旧 proxy 私钥在旧过期时间前仍可交易**，
  需与现行凭据同等保管。
- `--label` 会显示在 Polymarket 设置页，便于识别/清理历史凭据。

---

## 9. 故障排查

| 现象 | 可能原因 / 处理 |
|---|---|
| 脚本报 `createProxy failed: HTTP 4xx` | owner 私钥错误；账户不存在（需先在 polymarket.com 注册）；`ts` 与本机时钟偏差过大（查 NTP）；`exp` 已过期（`expiry-days` 配成 0/负数） |
| 脚本报 `no secret in response` | 网关返回了非预期结构，查看完整报错体；确认 `--api-url` 指向 `https://api.perpetuals.polymarket.com` |
| 启动报 `live trading needs credentials...` | `.env` 三项有一项为空，或 `.env` 未被加载（默认读项目根目录 `.env`，可用 `--env-file` 指定） |
| 私有 GET 返回 401/403 | `polymarket-proxy`/`polymarket-secret` 两项不配套，或凭据已过期 → 重新生成 |
| 下单返回签名类错误 | `POLYMARKET_PROXY_PRIVATE_KEY` 与 `POLYMARKET_PROXY_ADDRESS` 不是同一密钥对（三项不是同一次脚本运行的产物） |
| 下单报保证金类错误（`insufficient_margin` 等） | 凭据没问题，账户 pUSD 保证金不足（引擎会暂停该 venue，见 `MARGIN_ERRORS`） |
| 启动报 `not found on Polymarket` / `ambiguous` | symbol 写法问题：用完整 symbol（如 `BTC-USD`）或在 `symbol_map.yaml` 加映射 |

---

## 10. 附录：协议细节（排障时可对照）

- **CreateProxy 类型化数据**（owner 签名用，`tools/polymarket_make_proxy.py:31`）：

  ```json
  {
    "domain":   {"name": "Polymarket", "version": "1", "chainId": 137},
    "primaryType": "CreateProxy",
    "types": {"CreateProxy": [
      {"name": "addr", "type": "address"},
      {"name": "exp",  "type": "uint64"},
      {"name": "salt", "type": "uint64"},
      {"name": "ts",   "type": "uint64"}]}
  }
  ```

- **私有 REST 读**（持仓/权益/订单查询）：请求头
  `polymarket-proxy: <地址>` + `polymarket-secret: <secret>`。
- **交易操作签名**（PROXY 私钥，`entropy_arb/venue_polymarket.py:53`）：
  对 MessagePack 编码的紧凑 op 数组取 keccak256 得 32 字节哈希，再按
  EIP-712 `Op{data: bytes32, salt: uint64, ts: uint64}` 签名
  （domain 同上，chainId 137 仅作防重放标签）。
- **WebSocket 私有频道鉴权帧**：

  ```json
  {"id": 1, "req": "post",
   "op": {"type": "auth", "args": {"proxy": "<地址>", "secret": "<secret>"}}}
  ```

- 官方文档：<https://docs.polymarket.com/perps/authenticated-sessions>
