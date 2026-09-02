# 背景
目前采样数据时，数据统一落在logs/minutes.duckdb中，现在需要可以根据symbol来分表存储

要求
- 每个symbol对应一个表
- 每个表的字段与logs/minutes.duckdb中的字段一致
- 每个表的索引与logs/minutes.duckdb中的索引一致
- 执行analysis.py时，可以根据symbol来查询
- 每个symbol的查询结果是独立的，互不相关