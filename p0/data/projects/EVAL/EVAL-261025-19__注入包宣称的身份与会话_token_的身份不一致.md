# 注入包宣称的身份与会话 token 的身份不一致（hook 硬编码 FISH_ACTOR=workbuddy-main）

> 编码：EVAL-261025-19 ｜ 类型：lesson ｜ 记录于 2026-10-25T12:58:34Z

## ① 症状（可复现）

同一条会话里，两处对「我是谁」的回答互相矛盾。

**复现步骤**

1. 在 WorkBuddy 会话中，让 SessionStart hook 触发一次（`~/.dsh/hooks.json` 里注册了 `fish_session_hook.py`，其 `MODE` 默认 `inject`，见 `fish_session_hook.py:30`）。
2. 观察注入包第一行（`FISH_ACTOR=workbuddy-main python fish.py inject` 可直接复现）：
   ```
   登记身份：workbuddy-main（公民 17926159596950B9E67A9298E4，形态 role，角色 executor-reviewer，宿主 workbuddy）。
   你的主人是 安逸（公民 179261404173893688AC120C04，形态 resident）。你以角色成员身份行动，不要自称主人。
   ```
3. 在同一会话执行 `fish session start --project EVAL`：
   ```
   token: fsh_...
   citizen_id: 179287060402874057A87716F4
   session_key: codebuddy:c48fd810-f235-49df-a306-ae6df53d4542
   display_name: codebuddy-c48fd810-f23
   ```

**结果**：注入包说我是 `workbuddy-main` / `17926159596950B9E67A9298E4`；token 说我是 `codebuddy-c48fd810-f23` / `179287060402874057A87716F4`。两个不同的公民，同一条会话。

**根因**：`fish_session_hook.py:27` 写死 `ACTOR = os.environ.get("FISH_ACTOR", "workbuddy-main")`，并用它构造子进程环境 `FISH_ACTOR=ACTOR` 去跑 `fish inject`。于是**所有** WorkBuddy 会话注入的都是同一个宿主级共享账户，与会话实际签发的凭证无关。

## ② 影响

- **注入包每轮都在对模型说「你是 workbuddy-main」**，而模型写记忆时 token 指向另一个公民。模型容易据注入包自称身份，进而：
  - 在正文里写「我（workbuddy-main）…」，让后来的读者以为这是共享账户做的事；
  - 判断「这条记忆是不是我写的」时用错基准；
  - 最坏情况：顺着注入包的「主人是安逸」叙事去猜自己是主人——这正是 2026-10-22 `resolve_owner` 吃狗粮事故（AI 写入被静默记到主人名下）与 2026-10-23 串号事故的同一形状。
- 排查时浪费判断：要花额外步骤才能确定「注入包身份」与「写入归属」哪个是真的。本轮为此多跑了 4 次查询（whoami / inject / session start / audit 日志对照）。
- 与 fish 自己的铁律第 3 条「身份是系统签发的，不要根据记忆叙事推断自己是谁」直接冲突——注入包本身正在提供一段不可靠的身份叙事。

## ③ 建议

1. **注入包的身份段改为按当前会话凭证渲染**，不要用 `FISH_ACTOR` 单值。若此刻无凭证，就明写「本次会话未签发凭证，身份未核」，比注入一个共享账户更好——参考 `whoami` 无 token 时返回「身份未核 / unattributed」的既有正确做法。
2. **若 `workbuddy-main` 必须保留**（宿主级账户有其用途），在注入包里把它标成**账户**而非**身份**，并显式区分：`账户=workbuddy-main（宿主共享）` / `本次身份=<token 对应公民 or 未核>`。当前注入包把两者混为一句「登记身份：」。
3. 在注入包抬头加一个可自证字段，例如 `凭证: fsh_...前8位 / 公民: <id>`，让模型能一眼对账，不必再跑 `session start` 反查。

（本条全部为实测，无未验证推断。）

