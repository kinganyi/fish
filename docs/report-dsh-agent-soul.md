# 深读报告 · dsh-agent-soul（只读解剖）

> 来源：独立研究员，2026-10-27 03:1x；仓库 `I:\app\DSH-Home\workspace\learn\_research\dsh-agent-soul`（219 文件，含 16MB demo，已跳过）。
> 口径：代码级结论带 file:line；README 是"它自称"，冲突以 `docs/ECHO-权威口径.md` 为准（其自定）。

## 0 一页结论

它不是"记忆库"，是给 DSH 长会话补"回取链"的一套判据+零件。要防的失败一句话（README.md:52 自称）：「这才是『思考退化』：不是变笨，是在错误的前提上继续正确地推理」。手段硬：所有压缩/裁剪产物第一行必须是可回取指针（门牌）`log:<会话>#seq=<n>`；一手原文不搬家（memindex.mjs:614「数据仍在原处…不搬家」）。采集自动扫盘，检索仍靠 AI 主动开口，所以把时钟+SELF+门牌条款每步推进上下文补"触发"。

## 1 定位
- DSH 插件，8 个工具（README.md:12）。
- 五层（soul/00-layers.md:6-12）：Ⅰ ECHO 空间 / Ⅱ VITAL 时间与方向 / Ⅲ 能力 / Ⅳ 档案 / Ⅴ 运维。⚠️ ECHO 内部另有 L0–L5 六层，:14-15「两套别混」。
- 四条判据（soul/01-criteria.md:5-10）：①被管理资源 ②唯一权威源 ③失败模式+检测法 ④接口；②最硬「两处都写=双重记账」（:12-15）。
- 哲学原文：docs/ECHO-权威口径.md:87「摘要只能是索引，没有指针的摘要不许进 L4」；:95「摘要只是索引，不是记忆本体。一手层永远优先于任何转述」；:96「不连续不是缺陷，是要设计的东西」；:97-98「所有压缩／裁剪的产物都要指向原文…绝不猜」。瓶颈论：memq.mjs:6「瓶颈不是检索精度，是触发——取回不是问题，知道要取才是问题」。

## 2 采集
- 路径 `<DSH_HOME>/sessions`（echo-config.mjs:95-96；dshHome 推断 :87-91），布局 `<sessions>/<工作区>/<会话>/session.v<N>.jsonl.zstd`（memindex.mjs:212-231）。
- 格式：多帧 zstd + 帧内 JSONL，事件 `{type,seq,time,data}`；v3/v4 同解析器（session-store.mjs:9-12），版本号仅元信息（:14-15）。
- 入口：decodeZstd 逐帧→按行→carry 跨帧，单行>8MB 丢弃（memindex.mjs:90-146）。
- 触发：`memindex.mjs build`（:59）或面板 /api/rebuild（plugins/echo/index.js:399-408）。无 cron、无 hook；「每天 04:00」只是口径，心跳零件本仓库不提供（echo-doctor.mjs:975-981）。
- 唯一读宿主活状态旁路：memory/lib/index.js:200-216 直读 `storages/session_projcache/sessions/session-<sid>.json` 取 projectedTokens。
- 不写会话事件（README.md:116-118）。

## 3 存储
| 介质 | 内容 | 证据 |
|---|---|---|
| summaries.jsonl | 摘要 + srcSeqs + manifest | memindex.mjs:316-364 |
| words.jsonl | 原话/结论逐字 cap4000 | :375-410 |
| files.jsonl / shards / frames | nextByte、帧偏移表 | :414-444；framestore.mjs:31-42 |
| SELF.md | 12288B | self.mjs:39 |
| 02-墨.jsonl | 8 类型×6 字段 | soul/12-ledger.md:29-38,52-63 |

- 打标：session/ws/t/seq/turn/status；摘要带 shadowedTokens/provider/model。
- 原文保留：会话日志 append-only，裁剪留全回链（pruner README:32-33）。
- 压缩边界：只有 L2 可丢，L3/L4 丢即事故（权威口径:59-61）；门牌插首块 compaction/index.js:288-323；取不到范围→降级不猜 :313-321。

## 4 检索注入
- 无 FTS/BM25/向量（memq.mjs:129-131，同义词表双向）；三档 exact/norm/synonym :153-177；归一化只匹配不展示 :106-120；粗筛不 parse :197-237；四动词 :14-19；open 走帧索引二分+读 3 帧（framestore.mjs:139-197）。
- 回上下文：每步 systemPrompt.context(order130/131) memory/lib/index.js:307-322；水位 0.45/0.60/0.80 :280-298；MCP 常驻 mcp__echo__*（echo-mcp.mjs:8-12）。

