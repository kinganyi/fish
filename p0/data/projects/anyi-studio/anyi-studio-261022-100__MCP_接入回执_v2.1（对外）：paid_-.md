# MCP 接入回执 v2.1（对外）：paid_* 已修并部署 / 角色表述更正 / 渠道三方案

> 编码：anyi-studio-261022-100 ｜ 类型：artifact ｜ 记录于 2026-10-22T06:02:35Z

## 三条处置

| 反馈 | 定性 | 处置 |
|---|---|---|
| paid_* 全 null | **真缺陷** | 已修 `9b4bd59` + **14:01 已部署**，真机返回 `paid_points=13 / paid_cny=12.3039` |
| 角色判定不一致 | **我方写错** | 已更正：他是 `tenant_admin`（`money_visible=true`），null 与角色无关 |
| 渠道不能指定 | **设计如此** | 给机制+三方案，推荐方案 B（建 `seedance2.5-hq` 单候选公开码） |

## 部署回执（三源）

- 单文件热修，PID `1006381` → **`1013378`**，NRestarts=0，停机约 20s，双域 health 200，traceback 0
- 文件双端 sha 一致（CRLF 版 `3ac38e33…`，146291 B），旧版备份 `.bak_9b4bd59_prod`
- nginx：`14:01:16 POST /mcp/ 200 674`
- DB：订单行 `point_delta=13 / balance_delta=12.3039 / tenant_id=1`

## 写回执时新发现的两件事

1. **他能读到渠道名**：`task.get` 的 `structuredContent.model_id` 对 tenant_admin 原样返回 `notoken-seedance-2.5`，
   但**提交时又指定不了**。所以"渠道不可选"的原因不是隐私，而是提交路径的对齐机制。
2. **对齐有透传出口**：`align_submission_model` 第③步——**不属于任何启用路由的 model_id 原样透传**。
   所以若要锁渠道，不一定非改代码：建单候选路由即可。

## 附件
- [MCP接入_服务端回执_v2.1_20261022.md](blobs/ee/ee872ebba1761cab9bc7c37067a2936dded0c93d30f785ec18724ce8c4891ade.md) (9676 B, sha256:ee872ebba176…) — 对外回执 v2.1（答客户实测报告三条）

