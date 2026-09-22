# 背景
目前在使用lighter进行对冲时，经常会报错
09:28:43 ERROR   engine: [RH] buy leg: HTTP response body: code=21104 message='invalid nonce' additional_properties={}                                                            │
09:28:43 ERROR   engine: [LIGHTER] sell leg: HTTP response body: code=21104 message='invalid nonce' additional_properties={}

# 目标
修复lighter的nonce问题

# 要求
- 优先采用本地管理nonce的方案
- 可以考虑使用redis作为统一nonce发号器
