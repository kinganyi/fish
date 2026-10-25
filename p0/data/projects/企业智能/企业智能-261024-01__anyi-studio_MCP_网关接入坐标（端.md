# anyi-studio MCP 网关接入坐标（端点/权限/38 操作/调用方式）

> 编码：企业智能-261024-01 ｜ 类型：rule ｜ 记录于 2026-10-23T17:20:45Z

# anyi-studio MCP 网关：接入坐标（不含 Key）

## 端点与协议
- 端点：`https://anyippt.cn/mcp/`（**必须带尾斜杠**）
- 协议：MCP Streamable HTTP，legacy 握手版 `2025-06-18`（实测可用）；另有现代无状态版 `2026-07-28`
- 服务端：`anyi-studio-mcp-gateway` v2.1.0

## 鉴权（Key 不在本文）
- 请求头：`Authorization: Bearer <ays_...>`；Key 全文仅存于 `C:\Users\15596\.workbuddy\mcp.json` 的 `mcpServers.anyi-studio.headers`
- 本 Key 身份（2026-10-24 实测 platform.identity）：username=`anyi`，**mcp_role=tenant_admin**，`money_visible=true`，`channel_visible=true`，`effective_tenant_id=1`
- 权限：租户级 + 站长/管理员角色 → topic.* 之外全部可调；`topic.confirm` / `topic.rollback` 为高危，须平台审批

## 操作面（实测 38 个，文档写 37）
多出的一个是 `platform.identity`。八大能力域：
- **platform**：capabilities / identity（接入起点）
- **topic**：五话题开发（站点UI/模型接入/路由/动作/能力skill）——除 roundback/confirm 外均需租户上下文
- **generation**：task.submit / validate / status / status_batch / get / cancel / retry
- **model**：list / get / health
- **session**：list / create / rename / layout（生成任务的组织容器 = 站内画布）
- **asset**：list / download_url / upload_ticket / upload_complete / rename / finish / copy / delete / chain
- **history**：generation.history
- **review**：topic.export / topic.import_verify

## 业务失败口径（易错）
业务拒绝是 **HTTP 200 + `result.isError=true`**，典型 reason：`not_registered` / `role` / `tenant_scope` / `scopes` / `invalid_params`。**不要用 HTTP 状态码判成败**。传输层错误才是 401/429/400。

## 可在当前会话直接调用的方式
```
python C:/Users/15596/.workbuddy/skills/anyi-studio-feishu/scripts/platform_mcp_ping.py \\
  --base-url https://anyippt.cn --api-key <key> \\
  --tool <操作名> --params '<json>'
```
客户端库：`skills/anyi-studio-feishu/lib/platform_mcp.py`（纯标准库、无依赖）。
MCP 工具形式的 `anyi-studio` 条目已写入 `~/.workbuddy/mcp.json`，需在连接器管理页右上角手动「信任」后才会作为工具出现。

## 可用模型（2026-10-24 model.list 实测）
seedance-2.0 / notoken-seedance-2.0 / notoken-seedance-2.0-fast / musk-seedance-2.0 / notoken-seedance-2.5 / apimodels-seedance-2.5 / gemini-omni-flash / gemini-omni-video；渠道：apimodels / notoken / musk。