## 5 触发
- 每步（时钟/SELF/水位/门牌）、压缩时、裁剪时、手动重建、回合边界（huanqi 判空回合，属 VITAL）；无 SessionStart 专用钩子。
- 自动放宽 <3 条 → --near（:676-685）。
- 闸门：索引中≥5 跳过书房并明说 :541-543,700-702；4000 字/8MB/512KB/媒体黑名单/深度 4；no-op 早退 :257-274。
- huanqi：8000/1/2048/6（src/index.ts:649,650,615,621），无冷却窗口用跨会话额度（README:117-139）。

## 6 多宿主
- 版本无感（session-store.mjs:14-15）；工作区目录+ws 字段（memindex.mjs:245）；门牌带会话号；子会话 includeSubagents=false（memory/lib/index.js:57-60,135-144）+ huanqi session.meta.origin 早退（README:104-113）；主导/执行靠账本 by/lead/container，promise 只 lead 能写（soul/12-ledger.md:95-108）。
- 边界：仅 DSH 单宿主；额度有意跨会话共享（README:131-133）。

## 7 可借鉴 vs 局限
借鉴：①指针优先+三级纪律（精确/降级/绝不猜）compaction/index.js:120-138；②先 super 再改、加不上不拖垮主流程 :288-328；③帧偏移索引+size/mtime 自证失效（framestore.mjs:16-20,88-93，40.9s→1ms :8-10）；④三层增量（memindex.mjs:253-301）；⑤结构判据取代字符串搜（echo-doctor.mjs:517-535；权威口径:157-170）；⑥只留 text 不留 reasoning（:393-399）+注入前屏蔽凭据（self.mjs:48-69）。
局限：①「成果还不是一等公民」（memindex.mjs:548-549 自陈）；②mnemon 洞察无门牌与 L4 规矩 1 结构冲突（协同说明书:130-147）；③未验面：Windows 原样 ps1/真实压缩出门牌/面板渲染/升级兼容（external-validation-v5:50-64）；「自动兜底那条路仍未验」（compaction/index.js:311）；④心跳不自带、无向量语义、整机<100ms 未达（README:887-889）、检索仍 pull。

## 8 判据
- echo-doctor 实际 15 项（README 说"十项"，文档漂移）:1153-1169；门牌真取 seq，取不到=error :574-580；exit 只反映 error :1209-1214。
- memindex audit/verify 每个 srcSeqs 必须找得到否则 exit1（:496-551,:589-605）。
- soul-probe 只答"谁在管压缩"（ctor/chain/ownKeys），skipped≠error（tools/soul-probe/index.js:6-9,157-160）。
- mnemon-probe 80/90% 两档且明说测不到"实际注入几条"（:65-68,74）。
- echo.mjs --selftest 逐字节比对（echo.mjs:27-29）。verify-live 30/1/0（SANDBOX-VERIFICATION:171）、verify-packaging 116/116（v5:13）。

## 9 证据索引
README.md:12,40-52,61,76-95,116-118,630-665,782,887-889 · soul/00-layers.md:6-15 · soul/01-criteria.md:5-15 · soul/10-echo-anatomy.md:37-44,74-85 · soul/12-ledger.md:29-38,52-63,95-108 · docs/ECHO-权威口径.md:52-61,87-98,143-170 · docs/ECHO六层与mnemon协同说明书.md:102-103,130-147 · kit/echo/session-store.mjs:9-15,41-85 · echo-config.mjs:87-96,119-126 · memindex.mjs:59,90-146,151-153,212-231,253-301,316-410,414-444,496-551,589-614 · framestore.mjs:8-20,31-42,88-93,139-197 · memq.mjs:6,106-120,129-131,153-177,197-237,541-543,676-685,700-702 · self.mjs:39,48-69,191-194 · echo-mcp.mjs:8-12 · echo.mjs:27-29 · echo-doctor.mjs:483-591,966-998,1153-1169,1209-1214 · plugins/memory/lib/index.js:4-8,57-62,135-144,200-216,252-255,280-298,307-322 · plugins/compaction/index.js:118,275-282,288-328 · plugins/pruner/README.md:32-33,76-95 · plugins/huanqi/README.md:104-139 · plugins/huanqi/src/index.ts:615,621,649-650,3209-3236 · plugins/mnemon-probe/index.js:65-74 · tools/soul-probe/index.js:6-9,41-63,157-160 · plugins/清单.md:28-32 · external-validation-2026-10-22-v5-accepted.md:50-64 · SANDBOX-VERIFICATION.md:127-153,171,191-231
