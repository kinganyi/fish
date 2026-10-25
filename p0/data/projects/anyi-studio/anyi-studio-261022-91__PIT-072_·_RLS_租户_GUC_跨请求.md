# PIT-072 · RLS 租户 GUC 跨请求陈旧，子域写被误拒 ｜ 指针→anyi-studio-261022-82

> 编码：anyi-studio-261022-91 ｜ 类型：lesson ｜ 记录于 2026-10-22T05:53:28Z

## 指针

→ 宿主：`anyi-studio-261022-82`（PIT 错题本·部署与迁移，含 PIT-072）
→ 权威源：`notes/error-ledger.md` ⟦PIT-072 RLS 租户 GUC 跨请求陈旧⟧

## 一句话

租户 GUC `app.current_tenant` **只在事务边界刷新**（init/commit/rollback/heal）；生产是**单长连接**，commit 后不关 → 下一请求的首个写语句执行时，GUC 仍是“上一次 commit 时”的租户 → 子域写（tenant_id=2）遇上陈旧 GUC=1 → RLS 拒 → 500。

本质：**PIT-070 只修了“值”，没修“GUC 时效”** → 是同一条链上的第二根因。

## 为什么单列

`recall` 只搜标题；查 `PIT-072` 不命中宿主（`anyi-studio-261022-83`）。

