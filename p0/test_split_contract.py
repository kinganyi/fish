#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆包契约判据（`fish.py` 拆包第一轮 · T04 · 设计 §1.6）。

这份文件只回答一个问题：**"拆了但调用面没变"这句话有没有可验证的证据**。
它不看实现细节，只锁四条对外承诺：

- **J1 / J1b / J1c / J1d 调用面零变化**
  `fish.<name>` 与 `fish_kb.<name>` 必须是**同一个对象**（不是同名副本），
  清单与 AST 双向对齐（手写清单漏一个 = 运行时静默少一个），
  `_kb_commands_ready()` 仍为 True（它靠 `globals()` 判符号，最容易在搬迁后静默降级），
  共享原语同样是同一对象。

- **J2 依赖单向**：`fish → fish_kb → fish_common`。
  静态查 AST（两份新模块都不许 import fish），运行时在**干净子进程**里查
  `sys.modules`（同时验证两个方向的导入顺序都能跑通）。

- **J3 没有"漏 import"的定时炸弹**：用 `symtable` 找出 `fish_kb` 里所有
  **全局引用**，逐个要求在模块顶层有定义或 import。这类错误只在被调用的那一刻炸，
  单靠"跑一遍测试"抓不全。

- **J4 体量与例外**：行数落在窗口内、`__all__` 规模合理、
  `_kb_push_candidates` 必须在 `fish.py` 且**不在** `fish_kb`（它属于推送管线）。

- **J5 新模块不依赖模块自身身份**：不许出现 `__file__` / `__name__`
  ——搬迁后的函数若靠它们定位路径，指到的是**新文件**而不是原来的 `fish.py`。

