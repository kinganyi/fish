# fish 反馈：session start 缺省用 alias 当 account，首次接入会静默新建第二个公民且无法合并

> 编码：anyi-studio-261025-24 ｜ 类型：lesson ｜ 记录于 2026-10-25T07:00:48Z

## 具体场景

按 `register` 的提示，我在 CLI 跑：

```
fish session start --project anyi-studio --alias anyi-studio-审查AI
```

结果：`citizen_id: 179291152055941AF6FC469A34`、`citizen: 新建`。

但我在 MCP 里的 `workbuddy-main` 公民是 `17926159596950B9E67A9298E4`。

## 卡在哪

`--account` 缺省会退到 `--alias`，于是把我这个会话的 alias 当成了新账号，**建了第二个公民**。

发现后带 `--account workbuddy-main` 重跑：
```
citizen_id: 179291152055941AF6FC469A34
citizen: 复用
aaccount_id: workbuddy-main
```
→ **公民没换回来**（只改了归属标签），session_key 已绑死在新建的那个公民上。

而 CLI 只有 `citizen-add / citizen-list / citizen-retire`，**没有 merge**。

## 影响

同一个 AI（workbuddy-main）现在有两个公民：
- 旧：`17926159596950B9E67A9298E4`（MCP register 报的，历史记忆都记在它名下）
- 新：`179291152055941AF6FC469A34`（本会话 token 解出的，之后写入都归它）

记忆归属分裂，且我无法自己合并。这正是 ADR-002 说的 F3「静默错署」——只不过这次错在系统签发层，不在 AI 自述层。

## 期望（任一即可）

1. **`--account` 缺省不退回 `--alias`**：没给就报错要求显式指定，或从已有会话线索推断（宿主的 `CODEBUDDY_SESSION_ID` 已知）。
2. **提供 `citizen merge --from <A> --to <B>`**：把 A 的记忆/凭证改挂到 B（事件追加一条 `citizen.merged`，不删历史）。
3. **或 `session rebind --citizen <ID>`**：允许同会话改绑已存在的公民。

## 顺带确认

`register` 的提示本身是准确的（已说明 session_name 不再写进程身份、改用 token），我只是在执行顺序上踩了坑。如果暂时不修，建议在 `session start --help` 里把“**首次必带 --account，否则会新建公民**”写成醒目提示。

