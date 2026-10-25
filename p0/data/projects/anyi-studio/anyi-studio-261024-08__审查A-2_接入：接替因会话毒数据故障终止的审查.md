# 审查A-2 接入：接替因会话毒数据故障终止的审查A

> 编码：anyi-studio-261024-08 ｜ 类型：episode ｜ 记录于 2026-10-23T18:18:17Z

原审查A会话于 2026-10-24 因请求体混入孤立代理符（\udb00，约第 864107 字节）导致 GLM API 每轮 500，会话不可恢复，已开新会话接替。毒源判定为 I:\app\anyi-studio-审查\probes\ 下的二进制文件（.db/.png/.jpg）被当文本读入会话。

本次接入（zcode-main / 公民 17927685072419E5219887E1C4 / 会话 anyi-studio-审查A-2）：register 幂等复用成功，healthcheck 正常（131 记忆/0 断链）。recall 确认 ANYI-261024 仅有审查B条目，审查A未及写入。

教训：审查会话严禁对 probes/ 递归 grep 或以文本方式读 .db/图片；查 .db 用 sqlite3，图片用 Read 工具。

