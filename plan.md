# 需求
回答问题 -> 所有在运行中的仓位是否都可以通过webapp进行管理

# 实施方案：webapp 持仓管理能力（flatten / pause-resume）

## 0. 设计原则

- **只允许"降风险"操作**：webapp 永远不能开仓。只加两类能力：
  一键平仓（reduce-only，复用 `flatten.py`）和暂停/恢复策略循环。
  不做任意下单接口 —— 保持"trading path stays in the engine/CLI"的边界，
  只把已有人工操作（CLI flatten、手动重启）搬到网页上。
- **默认关闭，显式开启**：管理接口需要 `.env` 中配置 `ARB_WEB_TOKEN`
  才启用；未配置时返回 403 并提示。默认行为与现状一致（纯只读）。
- **仅内嵌引擎模式可用**：`entropy-arb web` 内嵌引擎时直调 engine 方法；
  bot 跑在别的进程（artifact 模式）时返回 409 + 指引用 CLI
  （跨进程指挥需要 IPC，风险/复杂度不成比例，本期不做）。

## 1. 后端 — engine 扩展（entropy_arb/engine.py）

新增状态（与单向的 `halted` 区分，paused 可逆）：

- `self.paused = False`；`_evaluate()` 开头：`if self.halted or self.paused: return`
  （`_scan` 只被 `_evaluate` 调用，无需改）。
- reconcile / hedge 循环**不受** pause 影响 —— 净敞口对冲和对账是
  安全网，暂停只停"开新仓"。
- `request_pause()` / `request_resume()`（幂等，resume 在 halted 时无效）。

新增 `async def flatten_all()`（webapp 以任务方式启动它）：

1. record_only → 直接失败（无签名器）。
2. `paused = True`，初始化 `self.flatten_state = {running: True, rounds: 0,
   log: [], result: None}`，并 `self.flatten_in_progress = True`。
3. 等待 `_exec_tasks` 清空（超时 `settle_timeout_sec + 2`，仍占用则报错退出，
   提示稍后重试）—— 复用 `_run_inner` 停机时的等待逻辑。
4. 持有两条腿的 `_vlock`（先 acquire 两把），调用
   `flatten.flatten_positions(self.base, self.hedge, self.cfg)`
   —— `flatten.py:43` 现有实现完全兼容引擎内 venue 对象
   （send_taker/fetch_position/px_round/book 接口一致），原样复用；
   其内部 `await asyncio.sleep(1.0)` 轮间等待期间继续持锁，
   reconcile 会排队等待，无竞争。
5. 释放锁；`result = "flat" | "not_flat"`，`running=False`，
   `flatten_in_progress=False`；保持 paused（平完仓默认不再自动开新仓，
   由用户在网页上手动恢复）。
6. halted 状态下**允许** flatten（这正是 HALTED 提示"flatten manually"
   的网页替代）。

## 2. 后端 — API（entropy_arb/webapp/__init__.py）

鉴权中间件（仅作用于写接口）：

- `ARB_WEB_TOKEN` 非空时启用管理；校验 `Authorization: Bearer <token>`
  （`secrets.compare_digest`）。token 永远不进日志、不进任何 GET 响应。

新增端点（全部 POST，读接口不动）：

| 端点 | 行为 |
|---|---|
| `POST /api/engine/pause` | `engine.request_pause()` |
| `POST /api/engine/resume` | `engine.request_resume()` |
| `POST /api/positions/flatten` | 已在跑 → 409；否则 `create_task(engine.flatten_all())` |
| `GET /api/flatten/status` | 返回 `engine.flatten_state` |

错误语义统一：403 未配 token/token 错；409 artifact 模式、record-only、
flatten 进行中、halted 时 resume；401 缺/错 Authorization 头。

快照扩展（`engine_snapshot()`，随 `/ws/live` 每 2s 推送）：
engine 节点加 `paused`、`flatten_in_progress`、`flatten_state`。
`/api/config` 不新增任何内容。

CORS 收紧：`allow_origins=["*"]` → 仅同源 + vite dev
（`http://localhost:5173`）。前端本就同源部署，收紧无副作用。

## 3. 前端（frontend/src/）

- `types.ts`：EngineInfo 增加 `paused`、`flatten_state` 字段。
- `App.tsx` 新增 Controls 卡片（仅 engine 内嵌时渲染；artifact 模式
  显示"管理功能需要 entropy-arb web 内嵌引擎"的说明）：
  - Pause/Resume 切换按钮；
  - Flatten 按钮 → 二次确认（输入 FLATTEN 的模态框）；
  - flatten 进行中显示轮次进度（来自 ws 快照），结束后显示
    "两腿已平仓 / 仍有残余，请到交易所确认"横幅。
- Token：设置弹窗粘贴 token，存 `localStorage("arb.web.token")`；
  403 时提示配置 `.env` 的 `ARB_WEB_TOKEN`。
- header 徽章：现有 HALTED 旁增加 `PAUSED` 状态。

## 4. 配置与文档

- `.env.example`：加 `ARB_WEB_TOKEN=`（留空 = 管理端点关闭）。
- README.md / README.zh-CN.md：Web dashboard 一节补"仓位管理"小节：
  用法、token 安全说明（默认绑定 127.0.0.1，公网部署需反代加 TLS）、
  "引擎在别的进程时用 CLI flatten"的边界说明。
- CLI `web` 子命令 description 补一句管理端点说明。

## 5. 测试

- `tests/test_engine.py`：paused 阻止 `_evaluate`；`flatten_all` 用 stub
  venue 验证：平仓前 set paused、发 reduce-only 单、结束后
  flatten_state.result 正确、期间 venue lock 被持有。
- `tests/test_web.py`：
  - 无 token → 403；错 token → 403；对 token → 200；
  - artifact 模式（engine=None）→ 409；
  - StubEngine 增加 paused/request_pause/request_resume/flatten_all，
    验证 pause/resume 端点；
  - flatten 端点 409（进行中）分支；
  - `/api/live` 快照含 paused/flatten 字段。
- 回归：现有全部测试必须保持绿。

## 6. 实施顺序（每步一个 commit，沿用 gitmoji 风格）

1. `♻️ refactor(engine)`：paused 状态 + flatten_all（含 flatten.py 复用）
   + engine 测试。
2. `✨ feat(web)`：鉴权 + 三个 POST 端点 + 快照扩展 + CORS 收紧 + web 测试。
3. `✨ feat(web)`：前端 Controls UI（暂停/平仓/token 弹窗）+ build 同步到
   `entropy_arb/webapp/static/`。
4. `📝 docs`：README×2、.env.example、CLI help。

## 7. 风险与对策

- **误触平仓**：前端输入确认 + reduce-only 单本身不开风险；
- **token 泄露**：只在 Bearer 头传输、不落日志；默认空值=功能关闭；
- **flatten 与策略竞争**：flatten 前置 paused + 等 `_exec_tasks` 清空 +
  持双 venue 锁，竞争窗口为零；
- **公网暴露**：文档明确只建议 127.0.0.1 / 反代加认证后暴露。