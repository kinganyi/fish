# PIT-083 · 全量 0F 但 EXIT=1（线程退出竞态告警，非缺陷）｜ 指针→anyi-studio-261022-70

> 编码：anyi-studio-261022-93 ｜ 类型：lesson ｜ 记录于 2026-10-22T05:53:41Z

## 指针

→ 宿主：`anyi-studio-261022-70`（PIT 错题本·验证与审查方法论，含 PIT-083）
→ 权威源：`notes/error-ledger.md` ⟦PIT-083 全量 0F 但 EXIT=1⟧

## 一句话

全量「**0F 但 EXIT=1**」：aiosqlite worker 线程退出竞态告警（`PytestUnhandledThreadExceptionWarning`），**非产品缺陷、非阻塞**（零断言失败；生产走 PG/asyncpg，不经 aiosqlite）。

判据：**认断言数，不只认退出码**。与 PIT-073 家族相邻但不并入（彼=挂死，此=退出码脏）。

## 为什么单列

`recall` 只搜标题；查 `PIT-083` 不命中宿主（`anyi-studio-261022-83`）。