纪律：本文件**不建库、不写文件**（连 tempfile 都不需要）；只读源码与模块对象。
"""
import gzip
import ast
import builtins
import hashlib
import json
import re
import importlib.util
import subprocess
import symtable
import sys
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
FISH = P0 / "fish.py"
FISH_KB = P0 / "fish_kb.py"
FISH_COMMON = P0 / "fish_common.py"
FISH_PUSH = P0 / "fish_push.py"
# 2026-11-11（E-11 拆包）新增的两个模块：同样必须守 J2 单向依赖。
# **之前 J2 只列了三份旧模块**——新模块一旦不在名单里，往里加 `import fish`
# 不会有任何判据变红，那正是"循环导入炸弹"最容易进来的时候。
FISH_TRUST = P0 / "fish_trust.py"
FISH_REBUILD = P0 / "fish_rebuild.py"
FISH_SCHEMA = P0 / "fish_schema.py"
FISH_DB = P0 / "fish_db.py"
FISH_PROFILE = P0 / "fish_profile.py"
# 2026-11-20（fish 引入 git · Phase 2）：仓库身份层两个新模块。
# `fish_git_schema` 只含纯 DDL 字符串常量（`fish_schema` 要 import 它拼 SCHEMA，
# 所以它必须比 schema 更底层）；`fish_git` 是逻辑层（发现器 / 身份归一 / 观测探针）。
FISH_GIT = P0 / "fish_git.py"
FISH_GIT_SCHEMA = P0 / "fish_git_schema.py"
# 2026-11-25（113 W1b）：会话树 DDL 叶子（W1 那三块常量从 fish_schema 抽出）。
# 它必须比 `fish_schema` 更底层——零 import、零函数，只含字符串常量（同 fish_git_schema）。
FISH_LINEAGE_SCHEMA = P0 / "fish_lineage_schema.py"
# 2026-11-20（Phase 3）：写库层从 fish_git 拆出（它一度六职责、589 行超窗）。
# 分层：store → git 是向下（写层用读层的解析结果），反向禁止。
FISH_GIT_STORE = P0 / "fish_git_store.py"
# 2026-11-20（Phase 5 收尾）：关联层从 store 拆出（store 五职责 376 行逼近窗口）。
# 分层：link 不依赖 git / git_store——它只在 repo_id/sha 两个外键上与它们相遇。
FISH_GIT_LINK = P0 / "fish_git_link.py"
# 2026-11-21（系统级修复 A-1）：记忆作废是独立领域（不是记忆编排、不是身份、不是 git）。
# 它只依赖 fish_db（append_event）+ fish_common（now_utc），**不 import fish**。
FISH_PURGE = P0 / "fish_purge.py"
# 2026-11-21（系统级修复 D-1）：PostToolUse 的 git 采集下沉到共享核心。
# 它**不 import fish_activity**（那会成环：fish_activity → fish_git_hook → fish_activity），
# 所以判断函数由调用方传参。
FISH_GIT_HOOK = P0 / "fish_git_hook.py"
# 2026-11-21（F-3）：身份诊断从 fish.py 搬出（`whoami all=true` 的取数与渲染）。
# 它**零内部依赖**（只 stdlib）：原来要 `load_citizen`，搬出后改成由调用方传
# citizen dict——没必要为拿一个记录制造 `identity_diag → fish` 的上行依赖。
FISH_IDENTITY_DIAG = P0 / "fish_identity_diag.py"

spec = importlib.util.spec_from_file_location("fish", str(FISH))
fish = importlib.util.module_from_spec(spec)
sys.modules.setdefault("fish", fish)
spec.loader.exec_module(fish)

if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))
import fish_common  # noqa: E402
import fish_kb  # noqa: E402

# 共享原语（设计 §1.2）：五个名字，两边必须是同一对象。
COMMON_NAMES = ("ulid", "now_utc", "iso_local", "count_tokens", "_exc_text")

# J4 行数窗口。设计 §1.6 写的是 15,600–15,900（由"fish_kb ≈ 2,629 行"的估算反推），
# 实测整段搬运（注释与 docstring 照搬）把 fish.py 从 18,356 行降到 15,380 行；
# 设计的估算与它自己给的 15,779 也互相矛盾（2,629+70 ≠ 18,357−15,779）。
# 窗口按实测收紧：判据目的不变（少搬/多搬一整个段都要红），数字以实测为准。
# 【偏差已上报，见交付报告"与设计的偏差"第 1 条】
#
# 2026-10-27（T02 拆包 fish_push）：fish.py 15,464 → 13,980（push 闭包 33 名/1,048 行搬走，
# 30 名基础层下沉 fish_common）。窗口随拆包**迁移**：下界按新基线留余量，**上界不变**；
# 防"多搬/漏搬"的硬判据由 J1/J1b/J7（名字集合 + 对象同一）与 J8（函数体 AST 等价）承担，
# 行数窗口只守"文件规模"。同时给新模块各自加窗口，防未来又长回去。
# 评审 🟡-7（2026-10-27）：窗口从 300 行放宽到 2,000 行被推翻——按实测收窄到 ±200 行。
# 2026-10-28（T03b 合并）：fish_push 因 rerank 接线（+60 行）到 1,309 → 窗口上抬到 1,400
# （功能增长导致的判据迁移，不是弱化：下限仍 1,050，且 J7/J8 守语义）。
# 2026-11-11（E-11 拆包第二刀）：**DDL 常量整块搬到 `fish_schema.py`**（45 个 / 392 行）。
# 建表语句是纯数据（无逻辑、无 IO），是拆包里风险最低的一块；`SCHEMA` 仍留在 fish.py，
# 因为它要拼 `fish_kb._KB_SCHEMA`，搬走会让分层倒置。
# 实测 14,058 → **13,704 行**。窗口**整体下移**：下界 13,800 → 13,400（它守的是
# "别把不该搬的一大块搬走"，搬家后当然要跟着挪），上界 14,150 → 13,900。
# 语义等价的真正硬判据仍是 J1/J1b/J7（名字集合 + 对象同一）与 J8（函数体 AST）。
# 2026-11-11（E-11 拆包第四刀）：建表/补列一族（20 个 ensure 函数 + 2 个常量 / 约 600 行）
# 搬到 `fish_db.py`。实测 13,606 → **12,976 行**，窗口随之下移：13,400–13,900 → 12,700–13,200。
# F-P1-10（2026-11-18）：main() + argparse（686 行）搬进 fish_cli.py 之后，
# 窗口**跟着往下走**。这条是审查方批评「J4 可抬窗」的正解：
# 拆一次就降一次，而不是拆完顺手抬窗把空出来的空间又填回去。方向只许降。
# 2026-10-29（P1 修复轮，外部审查 §8 P1）：🔴11 公民状态校验 / 🔴10 链式+唯一约束 /
# 🔴6 body_relpath 路径穿越拦截 / 🔴9 append_event 事务参数 都落在 fish.py 上，
# 实测 14,302 行 → 上界 14,300 → **14,500**。这是"功能增长"的判据迁移（不是放宽）：
# 下界不动、新模块窗口不动，且这些改动的行为由各自判据（test_identity_lifecycle /
# test_event_tx / test_path_traversal）逐条钉住。
#
# 2026-11-11（债务清理轮）：同样适用上面这条先例，但这次**先抽层再抬窗口**——
#   · 🔴5 身份接管：无凭证路径改由本机身份锚白名单授权（不再"签发过即放行"）；
#   · 🔴9 rebuild 不重放归属：重放前先建 citizen.merged / citizen.retired 映射；
# 两处新增逻辑**已抽到新模块 `fish_trust.py`**（145 行：锚授权 + 归属链解析），
# fish.py 只剩调用与指针（详细演进史在模块 docstring 里，不占这个 14.5k 行文件）。
# 抽完仍净增约 21 行（诊断注释与错误提示要留），实测 14,518 行 → 上界 14,500 → **14,550**。
# 口径不变：下界不动、行为由 test_session_bind（S4/S4b）与 rebuild 判据钉住。
# **真正的解法仍是错题本 E-11（把 fish.py 拆开）**，不是继续抬这个窗口。
# 2026-11-11（E-11 拆包第一刀）：**窗口还回去了**。
#   · `rebuild_projection`（404 行）→ `fish_rebuild.py`；
#   · 它依赖的四个纯函数/只读查询（`slug_title` / `body_relpath` / `resolve_owner` /
#     `split_code_prefix`，共 69 行）下沉到 `fish_common`（拆包的前提：拆出去的模块
#     不许 import fish，所以这些必须先到下层）。
# 实测：fish.py 14,541 → **14,056**。上界因此**从 14,550 收回 14,150**——
# 这才是"抽层"的收益：不是继续抬窗口，而是把窗口还回去（沿用 2026-10-29 那条先例）。
# 下界不动：它守的是"别把不该搬的一大块搬走"。
# 2026-11-18（修外部审查 F-P0-1/F-P0-2/F-P0-3）：13200 → 13300。
#
# ⚠️ 这个上调**正是审查方批评的那件事**（"J4 只守行数，可被抬窗"），所以理由写在这里：
#   · F-P0-1 单事务：record_memory 的写序改成 try/except + 回滚，+24 行
#   · F-P0-2 预检：rebuild 在删投影前先校验全部事件，+20 行
#   · F-P0-3 信任门：_allow_untrusted_lessons + 两处 SQL 过滤，+10 行
# 三处都是**安全修复**，不是功能膨胀。总量 13,226，窗口留 74 行余量。
# 下次再要抬，必须同样写明"加的是什么、为什么必须加"——不接受"顺手调大"。
# ⚠️ 死常量（2026-11-11 标注）：`EXPECTED_LINE_ACTUAL` 在**任何断言里都没被引用**，
# 它的值（14,032）也早已过期。只在 `docs/fix-list-语料层-20261027.md:43` 里被记为
# "陈旧：不改门禁"——保留名字是为了让那条历史记录还能对上号，**不要把它当判据**。
# 真正生效的是上面的 `FISH_LINES_MIN/MAX` 窗口。
EXPECTED_LINE_ACTUAL = 14032
FISH_PUSH_LINES_MIN = 1050
# 2026-10-29（结构整改）：融合/排序层抽出为 `fish_push_rank.py`（9 个纯函数，187 行），
# fish_push.py 1,647 → **1,486**。窗口因此**从 1,700 收回 1,550**——
# 这才是"抽层"的收益：不是继续抬窗口，而是把窗口还回去。
# 新模块另有自己的上界（`FISH_PUSH_RANK_LINES_MAX`），防止它再长成第二个巨石。
FISH_PUSH_LINES_MAX = 1550
FISH_PUSH_RANK_LINES_MIN = 120
FISH_PUSH_RANK_LINES_MAX = 300
FISH_COMMON_LINES_MIN = 550
# 2026-11-11（E-11 拆包第一刀）：`slug_title` / `body_relpath` / `resolve_owner` /
# `split_code_prefix` 四个纯函数下沉到这里（69 行），实测 727 → **802 行**，上界 750 → 880。
# 这是**下沉**导致的增长，不是"又长回去了"：它们搬到下层正是为了让 `fish_rebuild`
# 能在不 import fish 的前提下工作（J2）。
# 2026-11-11（序3）：+`citizen_ids_known`（有效性口径的唯一入口）。
# 它是**新增能力**不是搬家——但值得占这 30 行：把散落各处的
# `SELECT 1 FROM citizens WHERE citizen_id=?` 收敛到一个可审计的地方，
# 正是这次"归档后 rebuild 改写归属"缺陷的根因所在。
# 2026-11-19：950 → 960。加 `corpus.citizen_isolation` 配置键（默认 False）。
# 那个键是用户澄清「持票人不做默认限制」的落地位置——收窄改成可选。
# 2026-11-22：960 → 970。加 `board.empty_sweep_days`（空看板提前归档，缺省 1）。
# 这是**配置键表**的正常增长：所有 `board.*` 键都在这张表里，把新键放别处
# 就等于制造第二处配置面。注释已压到 1 行，只占 2 行（注释 + 键）。
FISH_COMMON_LINES_MAX = 970
# 2026-11-11 新模块 · `rebuild_projection`（404 行 + 说明）：fish.py 里最长也最危险的
# 函数，外部审查 🔴9 就出在它身上。给它自己的窗口，防它再长成第二个巨石。
FISH_REBUILD_LINES_MIN = 380
# F-P1-X（独立评审 🟠-1）：520 → 521。预检从「只扫 memory.recorded」扩到
# 五类事件（superseded/retired/accessed/dormant），多了类型清单与按类型分解
# 的报错。**这是安全修复不是功能膨胀**，且只 +1 行。
# F-P1-X（第二轮评审 🟠-1 剩余）：521 → 554。
# 预检从"只验能解析成 dict"扩到**按事件类型验字段类型**（5 类事件 × 字段契约）。
# 这是评审点名要的修复，不是功能膨胀；注释已压到最短，余量留 8 行。
# 第四轮评审：554 → 565。`DELETE` + 重放包进 `try/except: rollback(); raise`
# （+5 行）。这是**结构性修复**——让"投影未被修改"不再依赖预检枚举全字段。
# 前三轮已经证明枚举永远会漏下一行，所以这个 5 行的结构性保证价值高于 5 行注释。
FISH_REBUILD_LINES_MAX = 565
# 2026-11-11 新模块 · 数据库 DDL 常量 + `SCHEMA` 拼装 + `connect`（纯数据 + 8 行连接）。
# 第三刀把知识库表结构与主库拼装也归到这里——所以窗口随之上抬到 700。
# 2026-11-25（113 W1b · 会话树）：**窗口还回 700，一行都没抬**。W1 曾把这里抬到 780，
# 那正是被审查方点名批评的方向（本文件 :114-115「拆一次就降一次……方向只许降」）。
# W1b 按 `fish_git_schema` 的先例把会话树 DDL 抽成纯常量叶子 `fish_lineage_schema`：
# 75 行搬走、本文件只留 1 行 import，实测回到 **700**（= W1 之前，持平）。
# 判据没变：下界守「搬漏了」，上界守「长回去了」。
# 2026-11-25（113 W4.5 · 推送路由）：700 → **712**。这一波**只加表结构**，不加逻辑：
# `push_log` 两列（`agent_id` / `parent_session`）+ 一条部分索引 `ix_push_agent`（113 §6.1
# 明文要求那三行 SQL），实测 708。这不是"又长回去了"——DDL 常量正是本模块的唯一职责，
# 往别处塞才是制造第二处表结构。余量 4 行。
FISH_SCHEMA_LINES_MIN = 480
FISH_SCHEMA_LINES_MAX = 712
# 2026-11-11 新模块 · 建表/补列/迁移辅助（20 个 ensure 函数 + 2 个数据常量）。
# 注意 `migrate` **不在**这里：它是编排者，要调 KB 专属的 ensure，搬走会成上行依赖。
# F-P1-10（2026-11-18 外部审查）：窗口数字不再写死在测试里，改从
# `j4_ratchet.json` 读。数字写两处必然漂——这不是理论，是这个仓库
# 已经发生过的事（README 徽章 1,647 vs 实际 1,737）。单一出处在 JSON，
# 每次改动必须在 history 里写理由；没有理由的改动在评审时无处可藏。
# **F-P1-X（2026-11-18 独立评审 🟠-3）：从 `_audit/` 搬到顶层。**
# `_audit/` 按 .gitignore 的注释是"临时产物，每批重建，不是真相"，
# 而这个是**真相**（窗口数字的唯一出处）。放在被忽略的目录里 →
# 公开镜像里根本没有这个文件 → `pytest` 在**导入期**就 FileNotFoundError →
# "Interrupted: 1 error during collection" → 整个会话中止。
# 实测：公开树复制时全量测试跑不起来。文件位置错，代价是"别人无法验证结论"。
_RATCHET = json.loads(
    (Path(__file__).resolve().parent / "j4_ratchet.json").read_text(encoding="utf-8"))
FISH_LINES_MIN = int(_RATCHET["min"])
FISH_LINES_MAX = int(_RATCHET["max"])

FISH_DB_LINES_MIN = 520
# 2026-11-18（F-P1-7）：720 → 800。
#
# ⚠️ 这是**搬家**不是膨胀，与"抬窗掩盖增长"是两回事，所以把账算清楚：
#   · F-P1-7 补 `restore_citizen`（55 行）——归档没有还原入口，而错误提示却让用户去跑
#     一个不存在的 `citizen-restore`。
#   · 它放在 fish_db 而不是 fish.py，因为它只用 `_CITIZENS_ALL_COLUMNS` 与
#     `append_event`，都是本模块自己的东西；放 fish.py 会把 God module 顶破 J4。
#   · 净变化：fish.py 13,331 → 13,275（−56），fish_db 699 → 754（+55）。**合计 −1**。
# 判据看的是单文件行数，所以搬家的两端必须同时改窗口——但方向是一个减一个增。
# 2026-11-25（113 W1b · 会话树）：**窗口还回 800，一行都没抬**。W1 的 +19 行
# （`_ensure_lineage_tables` + 两行 import）整块搬到 `fish_lineage.ensure_tables`——
# 建表与建索引共用叶子里的同一个常量，不属于「建表/补列函数族」。实测回到 **800**。
# 2026-11-25（113 W4 · §5.3）：800 → 990。`session_tokens.evidence` 的 CHECK 只能
# 靠 12 步重建改（备份/探针/事务/索引重放），函数按施工图放 fish_db；
# 这是**新能力**（provisional 档），不是把已有逻辑挪回长窗口。
FISH_DB_LINES_MAX = 990
# 2026-11-11 新模块 · 主人画像（R1-1 槽位化；固定表 + 建议队列 + 注入分级）。
# 域逻辑 + CLI 表现层同处一个模块（改画像的人只需打开一个文件）；
# 因此窗口比纯领域模块大——它同时承担了 fish.py 里那 10 个命令的实现。
FISH_PROFILE_LINES_MIN = 600
FISH_PROFILE_LINES_MAX = 1000
# 2026-11-11 新模块：身份锚授权 + 归属合并链解析（外部审查 🔴5 / 🔴9 的修法）。
# 按既有惯例给新模块各自留窗口，防止它长成第二个巨石。
FISH_TRUST_LINES_MIN = 120
FISH_TRUST_LINES_MAX = 260

# 2026-11-20（本轮）：**窗口覆盖补全**。上面这些模块都有窗口，但 fish_board / fish_inject /
# fish_cli / fish_activity / fish_visibility / fish_naming / fish_surface 一直没有——没有窗口
# 的模块可以无限增长而无人察觉，那是棘轮体系的覆盖漏洞。这里按**当前实测**给它们各自设界：
# 下界守"搬漏了"，上界守"长成第二个巨石"。
FISH_BOARD_LINES_MIN = 1800
# 2026-11-22（Wave 2）：2400 → 2440。两件事：
#   ① 占用面提示 `_surface_hint`（只对当前会话、只在其没填时说一句）；
#   ② 帖子↔记忆关联 `recent_memory_code_for_session` / `autolink_memory_code`。
# ② 原本写在 `fish.py`（58 行），但那边有 J4 棘轮且它的职责是编排而不是
# 查事件流；搬到 board 是**为了不抬 fish.py 的窗**，所以这里如实加回。
FISH_BOARD_LINES_MAX = 2440
FISH_INJECT_LINES_MIN = 150
FISH_INJECT_LINES_MAX = 400
FISH_CLI_LINES_MIN = 700
FISH_CLI_LINES_MAX = 1100
FISH_ACTIVITY_LINES_MIN = 1400
# 2026-11-25（113 W4 · §5.2）：1900 → 1920。`resolve_citizen` 加第三级
# （session_identity）——子会话继承父公民后，凭证失效时归属不能掉成 None。
FISH_ACTIVITY_LINES_MAX = 1920
FISH_VISIBILITY_LINES_MIN = 30
FISH_VISIBILITY_LINES_MAX = 140
FISH_NAMING_LINES_MIN = 220
FISH_NAMING_LINES_MAX = 420
FISH_SURFACE_LINES_MIN = 250
FISH_SURFACE_LINES_MAX = 500
# 2026-11-20（fish 引入 git · Phase 2）：仓库身份层的两个新模块。
# 下界守「搬漏了」，上界守「长成第二个巨石」。按当前实测留足余量：
# fish_git 386 行 → 上界 550；fish_git_schema 175 行 → 上界 320。
FISH_GIT_LINES_MIN = 250
FISH_GIT_LINES_MAX = 550
FISH_GIT_SCHEMA_LINES_MIN = 120
FISH_GIT_SCHEMA_LINES_MAX = 320
# 2026-11-25（113 W1b · 会话树）：从 `fish_schema` 抽出的纯 DDL 叶子（零 import、零函数）。
# 当前 97 行 → 上界 200。照 `FISH_PUSH_RANK_LINES_MAX` 的先例给新叶子自己的上界：
# 加表可以长，但「顺手加逻辑」不行——逻辑一进来它就不再是零依赖的叶子了。
FISH_LINEAGE_SCHEMA_LINES_MIN = 60
FISH_LINEAGE_SCHEMA_LINES_MAX = 200
#: 会话树投影层本体（W1 骨架 207 → W2 实现 depth 后 ~380 → W6 还会加 detect_cycles；
#: 留住 200–700 的真实区间，长成第二个巨石时这条会红）。
FISH_LINEAGE_LINES_MIN = 200
FISH_LINEAGE_LINES_MAX = 700
# Phase 3 拆出的写库层：当前 217 行 → 上界 380（还要容纳 Phase 4/5 的写入入口）
FISH_GIT_STORE_LINES_MIN = 150
FISH_GIT_STORE_LINES_MAX = 380
# Phase 5 拆出的关联层：当前 100 行 → 上界 220
FISH_GIT_LINK_LINES_MIN = 60
FISH_GIT_LINK_LINES_MAX = 220
# 作废层：当前 210 行 → 上界 380
FISH_PURGE_LINES_MIN = 120
FISH_PURGE_LINES_MAX = 380
# git 采集（hook 侧）：当前 160 行 → 上界 320
FISH_GIT_HOOK_LINES_MIN = 90
FISH_GIT_HOOK_LINES_MAX = 320
# 身份诊断：当前 250 行 → 上界 420
FISH_IDENTITY_DIAG_LINES_MIN = 150
FISH_IDENTITY_DIAG_LINES_MAX = 420


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _module_level_names(tree: ast.Module) -> set[str]:
    """模块顶层绑定的名字（def/class/赋值/import）。"""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
    return names


def _defined_names(tree: ast.Module) -> set[str]:
    """模块顶层**定义**的名字（def/class/赋值；**不含 import 别名**）。

    为什么需要它：J7/J4 要判"某函数已搬走（fish.py 顶层不再定义）"，而
    `from fish_push import (...)` 的 re-export 会让 `_module_level_names` 里出现同名——
    那是导入面，不是定义面。混用会把合法的 re-export 判成"没搬干净"。
    """
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_modules(tree: ast.Module) -> set[str]:
    """模块顶层 import 了哪些包名（`from a.b import c` 记 `a.b`）。"""
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def _imported_names(tree: ast.Module) -> set[str]:
    """模块顶层 import 进来的**名字**（`__all__` = 顶层绑定 − 这些 − 记账名）。"""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
    return names




class SplitSurfaceTest(unittest.TestCase):
    """J1 家族：调用面零变化（名字、对象身份、清单对齐）。"""

    def test_j1_every_kb_name_is_the_same_object_in_fish(self):
        """J1：`fish_kb.__all__` 里每个名字在 `fish` 侧都在，且**是同一个对象**。

        为什么必须是同一对象：外部 12 个测试文件 335 处 + `mcp_server` 14 处引用的
        都是 `fish.xxx`。若再导出时复制了一份（比如又写一遍函数体，或包一层 wrapper），
        改动 `fish_kb` 的实现不会影响 `fish`，两套行为会慢慢漂开——而判据仍是绿的。
        """
        self.assertGreater(len(fish_kb.__all__), 100, "导出面太小，明显漏名")
        missing, not_same = [], []
        for name in fish_kb.__all__:
            if not hasattr(fish, name):
                missing.append(name)
                continue
            if getattr(fish, name) is not getattr(fish_kb, name):
                not_same.append(name)
        self.assertEqual(missing, [], "fish 侧缺名（外部引用会在运行时消失）")
        self.assertEqual(not_same, [], "fish 侧的名字与 fish_kb 不是同一对象")

    def test_j1b_reexport_list_matches_ast(self):
        """J1b：`fish.py` 的 `from fish_kb import (...)` 与 `fish_kb` 的导出面**双向**相等。

        单向查漏不够：多写一个不存在的名字会 ImportError（响），
        少写一个才是静默的——所以两侧集合都要比，且与 `__all__` 三方对齐。
        """
        tree = ast.parse(_read(FISH))
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "fish_kb":
                imported.update(a.asname or a.name for a in node.names)
        self.assertTrue(imported, "fish.py 里没有 from fish_kb import（拆包没做完？）")
        self.assertEqual(sorted(imported), sorted(fish_kb.__all__),
                         "fish.py 的再导出清单与 fish_kb.__all__ 不一致")
        tree_kb = ast.parse(_read(FISH_KB))
        defined = (_module_level_names(tree_kb) - _imported_names(tree_kb)
                   - {"__all__", "_IMPORTED"})
        self.assertEqual(sorted(defined), sorted(fish_kb.__all__),
                         "fish_kb.__all__ 与它自己的顶层定义集合不一致（_IMPORTED 记漏了）")

    def test_j1c_kb_commands_ready_is_true(self):
        """J1c：`_kb_commands_ready()` 必须为 True。

        它用 `globals()` 判 `kb_tree` / `kb_write` 是否存在。搬迁后 `globals()`
        指向 `fish_kb` 的命名空间——如果导出面漏了这两个名字之一，CAPABILITIES 页
        会静默退回"尚未接入"，而其它判据全绿。这条就是那个缺口的探针。
        """
        self.assertIs(fish._kb_commands_ready(), True)
        self.assertIs(fish_kb._kb_commands_ready(), True)
        self.assertIs(fish._kb_commands_ready, fish_kb._kb_commands_ready,
                      "再导出必须是同一对象（否则 fish 侧调用的是另一份实现）")

    def test_j1d_shared_primitives_are_the_same_object(self):
        """J1d：5 个共享原语在 `fish` 与 `fish_common` 之间是同一个对象。

        这条守的是"只有一份实现"：时间格式、ULID 形态、token 计数一旦分家，
        `kb` 与记忆/看板写出来的时间戳会慢慢不一致（而单测各自都绿）。
        """
        for name in COMMON_NAMES:
            self.assertTrue(hasattr(fish_common, name), f"fish_common 缺 {name}")
            self.assertTrue(hasattr(fish, name), f"fish 侧缺 {name}")
            self.assertIs(getattr(fish, name), getattr(fish_common, name),
                          f"{name} 在 fish 与 fish_common 里不是同一个对象")


class SplitDependencyTest(unittest.TestCase):
    """J2 / J3 / J5：依赖方向、漏 import、模块身份。"""

    def test_j2b_no_function_internal_upward_import(self):
        """**F-P2-1**：`food_common` 里原来有个**函数内** `import fish_activity`。

        J2 的两条判据（静态 + 运行时）只看**模块级** import——函数内的
        "延迟导入"写着"只有实际用到才爆"，实际效果是**隐藏的上行依赖**：
        写进代码不留痕，静态检查看不见，只有那条路径被调用时才炸。

        这条判据扫**所有** `import fish_activity`（含函数内的），
        要求它只能出现在测试里、或在本模块顶层（那才是正常方向）。
        """
        import ast as _ast
        import re as _re
        # **只查真正的底层模块**：`fish_common` 是 J2 链底，它 import 上层
        # 才是"上行依赖"。`daily_report.py` 之类在 `fish_activity` 之上，
        # 它们 import `fish_activity` 是**正常方向**——第一版判据只看"函数内"
        # 不看"方向"，把 `daily_report.py:113` 误报成了违规。
        offenders = []
        for p in [P0 / "fish_common.py"]:
            if not p.is_file():
                continue
            src = p.read_text(encoding="utf-8")
            try:
                tree = _ast.parse(src)
            except SyntaxError:
                continue
            # 模块级：正常方向（fish_activity 是上层，可以 import 基层）
            toplevel = set()
            for node in tree.body:
                if isinstance(node, _ast.Import):
                    toplevel |= {a.name for a in node.names}
                elif isinstance(node, _ast.ImportFrom):
                    toplevel.add(node.module or "")
            for node in _ast.walk(tree):
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                     _ast.ClassDef)):
                    for sub in _ast.walk(node):
                        if isinstance(sub, _ast.Import):
                            for a in sub.names:
                                if a.name == "fish_activity":
                                    offenders.append(
                                        f"{p.name}:{getattr(sub, 'lineno', '?')}")
                        elif (isinstance(sub, _ast.ImportFrom)
                              and (sub.module or "") == "fish_activity"):
                            offenders.append(
                                f"{p.name}:{getattr(sub, 'lineno', '?')}")
        self.assertEqual(
            offenders, [],
            "这些地方在**函数体内部** import 了 fish_activity——那是隐藏的上行依赖，"
            "J2 的模块级判据看不见它：\n  " + "\n  ".join(offenders))

    def test_j2_no_back_import_static(self):
        """J2（静态）：两份新模块都不许 import `fish`；`fish_common` 也不许 import `fish_kb`。

        循环导入的症状不是报错而是"某一侧拿到半初始化的模块"——属性缺失会以
        `AttributeError` 在随机位置出现。所以方向用 AST 钉死，不靠"跑起来没炸"。
        """
        for path, forbidden in ((FISH_KB, {"fish"}),
                                (FISH_COMMON, {"fish", "fish_kb"}),
                                # 评审 🟡-8：T02 新增的 fish_push 也要查（漏 import 炸弹）
                                (FISH_PUSH, {"fish"}),
                                # 2026-11-11（E-11 拆包）：新模块纳入同一判据。
                                # 依赖分层：fish_common ← fish_trust ← fish_rebuild ← fish。
                                # 所以 `fish_trust` 不许碰任何上层；`fish_rebuild` **允许**
                                # 依赖 fish_trust（那是向下），只禁更上层的那几个。
                                (FISH_TRUST, {"fish", "fish_kb", "fish_push", "fish_rebuild"}),
                                (FISH_REBUILD, {"fish", "fish_kb", "fish_push"}),
                                # 2026-11-11（拆包第二刀）：`fish_schema` 是**最底层**
                                # （纯 DDL 数据），除 stdlib 外谁都不该依赖。
                                (FISH_SCHEMA, {"fish", "fish_kb", "fish_push",
                                               "fish_trust", "fish_rebuild"}),
                                # 2026-11-11（拆包第四刀）：`fish_db`（建表/补列一族）
                                # 只许依赖 fish_common + fish_schema，不许碰上层。
                                (FISH_DB, {"fish", "fish_kb", "fish_push",
                                           "fish_trust", "fish_rebuild"}),
                                # 2026-11-11（序2）：画像模块。只许依赖
                                # fish_common / fish_schema / fish_db（写事件要它）。
                                (FISH_PROFILE, {"fish", "fish_kb", "fish_push",
                                                "fish_trust", "fish_rebuild"}),
                                # 2026-11-20（fish 引入 git · Phase 2）：仓库身份层。
                                # `fish_git` 只许依赖 fish_common / fish_surface /
                                # fish_schema / fish_db（都在它之下）；
                                # `fish_git_schema` 是**纯常量模块**，连 fish_common
                                # 都不该依赖——它必须在 fish_schema 之下才能被拼进 SCHEMA。
                                (FISH_GIT, {"fish", "fish_kb", "fish_push",
                                            "fish_trust", "fish_rebuild", "fish_profile"}),
                                (FISH_GIT_SCHEMA, {"fish", "fish_kb", "fish_push",
                                                   "fish_trust", "fish_rebuild", "fish_profile",
                                                   "fish_schema", "fish_db", "fish_common"}),
                                # 2026-11-25（113 W1b）：会话树 DDL 叶子。同 fish_git_schema：
                                # 它必须在 fish_schema 之下才能被拼进 SCHEMA，所以谁都不许依赖它。
                                (FISH_LINEAGE_SCHEMA, {"fish", "fish_kb", "fish_push",
                                                       "fish_trust", "fish_rebuild",
                                                       "fish_profile", "fish_activity",
                                                       "fish_board", "fish_common",
                                                       "fish_schema", "fish_db", "fish_lineage",
                                                       "fish_cli", "mcp_server"}),
                                # Phase 3 拆出的写库层：依赖 fish_git（向下）+ schema/db，
                                # 但不许碰 fish 本体与任何上层。
                                (FISH_GIT_STORE, {"fish", "fish_kb", "fish_push",
                                                  "fish_trust", "fish_rebuild", "fish_profile"}),
                                # Phase 5 拆出的关联层：只用 fish_common，连 git 层都不碰。
                                (FISH_GIT_LINK, {"fish", "fish_kb", "fish_push", "fish_trust",
                                                 "fish_rebuild", "fish_profile",
                                                 "fish_schema", "fish_db",
                                                 "fish_git", "fish_git_store"}),
                                # 作废层：库函数不碰 fish（CLI 入口里有局部 import，
                                # 那是命令装配，不是库依赖——静态检查只看顶层 import）。
                                (FISH_PURGE, {"fish", "fish_kb", "fish_push", "fish_trust",
                                              "fish_rebuild", "fish_profile",
                                              "fish_git", "fish_git_store", "fish_git_link",
                                              "fish_activity", "fish_board"}),
                                # git 采集（hook 侧）：只用 git / git_store / surface / common。
                                # **不许 import fish_activity**——那会成环。
                                (FISH_GIT_HOOK, {"fish", "fish_kb", "fish_push", "fish_trust",
                                                 "fish_rebuild", "fish_profile",
                                                 "fish_activity", "fish_board",
                                                 "fish_cli", "mcp_server"}),
                                # 身份诊断：**零内部依赖**（只 stdlib）。它不该 import
                                # 任何 fish_* —— 那是它"能单独测、不带上行依赖"的前提。
                                (FISH_IDENTITY_DIAG, {"fish", "fish_kb", "fish_push",
                                                      "fish_trust", "fish_rebuild",
                                                      "fish_profile", "fish_activity",
                                                      "fish_board", "fish_common",
                                                      "fish_schema", "fish_db",
                                                      "fish_cli", "mcp_server"})):
            mods = _imported_modules(ast.parse(_read(path)))
            bad = sorted(m for m in mods if m in forbidden)
            self.assertEqual(bad, [], f"{path.name} 反向依赖了 {bad}")

    def test_j2_no_back_import_runtime_and_both_orders(self):
        """J2（运行时）：干净子进程里 `import fish_kb` 不得把 `fish` 带进来；
        且两个导入顺序（fish→fish_kb / fish_kb→fish）都要能跑通。

        为什么要子进程：同进程里别的测试已经把 `fish` 放进 `sys.modules`，
        "有没有被带进来"就测不出来了——那是测试环境的假象，不是代码性质。
        """
        probe = (
            "import sys\n"
            "import fish_kb\n"
            "assert 'fish' not in sys.modules, 'import fish_kb 把 fish 带进来了'\n"
            "assert 'fish_common' in sys.modules, 'fish_kb 应当依赖 fish_common'\n"
            "print('OK-KB-FIRST')\n"
        )
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", probe],
                              cwd=str(P0), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0,
                         f"反向依赖/导入顺序失败：{proc.stdout}{proc.stderr}")
        self.assertIn("OK-KB-FIRST", proc.stdout)

        probe2 = (
            "import fish\n"
            "import fish_kb\n"
            "assert fish.kb_tree is fish_kb.kb_tree\n"
            "print('OK-FISH-FIRST')\n"
        )
        proc2 = subprocess.run([sys.executable, "-X", "utf8", "-c", probe2],
                               cwd=str(P0), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        self.assertEqual(proc2.returncode, 0,
                         f"先 import fish 再 import fish_kb 失败："
                         f"{proc2.stdout}{proc2.stderr}")

    def test_j3_no_unresolved_globals(self):
        """J3：`fish_kb` / `fish_common` / `fish_push` 里**所有全局引用**都必须在顶层有定义或 import。

        为什么用 `symtable` 而不是"跑一遍"：漏 import 只在**那条分支被走到**时抛
        `NameError`。`kb backup prune` 里少个 `_exc_text`、`doctor` 少个 `shutil`，
        常规测试路径可能一辈子碰不到，而线上第一次碰到就是崩溃。
        `symtable` 能精确区分"全局引用"与"局部变量"，不会把循环变量算进来。

        评审 🟡-8（2026-10-27）：T02 的 `fish_push.py` 恰好踩过"漏 import"的坑
        （`fish_activity`/`fish_naming`/`fish_surface`/`fish_kb`），探针必须覆盖它。
        """

        def missing_of(path):
            table = symtable.symtable(_read(path), path.name, "exec")
            module_names = set(table.get_identifiers())
            missing = {}

            def walk(tbl, scope):
                for sym in tbl.get_symbols():
                    name = sym.get_name()
                    if sym.is_global() and name not in module_names \
                            and not hasattr(builtins, name):
                        missing.setdefault(name, set()).add(scope)
                for child in tbl.get_children():
                    walk(child, child.get_name())

            walk(table, "<module>")
            return {k: sorted(v) for k, v in sorted(missing.items())}

        for path in (FISH_KB, FISH_COMMON, FISH_PUSH):
            self.assertEqual(missing_of(path), {},
                             f"{path.name} 里有全局名既没定义也没 import（运行时才会炸）")

    def test_j5_new_modules_do_not_use_module_identity(self):
        """J5：新模块 0 处 `__file__` / `__name__`（模块级 dunder 的**取值**）。

        为什么禁：`DEFAULT_ROOT = Path(__file__).resolve().parent / "data"` 这种写法
        搬进 `fish_kb` 后指向的是 `p0/`（恰好一样），搬进别处就指向别处——
        它让"这个模块在哪"变成隐式依赖。`type(x).__name__` 不在禁列（那是类型名）。
        """
        for path in (FISH_KB, FISH_COMMON, FISH_PUSH):
            tree = ast.parse(_read(path))
            hits = [node.lineno for node in ast.walk(tree)
                    if isinstance(node, ast.Name) and node.id in ("__file__", "__name__")]
            self.assertEqual(hits, [],
                             f"{path.name} 仍在用模块自身身份（__file__/__name__）：行 {hits}")



# ---------------------------------------------------------------- 外部锚
#
# 第三轮评审证伪了棘轮那 6 条判据：**哈希链由测试自己算**，算法公开、
# 无外部参照——攻击者改完 JSON 重算一遍链就通过了。
# 而 `test_raising_the_window_needs_a_new_entry` 的 docstring 写着
# "重算整条链也会红"，实现却只查 max 是否出现在尾条 new 的数字里——**那句话是假的**。
#
# 修法：把**期望的链头与长度写死在测试里**。这样改历史要同时改两个文件
# （JSON + 这个字面量），而改测试文件在评审里藏不住。
#
# 这不是密码学意义上的防篡改（人能重算并改字面量），但它把"改一个数字"
# 变成"必须动一个看起来就不是数据文件的测试文件"。**诚实地说，这是威慑，不是证明。**
EXPECTED_CHAIN_HEAD = "57dd658c701d6a61ae38bc9eef5368be1440aa2e49e6d7ccbaa7909e0a83a71c"
EXPECTED_HISTORY_LEN = 8
# 与上面链头同一时刻的 max。新增历史前，max 不许偏离它。
EXPECTED_RATCHET_MAX_AT_ANCHOR = 11200

class J4RatchetTest(unittest.TestCase):
    """J4 窗口的**棘轮**（F-P1-10，2026-11-18 外部审查）。

    审查方对 J4 的批评不是"它守错了东西"，而是"它只守行数、可以被抬窗"——
    也就是说：真正的门是**没有人会去抬窗**这个约定，而约定不是判据。

    这里把约定变成三条能红的东西：
      ① 数字只有一个出处（`j4_ratchet.json`），测试里不再写死；
      ② 每一次改动都必须在 history 里留下**非空理由**——抬窗可以，但必须留话；
      ③ 最新一条历史的 `new` 必须与当前值一致——不许改数字不改历史。
    """

    RATCHET = Path(__file__).resolve().parent / "j4_ratchet.json"   # 见上方 🟠-3 说明

    def _data(self):
        return json.loads(self.RATCHET.read_text(encoding="utf-8"))

    def test_every_change_carries_a_reason(self):
        """**核心**：没有理由的改动算违规。

        抬窗本身不禁止（有时候确实必须抬），但"顺手调大"不允许。
        """
        d = self._data()
        self.assertTrue(d.get("history"), "history 为空：连第一次登记都没有")
        bad = [h for h in d["history"]
               if not str(h.get("reason") or "").strip()]
        self.assertEqual(bad, [], f"这些改动没有写理由：{bad}")

    def test_latest_history_matches_current_values(self):
        """改了数字却没记历史 → 红。数字与历史必须同步。"""
        d = self._data()
        last = d["history"][-1]
        self.assertEqual(str(last["new"]), f'{d["min"]},{d["max"]}',
                         f"最新历史 {last['new']} 与当前值 {d['min']},{d['max']} 不一致")

    def test_window_is_sane(self):
        d = self._data()
        self.assertLess(d["min"], d["max"], "min 必须小于 max")
        real = len((Path(__file__).resolve().parent / "fish.py")
                   .read_text(encoding="utf-8").split("\n")) - 1
        self.assertGreaterEqual(real, d["min"], f"fish.py 只有 {real} 行，低于窗口下限")
        self.assertLessEqual(real, d["max"], f"fish.py 有 {real} 行，超出窗口上限")

    def test_no_hardcoded_window_in_tests(self):
        """反向判据：测试文件里不许再出现写死的窗口数字。

        否则"单一出处"只是说说：写死的那个照样能覆盖 JSON。
        """
        src = (Path(__file__).resolve().parent / "test_split_contract.py").read_text(
            encoding="utf-8")
        import re as _re
        hard = _re.findall(r"^FISH_LINES_(?:MIN|MAX)\s*=\s*\d+", src, _re.M)
        self.assertEqual(hard, [], f"仍然写死了窗口数字：{hard}")


    def test_history_is_append_only(self):
        """**核心**：history 是哈希链，就地改任何一条都会断链。

        外部审查 🟡-1 实测的绕过方式：把 `max` 抬到 14000，同时**就地改掉
        最后一条 history 的 `new`**（不新增记录、理由不动）→ 原来那 4 条判据全绿。
        "每次改动必须写理由"因此只挡住了"不写理由的人"，挡不住"懒得写就改旧的"。

        这条把"改旧的"变成可检测：每条 history 的 sha256 串着上一条，
        改一条就断一片。
        """
        import hashlib as _hl
        d = self._data()
        prev = ""
        for idx, h in enumerate(d["history"]):
            raw = "|".join([prev, str(h.get("date")), str(h.get("field")),
                            str(h.get("old")), str(h.get("new")), str(h.get("reason"))])
            want = _hl.sha256(raw.encode("utf-8")).hexdigest()
            self.assertEqual(
                h.get("sha256"), want,
                f"history 第 {idx} 条的 sha256 对不上——这一条被就地改过。\n"
                f"  记录 {h.get('sha256')}\n  实算 {want}")
            prev = want

    def test_chain_head_matches_the_pinned_anchor(self):
        """**核心**：链头必须等于测试里写死的那个值。

        第三轮评审实测的绕过：改 `max` → 就地改尾条 `new` → **重算整条链** → 6 条全绿。
        原因是链由测试自己算，没有任何外部参照。

        现在链头钉在测试文件里。要绕过就得同时改 JSON 和这个字面量——
        而后者在评审里一眼能看见（它是测试文件，不是数据文件）。
        """
        d = self._data()
        prev = ""
        for h in d["history"]:
            raw = "|".join([prev, str(h.get("date")), str(h.get("field")),
                            str(h.get("old")), str(h.get("new")), str(h.get("reason"))])
            prev = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        self.assertEqual(prev, EXPECTED_CHAIN_HEAD,
                         "history 链头与测试里钉的值不一致——历史被改过。\n"
                         "  如果你的改动是正当的，请同时更新 EXPECTED_CHAIN_HEAD；\n"
                         "  那次更新会在评审里被看见，这正是它存在的意义。")

    def test_history_length_never_shrinks(self):
        """历史只许变长。删条目（不是追加）会红。

        与链头互补：链头防"改内容"，长度防"删记录"。
        只抬窗不加记录的绕过会在长度这条红。
        """
        d = self._data()
        self.assertGreaterEqual(
            len(d["history"]), EXPECTED_HISTORY_LEN,
            f"history 从 {EXPECTED_HISTORY_LEN} 条变成了 {len(d['history'])} 条——"
            "只能追加，不能删")

    def test_raising_the_window_needs_a_new_entry(self):
        """抬窗必须**新增**一条历史记录。

        第三轮评审指出原版 docstring 声称"重算整条链也会红"是**假的**：
        实现只查 `max` 是否出现在尾条 `new` 的数字里，改尾条即通过。
        现在改成查**长度必须增长**——改尾条不会让长度变长。
        """
        d = self._data()
        last = d["history"][-1]
        recorded = sorted(int(x) for x in
                          re.findall(r"(\d{4,})", str(last["new"])))
        self.assertIn(d["max"], recorded,
                      f"当前 max={d['max']} 不在最新一条历史 {last['new']} 里"
                      "——抬了窗却没记新账")
        if len(d["history"]) == EXPECTED_HISTORY_LEN:
            self.assertEqual(
                d["max"], EXPECTED_RATCHET_MAX_AT_ANCHOR,
                "在**没新增历史**的情况下 max 变了——抬窗必须写一条新记录")



class SplitSizeTest(unittest.TestCase):
    """J4：体量窗口与例外清单。"""

    def test_j4_line_count_within_window(self):
        """J4：`fish.py` 行数落在窗口内；`__all__` 规模合理。

        窗口的作用是"整段搬运漏了一段/多搬了一段"要红。行数口径与设计一致：
        `len(fish.py 文本.split("\\n"))`（末尾换行也算一行）。
        """
        lines = len(_read(FISH).split("\n"))
        self.assertGreaterEqual(lines, FISH_LINES_MIN,
                                f"fish.py 只有 {lines} 行：可能多搬走了东西")
        self.assertLessEqual(lines, FISH_LINES_MAX,
                             f"fish.py 有 {lines} 行：可能漏搬了东西")
        push_lines = len(_read(P0 / "fish_push.py").split("\n"))
        self.assertGreaterEqual(push_lines, FISH_PUSH_LINES_MIN,
                                f"fish_push.py 只有 {push_lines} 行：搬漏了")
        self.assertLessEqual(push_lines, FISH_PUSH_LINES_MAX,
                             f"fish_push.py 有 {push_lines} 行：长回去了")
        # 新模块（2026-10-29 抽出的融合/排序层）自己的窗口：不许再长成第二个巨石
        rank_lines = len(_read(P0 / "fish_push_rank.py").split("\n"))
        self.assertGreaterEqual(rank_lines, FISH_PUSH_RANK_LINES_MIN,
                                f"fish_push_rank.py 只有 {rank_lines} 行：搬漏了")
        self.assertLessEqual(rank_lines, FISH_PUSH_RANK_LINES_MAX,
                             f"fish_push_rank.py 有 {rank_lines} 行：长回去了")
        common_lines = len(_read(FISH_COMMON).split("\n"))
        self.assertGreaterEqual(common_lines, FISH_COMMON_LINES_MIN,
                                f"fish_common.py 只有 {common_lines} 行：下沉漏了")
        self.assertLessEqual(common_lines, FISH_COMMON_LINES_MAX,
                             f"fish_common.py 有 {common_lines} 行：长回去了")
        # 新模块（2026-11-11 抽出：身份锚授权 + 归属合并链解析）
        trust_lines = len(_read(P0 / "fish_trust.py").split("\n"))
        self.assertGreaterEqual(trust_lines, FISH_TRUST_LINES_MIN,
                                f"fish_trust.py 只有 {trust_lines} 行：搬漏了")
        self.assertLessEqual(trust_lines, FISH_TRUST_LINES_MAX,
                             f"fish_trust.py 有 {trust_lines} 行：长回去了")
        # 新模块（2026-11-11 抽出：rebuild_projection）
        rebuild_lines = len(_read(P0 / "fish_rebuild.py").split("\n"))
        self.assertGreaterEqual(rebuild_lines, FISH_REBUILD_LINES_MIN,
                                f"fish_rebuild.py 只有 {rebuild_lines} 行：搬漏了")
        self.assertLessEqual(rebuild_lines, FISH_REBUILD_LINES_MAX,
                             f"fish_rebuild.py 有 {rebuild_lines} 行：长回去了")
        schema_lines = len(_read(P0 / "fish_schema.py").split("\n"))
        self.assertGreaterEqual(schema_lines, FISH_SCHEMA_LINES_MIN,
                                f"fish_schema.py 只有 {schema_lines} 行：搬漏了")
        self.assertLessEqual(schema_lines, FISH_SCHEMA_LINES_MAX,
                             f"fish_schema.py 有 {schema_lines} 行：长回去了")
        prof_lines = len(_read(P0 / "fish_profile.py").split("\n"))
        self.assertGreaterEqual(prof_lines, FISH_PROFILE_LINES_MIN,
                                f"fish_profile.py 只有 {prof_lines} 行：搬漏了")
        self.assertLessEqual(prof_lines, FISH_PROFILE_LINES_MAX,
                             f"fish_profile.py 有 {prof_lines} 行：长回去了")
        db_lines = len(_read(P0 / "fish_db.py").split("\n"))
        self.assertGreaterEqual(db_lines, FISH_DB_LINES_MIN,
                                f"fish_db.py 只有 {db_lines} 行：搬漏了")
        self.assertLessEqual(db_lines, FISH_DB_LINES_MAX,
                             f"fish_db.py 有 {db_lines} 行：长回去了")
        # 2026-11-20（本轮）：窗口覆盖补全——这几个模块此前没有上界。
        for module, low, high in (
                ("fish_board.py", FISH_BOARD_LINES_MIN, FISH_BOARD_LINES_MAX),
                ("fish_inject.py", FISH_INJECT_LINES_MIN, FISH_INJECT_LINES_MAX),
                ("fish_cli.py", FISH_CLI_LINES_MIN, FISH_CLI_LINES_MAX),
                ("fish_activity.py", FISH_ACTIVITY_LINES_MIN, FISH_ACTIVITY_LINES_MAX),
                ("fish_visibility.py", FISH_VISIBILITY_LINES_MIN, FISH_VISIBILITY_LINES_MAX),
                ("fish_naming.py", FISH_NAMING_LINES_MIN, FISH_NAMING_LINES_MAX),
                ("fish_surface.py", FISH_SURFACE_LINES_MIN, FISH_SURFACE_LINES_MAX),
                # 2026-11-20（fish 引入 git · Phase 2）：仓库身份层两个新模块
                ("fish_git.py", FISH_GIT_LINES_MIN, FISH_GIT_LINES_MAX),
                ("fish_git_schema.py", FISH_GIT_SCHEMA_LINES_MIN, FISH_GIT_SCHEMA_LINES_MAX),
                # 2026-11-25（113 W1b）：会话树 DDL 叶子
                ("fish_lineage_schema.py", FISH_LINEAGE_SCHEMA_LINES_MIN,
                 FISH_LINEAGE_SCHEMA_LINES_MAX),
                # 2026-11-25（113 W2/W3）：会话树投影层本体（懒加载注册表 + depth 重算）
                ("fish_lineage.py", FISH_LINEAGE_LINES_MIN, FISH_LINEAGE_LINES_MAX),
                ("fish_git_store.py", FISH_GIT_STORE_LINES_MIN, FISH_GIT_STORE_LINES_MAX),
                ("fish_git_link.py", FISH_GIT_LINK_LINES_MIN, FISH_GIT_LINK_LINES_MAX),
                ("fish_purge.py", FISH_PURGE_LINES_MIN, FISH_PURGE_LINES_MAX),
                ("fish_git_hook.py", FISH_GIT_HOOK_LINES_MIN, FISH_GIT_HOOK_LINES_MAX),
                ("fish_identity_diag.py", FISH_IDENTITY_DIAG_LINES_MIN,
                 FISH_IDENTITY_DIAG_LINES_MAX)):
            count = len(_read(P0 / module).split("\n"))
            self.assertGreaterEqual(count, low, f"{module} 只有 {count} 行：搬漏了")
            self.assertLessEqual(count, high, f"{module} 有 {count} 行：长回去了")
        self.assertGreater(len(fish_kb.__all__), 100,
                           "fish_kb 的导出面太小，明显漏名")
        self.assertGreaterEqual(len(_read(FISH_KB).split("\n")), 2500,
                                "fish_kb 太小：看起来没搬全")

    def test_j4_push_candidates_lives_in_fish_push(self):
        """J4：`_kb_push_candidates` 现在**属于 fish_push**（T02 拆包），fish 侧靠 re-export 可用。

        历史（第一轮拆包）：它当时刻意留在 fish.py（设计裁决 5：第二轮随 `fish_push` 改名
        再一起动）。T02 就是那一轮——所以判据随之迁移：
        · `fish_push._kb_push_candidates` 必须存在；
        · `fish_kb` 里必须没有；
        · `fish.py` 的**顶层定义**里不能再有它（只在 fish.py 顶部 re-export）。
        """
        import fish_push
        self.assertTrue(hasattr(fish_push, "_kb_push_candidates"),
                        "_kb_push_candidates 不在 fish_push 里（推送管线会断）")
        self.assertFalse(hasattr(fish_kb, "_kb_push_candidates"),
                         "_kb_push_candidates 被搬进 fish_kb 了（不该）")
        top = _defined_names(ast.parse(_read(FISH)))
        self.assertNotIn("_kb_push_candidates", top,
                         "fish.py 顶层仍定义 _kb_push_candidates（应只在 fish_push）")


class PushModuleContractTest(unittest.TestCase):
    """J6/J7/J8：`fish_push` 的拆分契约（T02，2026-10-27）。"""

    def test_j6_push_module_does_not_import_fish(self):
        """J6：`fish_push` 不许 import fish（干净子进程验证，防循环依赖）。"""
        code = (
            "import sys\n"
            f"sys.path.insert(0, r'{P0}')\n"
            "import fish_push\n"
            "assert 'fish' not in sys.modules, 'fish_push 把 fish 带进来了'\n"
            "print('ok')\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, cwd=str(P0), timeout=120)
        self.assertEqual(proc.returncode, 0, f"J6 失败：{proc.stderr[-800:]}")

    def test_j7_reexport_surface(self):
        """J7：`fish_push.__all__` 每个名字在 fish 侧都在且是同一对象；fish.py 顶层不再定义。"""
        import fish_push
        self.assertGreater(len(fish_push.__all__), 25, "fish_push.__all__ 太小（漏名）")
        for name in fish_push.__all__:
            self.assertTrue(hasattr(fish, name), f"fish 缺 re-export：{name}")
            self.assertIs(getattr(fish, name), getattr(fish_push, name),
                          f"{name} 不是同一个对象（re-export 断了）")
        top = _defined_names(ast.parse(_read(FISH)))
        for name in ("build_push_candidates", "select_push_candidates",
                     "build_prompt_candidates", "build_push_block",
                     "mark_push_accepted", "mark_push_accepted_prefix",
                     "_kb_push_candidates", "_conv_push_candidates"):
            self.assertNotIn(name, top, f"{name} 仍在 fish.py 顶层定义（应只在 fish_push）")

    def _git_blob(self, rev, path):
        """取某次提交时的文件内容（None = 取不到）。"""
        try:
            out = subprocess.run(
                ["git", "-C", str(P0.parent), "show", f"{rev}:{path}"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120)
            return out.stdout if out.returncode == 0 and out.stdout.strip() else None
        except Exception:      # noqa: BLE001
            return None

    def test_j8_function_bodies_semantically_identical(self):
        """J8：T02 拆包前后**公共函数/类定义** AST 等价（评审 🟠-4/5 的收口）。

        **基准是 T02 提交那一刻**（`da8d24f` 及其父），不是"现在"——否则 T03/T04/T05/B
        的后续改动会被误算成"搬运违例"。
        优先本地备份；没有则从 git blob 取（发布树无 `历史版本/`，评审 🟠-4）。
        比对**全交集**（实测 386 个定义），而不是只比 12 个搬走的函数（评审 🟠-5）。

        **覆盖边界（外部审查 🔴12 的收口，2026-10-28 晚明确）**：
          覆盖 —— 顶层 `FunctionDef/AsyncFunctionDef/ClassDef` 的 **body**（含函数体、
                  嵌套函数与全部语句）；这是"搬运没改行为"的主证据。
          不覆盖 —— 模块级 `Import/ImportFrom`、`Assign/AnnAssign`、`If/Try`（条件定义）。
                  拆包**必然**改 import（`fish.X` → 直接 import）与常量来源，逐字节比会把
                  "正确的拆包"判成违例；这些由 J1b（re-export 清单双向相等）、J7（同一
                  对象）、J2/J6（依赖方向）与 1,587 条行为判据兜底。
          一句话：**J8 保证"函数与类的定义等价"，不保证"整个模块字节等价"**——引用它时
          必须带上这句限定（审查官指出：一个连审查者都会误读的判据，其保证力不应被夸大）。
        """
        pre = P0 / "历史版本" / "fish.py.20261027-t02-pre.bak"
        if pre.exists():
            old_src, origin = pre.read_text(encoding="utf-8", errors="replace"), "backup"
        else:
            old_src, origin = self._git_blob("da8d24f^", "p0/fish.py"), "git:da8d24f^"
        new_srcs = [self._git_blob("da8d24f", p)
                    for p in ("p0/fish.py", "p0/fish_push.py", "p0/fish_common.py")]

        # 2026-11-11（外部审查 🔴12 的收口）：此前这条判据**永远 SKIP**——它要的
        # `da8d24f`（T02 提交）已经不在了（仓库历史上被 force-push 过，那个对象
        # 不在任何 ref 里），而发布树又没有 `历史版本/` 备份。
        #
        # T02 那一刀的**等价提交还在**：全分支 bundle 里的 `bc69ef7`，提交信息原文就是
        # "fish.py 15,464->13,980"，与文档记载一致；其父 `bc69ef7^` 就是 T02 前版本。
        # 已把**这一对**导出成随仓库分发的 fixture（`test-fixtures/t02/*.gz`，共 546 KB），
        # 于是**任何 clone 都能真判**，不再依赖本机历史或网络。
        #
        # 优先级：fixture → 本地备份 → git blob → skip。用同一刀的 pre/post **配对**，
        # 比"一个来自找不到的提交、一个来自别处"更可靠——判据证的是"这次搬运没改函数体"，
        # 配对必须是同一次搬运。
        fx = P0 / "test-fixtures" / "t02"
        fx_names = ("t02-pre.fish.py.gz", "t02-post.fish.py.gz",
                    "t02-post.fish_push.py.gz", "t02-post.fish_common.py.gz")
        if all((fx / n).exists() for n in fx_names):
            def _read_gz(name):
                with gzip.open(fx / name, "rt", encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            old_src = _read_gz(fx_names[0])
            new_srcs = [_read_gz(n) for n in fx_names[1:]]
            origin = "fixture:test-fixtures/t02（源自 bundle 的 bc69ef7 配对）"
        elif not all(new_srcs):
            post_dir = P0 / "历史版本" / "t02-post"
            fallback = []
            for f in ("fish.py", "fish_push.py", "fish_common.py"):
                p = post_dir / (f + ".bak")
                fallback.append(p.read_text(encoding="utf-8", errors="replace")
                                if p.exists() else None)
            if all(fallback):
                new_srcs = fallback

        if not old_src:
            self.skipTest("T02 前版本不可达（fixture/备份/git 都没有）——判不到，不是通过")
        if not all(new_srcs):
            self.skipTest("T02 提交快照取不到（fixture/备份/git 都没有）——判不到，不是通过")
        old_defs = {}
        for node in ast.parse(old_src).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                old_defs[node.name] = node
        new_defs = {}
        for src in new_srcs:
            for node in ast.parse(src).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    new_defs[node.name] = node
        # 已登记差异（每条必须有原因；T02 报告 §2 的六类副作用，逐条对应）
        known_diff = {
            # 跨模块引用改写：`fish.X` → 直接 import 的 X（拆包必需）
            "_kb_push_candidates": "跨模块引用改写（kb_vault_list/_kb_search_multi 改为直接 import）",
            "_conv_push_candidates": "跨模块引用改写（fish_activity/fish_corpus 改为直接 import）",
            "build_prompt_candidates": "跨模块引用改写（内部 helper 已搬出）",
            "build_push_candidates": "跨模块引用改写（内部 helper 已搬出）",
            "select_push_candidates": "跨模块引用改写（内部 helper 已搬出）",
            "build_push_block": "跨模块引用改写（内部 helper 已搬出）",
            "clean_prompt": "函数内 import fish_activity（跨模块调用，不 import fish）",
        }
        common = sorted(set(old_defs) & set(new_defs))
        self.assertGreaterEqual(len(common), 380,
                                f"只找到 {len(common)} 个公共定义（实测应 386）：{origin}")
        changed = []
        for name in common:
            if name in known_diff:
                continue
            a = ast.dump(ast.Module(body=old_defs[name].body, type_ignores=[]),
                         annotate_fields=False)
            b = ast.dump(ast.Module(body=new_defs[name].body, type_ignores=[]),
                         annotate_fields=False)
            if a != b:
                changed.append(name)
        self.assertEqual(changed, [],
                         f"这些定义在 T02 拆分中变了（且未登记原因）：{changed[:10]}（来源 {origin}）")

    def test_unread_mem_types_are_registered(self):
        """**F-P2-2**：写进去却没人读的记忆类型，必须**登记在代码里**。
        外部审查指出 `identity` 这个 mem_type 没有任何读取路径。
        核实：设计文档 `03-规格补充与修订-v0.2.md:142` 说它「L0 常驻」，
        但 L0 段是从 `citizens` 表建的，`code_index` 里 identity 行
        **一次都没被读过**（全仓查询 0 处）。
        判据守的不是"必须接线"（那是产品决定），而是：
        **代码里必须写着它没接线**，以及接线需要什么。
        没有这句话，读代码的人会以为写一条 identity 记忆就能影响 L0——
        而它实际只写不读。
        """
        src = (P0 / "fish.py").read_text(encoding="utf-8")
        self.assertIn("F-P2-2", src,
                      "fish.py 里没有 F-P2-2 的登记说明")
        # **第七轮评审 🟠**：这条原来断言"identity` 无读取路径"——
        # 而那句话是**错的**（`search_memories`/`list_memories`/`read_body_path`
        # 都读得到 identity）。判据把假话锁死了：谁改成真话它就红。
        # 现在断言的是**真话**：不进 L0、不进注入包。
        self.assertIn("不进 L0", src,
                      "登记说明没写清「不进 L0 / 不进注入包」这件事")
        self.assertIn("按 identity 特殊处理", src,
                      "没写清准确口径：是没有专门处理，不是读不到")
        self.assertIn("定 identity 与 citizens.display_name 的优先级", src,
                      "没写接线需要先定什么")
        # 反向：**错的措辞不许回潮**
        self.assertNotIn("identity` 无读取路径", src,
                         "「无读取路径」是错的——普通检索读得到它，"
                         "别再把这句话写回去")


if __name__ == "__main__":
    unittest.main(verbosity=2)
