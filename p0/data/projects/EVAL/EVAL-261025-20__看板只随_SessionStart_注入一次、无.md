# 看板只随 SessionStart 注入一次、无任何变更推送，多AI陆续开工场景失效

> 编码：EVAL-261025-20 ｜ 类型：lesson ｜ 记录于 2026-10-25T12:59:29Z

## ① 症状（可复现）

看板内容确实会自动进入模型上下文，但**只在会话启动那一刻，之后再不更新**；且没有任何事件级推送。

**复现：确认「有注入」**

1. `data/_audit/hook-calls.jsonl` 里可以看到同一会话被反复注入，例如本次会话 `c48fd810-f235-49df-a306-ae6df53d4542`：
   ```
   18:20:27 mode=inject injected 1603 chars
   19:11:33 mode=inject injected 2225 chars
   20:51:31 mode=inject injected 1665 chars
   20:56:34 mode=inject injected 1729 chars
   ```
2. 按 hook 的环境变量复现整包：`FISH_ACTOR=workbuddy-main python fish.py inject`，输出含：
   ```
   看板（2026-10-25）：1 人在场 —— codebuddy-8a83d218-8b2（17929164）（…｜占 projects/eval）（已闲置）
   （另有 1 行已闲置超时仍声明占用：17929164334384A67C1B1F95C4——这些不算重叠，别当成冲突）
   最近动态：· 19:22 … checkin：…
   ──── 1448 / 1600 token（L0=459 L1=318 看板=343 规矩=0 注意=171 L2=0）裁剪=['候选','L2'] ────
   ```
   看板段 343 token，占整包 24%，是第三大块。

**复现：确认「无推送」**

3. `grep -l "board\|看板" fish_dsh_hook.py fish_posttool_hook.py fish_push_hook.py fish_session_hook.py patch_hook.py patch_hook2.py` → **零命中**。6 个 hook 无一处理看板。
4. `fish.py` 里的 `build_push_candidates` / `select_push_candidates` / `build_push_block` / `push_log_rows` 全部服务**记忆推送**，与看板无关。

**结论**：看板是「拉取式注入」——`board_block()` 在 `build_injection_pack` 组装时被调一次（`fish.py:6100`），塞进 SessionStart 的 `additionalContext`。**不是事件推送**。

**可观测的后果（时间线实证）**：本会话 18:20 启动时注入的是当时看板；19:11 之后有别的 AI 签到（看板出现 `codebuddy-8a83d218-8b2`），我在 20:51 之前**完全不知道**，只能靠主动 `fish board show` 才发现。

## ② 影响

- **对「避免撞车」只在开工瞬间有效**。而多 AI 并发最常见的形态是**陆续开工**：A 在 10:00 占 `backend/routes`，B 在 10:20 开工——B 的注入包是 10:00 之前的状态，看不到 A。恰恰在 B 需要这条信息的时候（他已经在写代码）它不在。
- **看板变更对在场者零感知**：有人签到你不知道，有人 `left` 下板你还以为他占着，有人 write 了动态你没看到。看板因此退化成「开局读一次的静态快照」。
- **裁剪是全有或全无**：`fish.py:6149` `while total() > budget and board_text: board_text = ""`——整段清空，没有"只留 head 行"的降级。当前 1448/1600，余量仅 152 token；注意/错题本一长，看板会整段消失。
- 与 `EVAL-261025-15`（裁剪顺序与注释理由矛盾）叠加：代码注释自己写「它只值 150-200 token，比错题本/规矩更该活下来」，实际排序却在注意事项之后被砍。

## ③ 建议

1. **在 PostToolUse 侧加「看板变更」轻量检测**，与记忆推送共用那套节流（每 N 次最多一次）：比对 `board_posts` / `presence.last_seen_at` 的 `max(created_at)` 与会话内已推送的水位，有新增才推一行摘要。**不要推整段**，一行足够：`看板新增：X 在 19:22 签到了 Y`。这样可以复用 `fish_push_hook.py` 已有的频率闸与失败静默纪律，不需要新机制。
2. **裁剪降级而非清空**：`board_text` 被裁时保留 head 一行（「N 人在场，明细见 fish board show」），代价 <20 token，收益是「看板存在」这件事不被静默抹掉。
3. 若短期不做 1，至少在注入包里写明**注入时刻**并提示「以上为 T 时刻快照，开工前建议 `fish board show` 复核」——把"我不知道有变更"变成"我知道我可能不知道"，让 AI 自己决定要不要复读。

（本条全部为实测或代码引用，无未验证推断。）

