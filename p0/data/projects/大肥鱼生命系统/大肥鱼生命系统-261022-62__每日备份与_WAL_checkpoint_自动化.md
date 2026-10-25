# 每日备份与 WAL checkpoint 自动化（+ 订正 D-8 定性）

> 编码：大肥鱼生命系统-261022-62 ｜ 类型：artifact ｜ 记录于 2026-10-22T05:10:45Z

﻿（观察期必做项：每日备份与 WAL checkpoint 自动化；另订正 07 的 D-8 定性）

为什么：一周真实数据全在 data\ 里，此前只有文档里一句「备份纪律」、没有任何副本。
磁盘故障/误删/WAL 损坏 = 七天数据归零。这比任何功能 BUG 都严重。

改法（只改 daily_report.py，复用既有每晚 23:00 计划任务 fish_daily_report，不新增任务）：
 1 PRAGMA wal_checkpoint(TRUNCATE)；busy/log/checkpointed 如实记进 meta（有连接在读写时可能只完成部分，不谎报）
 2 VACUUM INTO 出一致快照 -> data\_backups\YYYY-MM-DD\index.sqlite（不是裸拷；裸拷会丢整个 WAL，见 07 D-9）
 3 写 _meta.json：条数/文件大小/sha256/时间/checkpoint 结果
 4 保留最近 14 天（含今天），只删能解析成日期的目录，删前确认在 _backups 之下（防越界）
 幂等：先写 .tmp -> 独立打开校验（integrity_check + 条数逐项比对）-> 通过才原子替换；
      校验不过删半成品、保留旧备份。绝不拿失败的新备份换掉好的旧备份。
 踩坑：第二次覆盖报 PermissionError，根因是一次没关的只读连接占着句柄；
      已加 _atomic_replace()（5 次退避重试），杀软/索引器短暂占用不该让当晚备份失败。

验收：手动跑一次 -> data\_backups\2026-10-22\index.sqlite（139264 B）+ _meta.json；
 独立打开 68 记忆/108 事件，与 meta 及源库一致；sha256 一致；
 连跑两次 exit 0、单一日期目录、无 .tmp、sha256 相同（幂等）；
 备份后源库 -wal = 0 B（备份前 2459672 B）；测试 44 条全绿（原 33 未改）；
 两个变异（不做 checkpoint / 保留天数算错）各只红对应用例。

顺带订正 07 的 D-8 定性：规格前提「多进程会 SQLITE_BUSY」已被 WAL 实现推翻——
实测 5-6 个连接并发，审计 181 条调用 0 条 busy/locked（2 条失败是业务校验类，1ms），
且并发下 wal_checkpoint(TRUNCATE) 仍 busy=0 并能截断 WAL。
单进程收敛不再是必要项；观察期后按「是否需要跨客户端共享状态」重新评估。


