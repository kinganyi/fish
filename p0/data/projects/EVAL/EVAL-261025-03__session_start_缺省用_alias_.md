# session start 缺省用 alias 当 account：首次接入会静默新建第二个公民且无法合并

> 编码：EVAL-261025-03 ｜ 类型：lesson ｜ 记录于 2026-10-25T08:18:52Z

## ① 症状

同一会话（`CODEBUDDY_SESSION_ID=c35cb6ca-1291-4d4d-8628-889e391c67f1`）首次签发凭证：

```
fish session start --project anyi-studio --alias anyi-studio-审查AI
```

返回：
```
citizen_id: 179291152055941AF6FC469A34
citizen: 新建
account_id: anyi-studio-审查AI
```

但同一会话的 MCP `register(display_name="workbuddy-main")` 给我的公民是 `17926159596950B9E67A9298E4`。两者不是同一个。

发现后带 `--account` 重跑：
```
fish session start --project anyi-studio --alias anyi-studio-审查AI --account workbuddy-main
```
返回：
```
citizen_id: 179291152055941AF6FC469A34   ← 没变
citizen: 复用
account_id: workbuddy-main                 ← 只改了归属标签
```

CLI 只有 `citizen-add / citizen-list / citizen-retire`，**没有 merge**。

## ② 影响

1. 同一个 AI 出现**两个公民**：历史记忆在旧公民名下，之后的写入全在新公民名下 → 归属分裂。
2. 我必须回头告诉用户“你之前的记忆不在这个名下”，而这是我一开始一个参数没写造成的，不是用户操作错。
3. **最贵的是它静默**：`register` 成功、`session start` 成功、`remember` 也成功，三个命令全都返回 OK，只有去比 `citizen_id` 两串十六进制才能发现不对。
4. 实际耗时：发现 + 重试 + 核对公民表，约 3 轮工具调用；如果没发现，后续所有写入都会记到错误公民下。

## ③ 建议

任一即可：

1. **`--account` 缺省不退回 `--alias`**：没给就报错；或从宿主 `CODEBUDDY_SESSION_ID` 反查已绑定账号。现在这种做法等于把“会话别名”当成了“账号锚点”，两者语义不同。
2. **`citizen merge --from <A> --to <B>`**：追加一条 `citizen.merged` 事件，把 A 的记忆/凭证改挂到 B，不删历史（符合事件溯源）。
3. **`session rebind --citizen <ID>`**：允许同会话改绑到已存在的公民。
4. 短期缓解：`session start --help` 里把“**首次必带 `--account`，否则会新建公民且无法回绑**”写成醒目提示。

## 自检

另一个 AI 读完这条，应该知道：**首次 `fish session start` 必须显式带 `--account`（用你在 MCP register 里的那个名字），否则会凭空多出一个公民，且事后改不回来。**

