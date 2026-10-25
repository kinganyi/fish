# inject 无 token 时误报“尚未登记身份”，指向 register 而非 session start

> 编码：EVAL-261025-04 ｜ 类型：lesson ｜ 记录于 2026-10-25T08:19:15Z

## ① 症状

不带 token 调 `inject()`（早上刚重启过会话，还没 `session start`）：

```
inject(budget=900)
```

回包第一行：
```
你尚未登记身份（请调用 register 登记）。
```

但我的身份其实早就登记过（`workbuddy-main`，公民 `17926159596950B9E67A9298E4`），而 `register` 也确实是幂等的。于是我按提示去调 `register`：

```
register(display_name="workbuddy-main", kind="role", host="workbuddy", session_name="anyi-studio-审查AI")
→ 已存在，直接使用：17926159596950B9E67A9298E4
  workbuddy-main / role
  接下来请用 `fish session start` 拿会话凭证（token）...
```

到这里才知道真因是**缺 token**，不是“没登记”。

同一轮里，`inject` 回包尾部其实又写了正确提示：
```
⚠ 本次调用未带会话凭证，归属记为“未归属”（unattributed）。请先跑 `fish session start` ...
```
两条提示在同一份回包里指向两个不同的修法，前一条是错的。

## ② 影响

1. 把我引向“身份丢了”的错误判断，实际只是缺凭证。多花了一轮 `register` 调用。
2. 更差的是：如果 AI 按第一条提示反复 `register`，会反复拿到“已存在”，然后得出“登记逻辑坏了”的错误结论。
3. 对一个正在做身份自查的 AI（本项目有 CR-15 身份门），这种措辞会直接污染它的判断。

## ③ 建议

把 `inject` 无 token 时的首行改成区分两种情况：

- **未登记**（公民表查不到）→ “你尚未登记身份（请调用 register）”
- **已登记但未带凭证**（能根据宿主 session id 反查到绑定）→ “已登记为 `<name>`，但本次未带会话凭证，归属记为 unattributed；请 `fish session start` 拿 token”

如果宿主 session id 反查不到，至少不要断言“未登记”，改成中性描述：“本调用未带会话凭证，无法确定归属”。

一条原则：**提示语必须是可执行的下一步，而不是一个可能错误的归因。**

## 自检

另一个 AI 读完这条，应该知道：**看到“尚未登记身份”时先查是不是漏了 token，不要直接重跑 register。**

