# 需求
在通过entropy-arb启动实盘交易后，将进程pid写入到文件中
文件路径：/tmp/entropy-arb-${symbol}-${base}-${hedge}.pid
这样 在执行flatten命令时，就可以根据文件路径读取进程pid，然后执行kill命令，使用kill命令终止进程

# 实现决策
- 写入方：cli.amain（非 --record-only 实盘路径）与 cli.web_entry（非
  record-only 的 web 仪表盘）启动时写 pid，退出时（finally）删除；
  --record-only 不写文件。
- 读取方：cli.flatten_entry 在 creds 校验之后、run_flatten 之前调用
  kill_running_bot，避免平仓单与引擎自身下单互相竞争。
- 安全边界：
  - 写入失败仅 warning，绝不阻止交易；
  - flatten 发信号前用 `ps -p <pid> -o command=` 核实命令行含
    entropy-arb/entropy_arb，防止 pid 复用后误杀无关进程；
  - SIGTERM 优雅退出（引擎已有信号处理），约 5s 未退出则 SIGKILL；
    进程消失或文件缺失/损坏时静默跳过；kill 后删除 pid 文件。
- 文件路径格式：/tmp/entropy-arb-${symbol}-${base}-${hedge}.pid
  （symbol/base/hedge 用启动时解析出的组合，CLI 覆盖优先于 config.yaml）。
- 测试：tests/test_cli.py 新增 5 个用例（路径格式、实盘写+退出删、
  record-only 不写、kill 真实子进程、外来 pid 不动）；219 个测试全绿。
