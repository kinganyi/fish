# 项目前缀治理：写入只用 anyi-studio，ANYI 已退役为只读别名

> 编码：anyi-studio-261025-23 ｜ 类型：rule ｜ 记录于 2026-10-25T07:00:28Z

# 项目前缀治理：写入只用 anyi-studio

> 依据：2026-10-25 项目改名治理（`fish alias list` 可查：`ANYI → anyi-studio`，单向）。

## 规则

| 场景 | 用什么 |
|---|---|
| **写入**（remember / 新记录） | `project="anyi-studio"` |
| **检索旧账** | `project="ANYI"` 仍可用（别名单向映射到 anyi-studio） |
| **已存在的 ANYI-* 编码** | 不改、不重建；检索时自动归到 anyi-studio |

## 为什么

治理前 ANYI 同时装了两条业务线的内容（anyi-studio 与「企业智能」早期误写），检索相互污染。改名后：
- anyi-studio 是唯一写入前缀；
- ANYI 退为**只读别名**（别名只影响检索，**不改写入归属**）。

## 同类治理（一并记住）

- `cdql → CDQL`
- `测试项目 → TESTP`

## 会话凭证（token）

- MCP 工具均支持 `token=` 参数；不带则归属记 `unattributed`。
- token 由 CLI `fish session start --project anyi-studio --account <账号>` 签发，**明文只显一次**。
- 注意：**首次 `session start` 必须带 `--account`**（缺省会拿 `--alias` 当账号 → 新建一个公民，且无合并命令）。

