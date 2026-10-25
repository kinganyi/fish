# MCP 接入回执 v2.0（对外·2026-10-22）：validate 已修 / 额度剩余 189.76 / 幂等仍未接线

> 编码：anyi-studio-261022-56 ｜ 类型：artifact ｜ 记录于 2026-10-22T04:59:03Z

## 这是什么

对外客户（杨智勇 / `dayong`，tenant 1）《MCP 通道接入 · 服务端确认清单》Q1–Q10 的**逐条回复 v2.0**，承接 10-21 的 v1.0。

## 相比 v1.0 的唯一实质变化

**`task.validate` 的素材属主缺陷已修并上线**（10-22 07:14）。
旧行为：预检把 `getattr(task,"user_id",0)` 取成 0 → 自有素材被判不存在（`[1503]`/`[1603]`），而同一请求 `task.submit` 却能成功。
修法：`capabilities.py:1103-1104` 显式传 `_principal_user_id(principal)`。
⇒ 给客户的指引从「别信 validate」反转为「可以当真实预检用」。

另外两处：团队额度改全站口径（对 team 5 无影响，因为其消耗全在 tenant 1）、后台权限改造（MCP 面零变更）。

## 本版的两个新产出

1. **额度口算**：`dayong` 属 team 5，`limit 1000 / offset 1004.7826 / 全站消耗 1815.0235` → 剩余 ≈ **189.76**，余额已不高。
2. **登记了一条我方待办**：REST 建单响应未过隐私投影（`routes/tasks.py:137-141` 裸 `task.model_dump()`，Task 含 `cost_cny`/`total_price_cny`）。

## 不变的两条待办

- `record_id` 幂等**仍未接线**（`mcp_task_idempotency` 只出现在迁移清单/测试里）
- REST 建单投影（上条）

## 源文件

`I:/app/anyi-studio-AI工作空间/artifacts/05_交接与复盘/MCP接入_服务端回执_v2.0_20261022.md`

## 附件
- [MCP接入_服务端回执_v2.0_20261022.md](blobs/5f/5fabf5f93fcd51103f1eca1ce639425064f810332bb78ffd470062ed8b63068f.md) (18822 B, sha256:5fabf5f93fcd…) — 对外回执 v2.0（可直接发给杨智勇）

