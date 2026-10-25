# fish 反馈：project 写入即锁死，且新建前缀只能走 CLI 不在 MCP 里

> 编码：企业智能-261022-01 ｜ 类型：lesson ｜ 记录于 2026-10-21T22:42:28Z

## 场景
接入时按任务书用 `project="ANYI"` 写了 7 条（大肥鱼生命系统-261022-23~29）。随后发现：① 本工作空间项目名是「企业智能」，ANYI 对不上；② ANYI 下还混着 anyi-studio 的技术交接记（anyi-studio-261022-30「生成防重 P0 已合 main 但生产未生效」），两类内容互相干扰检索。

## 卡在两处
1. **改不了旧条目**。只有 remember / recall / resolve_code / neighbors / inject，没有 update / move / delete。project 直接决定目录 `projects/{PROJECT}/` 与编码前缀 `{PROJECT}-YYMMDD-NN`，所以那 7 条只能永久留在 ANYI，改名只能重写一遍，旧条目变孤儿。
2. **新建前缀也不在 MCP 里**。改用中文「企业智能」直接调 remember，报「未知项目前缀：企业智能（先运行 project-add）」——但 MCP 工具列表里根本没有 project-add，只有 CLI（`fish.py project-add <prefix> --name`）有。我是翻了 `fish.py` 源码才知道怎么加。对一个只能走 MCP 的会话，这一步会直接卡死。

## 期望
- MCP 暴露 `project-add`；或 remember 遇到未知前缀时，把具体命令和 --root 路径直接回显给调用方。
- 给一个 `move` / `reproject`，否则项目一旦命名就锁死。
- remember 时对前缀做一次存在校验，避免随手拼错产生新前缀。

