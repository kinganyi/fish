# fish 记忆库工作方式（workbuddy 宿主 / 会话 企业智能-主AI）

> 编码：大肥鱼生命系统-261022-23 ｜ 类型：rule ｜ 记录于 2026-10-21T22:35:46Z

## 记东西
- `remember`，project 固定 "ANYI"
- mem_type：identity / rule / fact / episode / lesson / artifact；其中 rule 与 identity 永不衰减
- 编码由系统分配，不自己编
- 图片、文档走 `attachments`，传本地绝对路径；系统会复制进 blobs/ 并按 SHA256 去重

## 查东西
- `recall`：按语义检索
- `resolve_code`：按编码取
- `neighbors`：看相关记忆

## 边界
- 不直接读写 data/ 目录或 sqlite。绕路会破坏索引，一律走工具。

## 会话坐标
- 宿主：workbuddy
- 身份：workbuddy-main（kind=role，公民 17926159596950B9E67A9298E4）
- 会话名：企业智能-主AI
- 主人：安逸

