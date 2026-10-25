# 记忆归属约定：项目前缀 anyi-studio，ANYI 仅为检索别名

> 编码：anyi-studio-261025-22 ｜ 类型：rule ｜ 记录于 2026-10-25T06:58:30Z

## 约定（2026-10-25 起生效）

1. **项目前缀 = `anyi-studio`**。记忆编码形如 `anyi-studio-YYMMDD-NN`。
2. **`ANYI` 降级为检索别名**（`alias list` 显示 `ANYI → anyi-studio`，2026-10-24T20:20:29Z，备注「2026-10-25 项目改名治理」）。别名**只影响检索，不改变写入归属**——查得到旧码，但新写入一律落 `anyi-studio`。
3. **归属的权威来源是「工作区映射」**，不是调用方自报。已登记：
   - `I:/app/anyi-studio` → anyi-studio（主代码库）
   - `I:/app/anyi-studio-AI工作空间` → anyi-studio（记忆/artifacts）
   - `I:/app/anyi-studio-审查` → anyi-studio（外部审查产物）
   查：`python fish.py workspace list`
4. **fish 现在必须带会话 token**：`cd "I:/app/DSH-Home/workspace/learn/大肥鱼生命系统/p0" && python fish.py session start` → 回 token + citizen_id，24h 有效。进程内身份已废除（根因：2026-10-23 串号事故）。
5. `fish` 不在 PATH，一律 `python fish.py <子命令>`。

## 背景
治理轮把原本混乱的 ANYI 项目收敛为 anyi-studio，并清理了空壳前缀。本轮补齐了缺失的工作区映射（此前全库只有 fish 自身 1 条）。

## 生效范围
本工作区（anyi-studio-AI工作空间）及 anyi-studio 代码库/审查目录的所有新记忆。

