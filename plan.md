# 需求
当前程序执行`python3 main.py --record-only --symbol SNDK --hedge lighter-rh`时会将信息写入`logs/minutes.csv`
现在要使用duckdb替代`logs/minutes.csv`，将信息写入duckdb数据库