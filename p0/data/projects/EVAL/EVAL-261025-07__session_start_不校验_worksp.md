# session start 不校验 workspace 映射：当前目录无法映射也照样签发 token，并把 --project 回显当成了归属证据

> 编码：EVAL-261025-07 ｜ 类型：lesson ｜ 记录于 2026-10-25T08:22:00Z

### ① 症状

```bash
python fish.py workspace list
# 首次实测：只有 1 条映射，且是 fish 本体自身
#   i:/app/dsh-home/workspace/learn/大肥鱼生命系统 → 大肥鱼生命系统（R1 预置）

python fish.py session start --project anyi-studio
# 正常签发，输出 project: anyi-studio，无任何提示
```

我实际工作的三个目录——`I:/app/anyi-studio`（代码库）、`I:/app/anyi-studio-AI工作空间`（记忆/artifacts）、`I:/app/anyi-studio-审查`——**全部不在映射里**。我是靠外部文档知道「写入归属的权威源 = workspace 映射」才手动补了 3 条（补完现为 4 条）。

### ② 影响

- **把参数回显当成了归属证据**：`session start` 回的 `project: anyi-studio` 只是 `--project` 的回显，不是映射命中。我看到这行就以为归属已确定，实际判据是空的。
- **「判据缺失但看起来成功」**：若不补，写入归属的依据是什么——**未验证**。但「签发成功 ≠ 归属可判定」正是 fish 自己在 `anyi-studio-261025-19` 里总结的「宣称不代表建成」的同款形态。
- 成本：额外 4-5 次往返去确认「映射到底是不是权威源、我的目录在不在里面」。

### ③ 建议

1. `session start` 增一行自检：用 cwd 匹配 workspace 映射，未命中就打印 `⚠ 当前目录无 workspace 映射（写归属无依据）：建议 fish.py workspace add <path> --project <PREFIX>`。
2. 命中时也回一行 `归属：anyi-studio（workspace 映射 I:/app/xxx 命中）`，与 `--project` 回显**分开标注**，别让参数看起来像证据。
3. 映射表条目很少（如 <2 条）时，在 `session start` 里额外提示「映射表仅 N 条，多目录工作请先补全」——新人接入最容易漏的就是这一步。

