# 深读报告 · dsh-world（只读解剖）

> 来源：独立研究员，2026-10-27 03:0x；仓库 `I:\app\DSH-Home\workspace\learn\_research\dsh-world`（package.json 0.5.6，README 仍写 0.5.3）。
> 结论口径：代码级结论带 file:line；README 只作"它自称"。

## 0 一页结论

dsh-world 是 DSH 的"陪伴角色世界"插件。它不新建记忆系统，而是把宿主会话日志**增量投影**成 SQLite 归档，再把"最近 N 轮原文"渲染成一条 user 消息插进每个世界会话最前面（src/runtime.ts:119-131）。
没有向量库/embedding（README.md:22-25）。检索＝按时间倒序分页读归档 + 原文交给模型；旧事靠她自己 grep 导出文件（templates/world-prompt.md:15）。
"总日志"两层：宿主 session log 是唯一原文（图片附件只进它，README.md:69、CHANGELOG.md:97）；archive.sqlite 是插件正本，conversations/*.jsonl 只是给人看的导出（README.md:161）。
压缩由自带 ToolFreeCompaction 顶替宿主默认：剥掉 tool-call/result/reasoning，只留"你们之间的话"；记忆块/世界提示/时间块排除在摘要外，压缩后重新注入（src/compaction.ts:14-20,25；src/runtime.ts:110-113）。
隔离＝worldId（目录）+ agentPreset + 非 subagent + 工作区成员关系；会话路由靠结构化身份 key，不靠显示名（src/ecosystem/shared/identity.ts:36-38）。

## 1 定位
- 她＝陪伴角色（默认 TA），有家会话（`<名字>的家`）与房间（家所在工作区）（README.md:29；src/life/home.ts:55-75）。
- home＝life.json 的 homeSessionId；生活线只在家生效（src/life/types.ts:10；src/life/runtime.ts:246-247,315）。
- 房间＝`<world>/room` 注册成宿主工作区（src/index.ts:84；src/life/home.ts:50-54）。
- 群会话：每个微信/QQ 群各开一个会话，挂同一房间（src/ecosystem/shared/sessions.ts:18-37）。
- 世界控制台＝`/dsh-world/console/*` HTTP 面板（src/console/routes.ts:7,96-105；src/index.ts:210-247）。

## 2 会话总日志
- 代码里没有 transcript 实体；"会话总日志"=宿主每条会话自己的日志。插件用 `ctx.sessionPersistence.open(id,'read')`（src/sourceArchive.ts:43）、list()（:99）、stat()（:114）读。
- 落盘：`<DSH_HOME>/worlds/<worldId>/archive.sqlite`（正本）+ `conversations/<YYYY-MM-DD>.jsonl`（导出）（src/store.ts:15-18,48）。
- Schema：`records(sequence PK, recordId UNIQUE, day, occurredAt, turnId, sourceSeq, body JSON, fileOffset, fileBytes)` + `days` + `progress(sessionId PK, nextSeq, turn)` + `channelSessions` + `channelDeliveries`；WAL + synchronous=FULL（src/store.ts:20-32）。
- 记录体：schemaVersion/worldId/recordId/sourceSessionId/sourceMessageId/sourceSeq/turnId/role/occurredAt/content/status + SpeakerIdentity（src/types.ts:58-71）。
- 写入触发：session/event 的 assistant/message、turn/end、turn/start、compaction/end、user/message 触发 schedule（src/runtime.ts:115），启动 catchUp（:142）；replay() 从 progress.nextSeq 分页读宿主日志 → 抽取 → 同事务 commitRecords 写记录+游标（src/sourceArchive.ts:49-61；src/store.ts:83-92）。
- 写什么：只 user/assistant 可见文本（visibleText 取 text 与 image/file 的 attachment JSON；tool-call/reasoning 丢弃，src/archive.ts:11-17）；排除 subagent、delegationDepth>0、非 append surface、注入记忆块、非 user 源（src/archive.ts:21-26）。
- 幂等：recordId=sha256([worldId,sessionId,messageId]) + INSERT OR IGNORE（src/archive.ts:8-9；src/store.ts:88）；JSONL 可重建（src/store.ts:46-74）。

## 3 共享记忆归档
- `archiveScope`：home|workspace（src/archiveSelection.ts:4-5）；workspace 用宿主 workspaceRegistry 找"家所在工作区"成员（:8-17，注释：成员关系来自宿主，不来自预设或显示名）。默认 workspace。
- 进：按 includesSession 过滤后取最近 N 轮，渲染进**每个**世界会话（src/runtime.ts:82；src/history.ts:10）。
- 不进：home 范围下群正文不进（tests/channelSessions.test.ts:61-78）；群图片永不进共享记忆（README.md:69；tests/wechatImages.test.ts:24）；QQ 群不归档（README.md:83）；子代理/注入块/非 append 替换不入（src/archive.ts:21-26）。
- 原文 vs 压缩：归档是原文不摘要；记忆块是最近 N 轮原文逐轮 JSON 渲染（src/history.ts:63-76），N 默认 200（src/worldSwitch.ts:8）。更早/范围外靠她翻导出（README.md:18；world-prompt.md:15）。
- "只留你们之间的话"：一段摘要替换整个会话表面，剥掉全部 tool call/result/reasoning，被工具切断的 assistant 文本重新拼接（src/compaction.ts:40-53）；指令要求保留意图/情绪/决定/未完成事项/最后一条用户发言，不写记忆块/世界提示/时间块、不提工具（:14-20）；摘要不得比被遮内容更长（src/compactionRegion.ts:130）。

## 4 打标与隔离
- worldId 必须是安全目录名并成为 worlds/<worldId>（src/store.ts:14-15）；换 worldId＝另一个世界另一份记忆（README.md:133）。
- session：sourceSessionId；turnId=`${sessionId}:${turn}`（src/archive.ts:40）；recordId 三元组哈希（:8-9）。给模型的记忆只留 session id 前 8 位（src/history.ts:49-51；tests/history.test.ts:66-67）。
- 平台身份：9 字段 SpeakerIdentity（channel/accountId/conversationId/speakerId/speakerName/conversationType/conversationName/platformMessageId/mentionIds），强校验、speakerId 必须带 `channel:` 命名空间（identity.ts:14-34）；正文前加 `【消息来源 {json}】`，归档时剥掉（:40-42；src/archive.ts:32）。
- 隔离：conversationKey=[channel,accountId,conversationType,conversationId] → channelSessions → 会话 id；显示名不参与路由（identity.ts:36-38；tests/channelSessions.test.ts:24-30）。
- 重启复用三条游标：progress(sessionId,nextSeq,turn)（store.ts:30,91）；家 homeSessionId（life/types.ts:10）；channelSessions（store.ts:31）。

## 5 检索与推送
- 检索：无 query API、无向量。readPage 按 (day DESC, occurredAt DESC, recordId DESC) 分页，游标 {date,occurredAt,recordId,maxSequence} 固定"读的那一刻"（store.ts:97-109）；loadRecentTurns 回翻凑 N 轮，按 turnId 去重、includesSession 过滤（history.ts:10-37），一轮须有 assistant（:27）。
- 取历史两条路：①读 conversations/*.jsonl（world-prompt.md:15）；②`GET /dsh-world/console/snapshot`（轮数/窗口/近似 token/最早时间/记录数/会话数/压缩记录；console/snapshot.ts:59-70；routes.ts:96-105）。
- 注入时机：只在会话开始与成功压缩后（compaction/end 且无 error）插到 pre-step 最前，其余步骤不重投，保 provider 缓存前缀（runtime.ts:26-30,110-113,119-131；tests/plugin.test.ts:102-136）。
- 桥：`<world>/bridge.json={origin,token,pid,startedAt,name}`（bridge/bridge.ts:81）；`POST {origin}/dsh-world/bridge/say`，头 x-dsh-world-token，体 {text}，202（上限 32KB/2000 字，拒跨站与陌生 Host，token 常量时间比较）（bridge/route.ts:5-23；bridge.ts:94-98）；送入的话以 source.kind='user' 进家，照常归档（bridge.ts:105）。
- 推送：她第一段可见回复 → outlets，{text,kind:reply|busy|reach,name,images?}，2xx 送达（outlets.ts:9-20,64-74）；3 分钟无回复推 busy（bridge.ts:12,66）。主动推送走生活线 agent.steer（life/runtime.ts:79）+ reach_out（系统通知+outlets，life/tools.ts:38-55）。
- 微信独立 HTTP：`POST /dsh-world/wechat/ask`，wechat.json 记 {origin,token,path,protocol:3}，适配器轮询 outbox、回报 receipt（wechat/index.ts:16,51-74,80）。

## 6 life runtime
- 生活线总装（life/runtime.ts:42）：只维护一个待办（greeting/review/alarm 取最早，:86-91），一个定时器（:92），串行投递、失败退避（:126-130）。
- 状态：life.json（homeSessionId/alarm/greeting/review/spend，types.ts:10）；wakes.jsonl、activity/<day>.jsonl、compactions.jsonl（lifeStore.ts:41-69）。
- 每日回顾：固定 23:00（alarm.ts:8），nextReview 给下一次与"回顾哪一天"（:75-78）；lookBack 先给我是谁.md 快照（self.ts:24-32），再把当天导出文件路径 + 日记目录 + 我是谁 + 快照路径写进提示（runtime.ts:169-177；alarm.ts:82-86）。她自己读、自己写日记、改自述——插件从不代写（README.md:20）。
- 主动联系：闹钟文案附"这段时间实际做了什么"（读活动日志而非自述，runtime.ts:72-77；activity.ts:34-48）；reach_out 才真送到人面前。

## 7 可借鉴 vs 局限
借鉴 6：①原文优先+轮级窗口、不上向量库（README.md:22-25；world-prompt.md:15）；②记忆块只在会话开始/压缩后插、原地 replace，不破坏 provider 缓存前缀（runtime.ts:85-92,119-131）；③SQLite 正本+每日 JSONL 导出，带 fileOffset/exportedBytes 断点，崩溃/截断可重建（store.ts:46-74）；④每会话 (nextSeq,turn) 游标+幂等 recordId，重放稳定（store.ts:88-92；tests/archive.test.ts:17-22）；⑤压缩"只留对话"：剥工具、排除记忆/提示/时间、压缩后重注（compaction.ts:14-20,25）；⑥身份结构化、显示名不参与路由（identity.ts:36-38）。
局限 4：①固定窗口、无时间衰减/重要性排序，窗口外只能翻文件（history.ts:31；README.md:18）；②无检索接口（routes.ts:96-105 仅 snapshot/activity/diary/prompts）；③归档无限增长、无冷热分层；workspace 会把同工作区其他预设普通会话纳入（CHANGELOG.md:118），隔离是"成员关系"不是沙箱（archiveSelection.ts:8-17）；④平台面不可靠：微信非官方 API、QQ 依赖外部桥、群正文/图片在 home 范围不进共享记忆（README.md:69,71,75,83）。

## 8 判据
tests/ 37 文件（34 *.test.ts + 3 fixture）；微信适配器另有 11 个 Python 测试；CHANGELOG.md:151 自述 Node 226 / Python 40 通过。
- archive.test.ts:12/17/23/33/40/46 — turn 身份与角色、重放稳定、注入/replace/subagent/别预设不计、interrupted、身份字段保留。
- store.test.ts:17/23/33/42/52/59/69 — 跨午夜分文件、分页游标、导出失败回滚、崩溃续传、截断重建、双进程并写、worldId 越界拒绝。
- sourceArchive.test.ts:29/36/42/50 — 日志消失/损坏只丢一份、flush 仍报错、游标遗忘。
- history.test.ts:16/27/40/45/52/70/82/92 — 200 轮原文、跨午夜、无回复不造轮、resume 去重、渲染与注入防护、图片缩短、身份、微信回执。
- archiveScope.test.ts:17/32/45/58 — 改范围需重启、workspace 选家所在工作区、缩小只过滤不删、重启发现冷会话换块。
- channelSessions.test.ts:16/24/32/61 — 身份不改路由、key 区分平台/账号/类型、每群一个持久会话、群拿家记忆但正文不入档。
- worldPrompt.test.ts:86/129；plugin.test.ts:34/63/102/147/166/198/562/581/599；wechatImages.test.ts:24。
缺口：真实桌面/真实账号端到端未回归（CHANGELOG.md:147）；"重启后记忆块逐字一致"无单条端到端测试。

## 9 证据索引
README.md:9,15,18,22-25,45,53,57,59,69,71,75,83,133,161,176-184；CHANGELOG.md:97,118,142,144,147,151；src/store.ts:14-32,46-74,83-92,97-109,126-137；src/archive.ts:8-9,11-17,19-44；src/sourceArchive.ts:19-22,38-64,71-119；src/history.ts:10-37,49-56,63-76；src/runtime.ts:26-34,50-65,80-95,107-131,142-143；src/compaction.ts:14-25,40-53,78-82,236-258；src/compactionRegion.ts:33-41,130,145-154；src/archiveSelection.ts:4-17；src/worldSwitch.ts:8,19-44,54-58；src/life/runtime.ts:72-91,126-130,166-178,188-218,246-262；src/life/alarm.ts:8,75-86；src/life/home.ts:55-75；src/life/lifeStore.ts:41-69；src/self.ts:5,12-32；src/ecosystem/shared/identity.ts:1-42；src/ecosystem/shared/sessions.ts:18-37；src/ecosystem/wechat/index.ts:16,51-80；src/ecosystem/wechat/archive.ts:22-64；src/ecosystem/bridge/bridge.ts:43,81,94-118；src/ecosystem/bridge/route.ts:5-23；src/ecosystem/shared/outlets.ts:9-20,64-74；templates/world-prompt.md:7-15,27-38。
