# 背景
执行命令 entropy-arb flatten --config config-btc.yaml 报错
usage: entropy-arb flatten [-h] --symbol SYMBOL --base VENUE --hedge VENUE [--config CONFIG] [--env-file ENV_FILE] [--symbol-map SYMBOL_MAP]
entropy-arb flatten: error: the following arguments are required: --symbol, --base, --hedge

# 目标
当传递 --config 参数时，能够成功执行命令，从 config 文件中读取参数