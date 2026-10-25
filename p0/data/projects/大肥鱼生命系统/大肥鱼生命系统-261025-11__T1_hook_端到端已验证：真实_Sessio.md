# T1 hook 端到端已验证：真实 SessionStart payload → 输出「项目: 未分类」+ 副本库落下档期

> 编码：大肥鱼生命系统-261025-11 ｜ 类型：fact ｜ 记录于 2026-10-24T18:50:24Z

针对审查报告对 T1 的「未确认」，2026-10-25 02:52 做了**真实 hook 端到端验证**（在生产库副本上，不污染生产库）：

## 做法

```powershell
# 生产库完整副本 → $TEMP\fish_hook_t1\data
$env:FISH_ROOT = <副本 data>
'{"session_id":"hook-t1-verify","cwd":"C:/no/such/path/at/all","hook_event_name":"SessionStart"}'
  | python fish_dsh_hook.py
```

cwd **故意给一个绝不匹配 workspace_map 的路径**。

## 结果（两条独立证据）

**① hook 的输出里明写项目**：

```
[fish] 本次会话凭证已自动签发，无需你操作
  token      : fsh_lndf…
  身份       : dsh-hook-t1-veri（证据级别 machine）
  项目       : 未分类          ← T1 生效
  工作区     : C:/no/such/path/at/all
```

**② 副本库里真的落了档期**：

```
档期: 未分类  to_ts=None  公民=dsh-hook-t1-veri
```

（`to_ts=None` = 开着的档期，会话结束后由 `session end` 关闭。）

## 这条证据说明了什么

- T1 **端到端是通的**：hook 薄壳 → `fish session start --cwd` → `cmd_session_start` → `fallback_project` → 开「未分类」档期
- 之前我说"hook 端到端未验证"是**当时确实没跑**，不是掩饰——现在跑了
- 顺带证明了 `project_stints` 恒 0 的修复是**真的**：这是全库第一次由 hook 自动开出的档期（生产库因为本次会话启动时还没有这段代码，所以仍为 0；下次会话起会开始有）

## 边界

- 本次验证用的是**副本**，生产库 `project_stints` 目前仍是 0 —— 要等**下一次真实会话启动**才会有第一条
- 这个"下一次"不是我能在这个会话里伪造的，只能等

