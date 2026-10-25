# rebuild 定时炸弹已拆：事件缺完整映射 + 改名前缀漏了「标 closed 阻断写入」

> 编码：anyi-studio-261025-19 ｜ 类型：lesson ｜ 记录于 2026-10-25T06:48:44Z

## 问题

`rebuild_projection` 重放 `memory.recorded` 时用的是事件里的 `p["code"]`——而 events 是 append-only（ADR-001），历史事件存的是**旧编码** `ANYI-*`。

> **任何人跑一次 `fish rebuild`，153 条编码就退回旧值，治理结果被一键撤销。**

更糟：`healthcheck` 会建议"跑 rebuild 可归一到系统主人"——**一条无害的建议会炸掉治理结果**。

## 修法

1. **追加 `code.renamed.batch` 事件**（含完整 153 条映射，6586 B）。
   `project.renamed` 只存了 sha256 指纹，不足以还原——**这是当初设计上的一个缺口**。
2. `rebuild_projection` 重放前扫该事件建 `rename_map`，重放时应用。
3. **必须在 `snap.get(code)` 之前应用**：快照的键是新编码，用旧编码去查会查不到，owner/occurred_at 的兜底链全断。
4. 报告增加 `renamed_map_size` / `renamed_events` / `renamed_applied` 三个读数——**看不见就等于没法判断有没有生效**。

## 判据（`test_rebuild_rename.py`，5 条，全绿）

R1 编码不退回去 / R2 行数不变 / R3 映射条目数如实报告 /
**R4 变异：不写映射事件 → 必然退回旧编码**（证明 R1 有判别力）/ R5 老库行为不变。

副本实证：rebuild 前 210 行、`ANYI-*` 残留 4 → rebuild 后 206 行、残留 **0**。

全量 **310 → 315 条全绿**。

## 一条要记住的教训

**"改名"不只是改数据，还要通知所有写入方，而且要用技术手段强制。**

我做完治理后在报告里提醒了"最好没有其他会话在写"，但**没标 `closed` 阻断写入**。于是 10 小时后
ZCode 宿主的 AI 执行了 `add_project ANYI`（`last_seq` 从 0 开始），写了 4 条 `ANYI-261025-01~04`——
**复用了腾出来的编码**（旧的同名编码已迁成 `anyi-studio-261025-01`，主键腾空）。

→ **改名前缀的正确顺序**：① 通知写入方 → ② **标 `closed` 阻断新写入** → ③ 迁移 → ④ 保留可检索。
我漏了第 ②步。

补迁脚本 `gov_migrate_late.py` 已写好并在副本演练通过（4 条 → `anyi-studio-261025-19~22`），
等 ZCode 会话停止写入后跑一次即可。

