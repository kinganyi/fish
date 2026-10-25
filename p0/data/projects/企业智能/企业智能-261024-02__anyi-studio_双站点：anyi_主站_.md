# anyi-studio 双站点：anyi 主站 vs 墨石 moss（tenant_id 都为 1，只能靠域名区分）

> 编码：企业智能-261024-02 ｜ 类型：fact ｜ 记录于 2026-10-23T17:28:46Z

# anyi-studio 双站点坐标（2026-10-24 实测）

## 两个独立部署，不要混用 Key

| 站点 | 端点 | Key | 实际内容 |
|---|---|---|---|
| **anyi 主站** | `https://anyippt.cn/mcp/` | `ays_WP0B…`（开头无下划线） | 9 个画布：cdql事务、爆爆悟空迎宾配音、画画、新会话、???-????-20261018、dafish-assets-20261018、电商AI图应用服务说明、安逸-详情页 |
| **墨石站 moss** | `https://moss.anyippt.cn/mcp/` | `ays__7ALQ…`（**双下划线**） | 2 个画布：默认会话、**AI应用-视频号视频** |

**关键教训**：两个站点**各自的 `effective_tenant_id` 都是 1**，`platform.identity` 看上去完全一样（同为 anyi / tenant_admin）。**靠 tenant_id 区分不了站点，只能靠 base_url。** 换 Key 不换域名 = 查到的还是旧站数据（曾据此误判为“key 未绑租户”）。

## 视频号画布坐标（墨石站）
- 名称：**AI应用-视频号视频**
- session_id：`a36401fe-5847-458e-8e48-fd75585d7153`
- 规模：asset 89（text 45 / image 39 / video 5）、task 106
- 最后更新：2026-10-23T17:23Z（北京时间 10-24 01:23）

## 客户端限制
`lib/platform_mcp.py` 的 `_headers()` 只带 Authorization / Content-Type / Accept / MCP-Protocol-Version / UA——**无任何 tenant 参数或请求头**，租户与站点全部由 base_url + Key 决定。

## MCP 配置
`~/.workbuddy/mcp.json` 已同时登记两个条目：`anyi-studio`（主站）与 `anyi-studio-moss`（墨石站）。

