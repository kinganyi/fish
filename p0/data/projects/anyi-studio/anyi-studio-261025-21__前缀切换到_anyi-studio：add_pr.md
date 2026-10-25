# 前缀切换到 anyi-studio：add_project 拒绝但 remember 可写（实现矛盾，需维护方看）

> 编码：anyi-studio-261025-21 ｜ 类型：fact ｜ 记录于 2026-10-25T06:57:41Z

> 记录者：`anyi-studio-开发AI-2`（会话 `8a83d218`）。时间：2026-10-25 15:0x。

## 背景
用户要求：fish 侧已完成一轮数据治理，原「ANYI」项目拆分完毕；**后续 anyi-studio 相关内容一律写入 `anyi-studio` 项目，不再用 `ANYI`**。

## 实测到的矛盾（值得跟进）

| 事实 | 证据 |
|---|---|
| ✅ `anyi-studio` 前缀**已在用** | `recall` 返回编码均为 `anyi-studio-261022-99` / `anyi-studio-261025-16` 等 |
| ✅ 治理已完成 | `anyi-studio-261025-16`「ANYI 拆成 anyi-studio(96)+大肥鱼生命系统(55)，309 处引用重写」 |
| ❌ `add_project(prefix="anyi-studio")` **报错** | 「prefix 不能含 '-'（编码用它分隔段）」 |
| ✅ 但 `remember(project="anyi-studio")` **写入成功** | 即本条记录 |

## 判断
`add_project` 的校验规则（禁止 `-`）与**存量数据已使用含 `-` 前缀**的事实相矛盾。可能是治理时直改数据、绕过了 `add_project`；也可能是校验只拦截「新注册」而不拦截「写入」。

**后果**：任何会话想确认「anyi-studio 这个项目是否存在」，调 `add_project` 会得到否定答案（报错），但实际上项目完全可用。这会让人误判、甚至改用别的名字重建一个重复项目。

## 建议
`add_project` 对**已存在**的前缀应先查库直接返回「已存在」，再做字符校验；或把 `-` 的禁令只用于「新前缀」并对存量白名单放行。

## 后续约定（本次生效）
- anyi-studio 相关 → `project="anyi-studio"`
- **不再使用 `ANYI`**（旧编码已重写，再写 ANYI 会形成第三个前缀）
- 大肥鱼生命系统自身的记忆 → `project="大肥鱼生命系统"`

