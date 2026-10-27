#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish MCP server — dual-era 实现（零依赖 · JSON-RPC 2.0 over stdio）
===================================================================
规范依据（2026-10-22 核对）：
  - MCP 最新版：2026-07-28（https://modelcontextprotocol.io/specification/latest）
  - 该版取消 initialize 握手，改为 per-request `_meta` + `server/discover`
  - 同时兼容 legacy（2025-11-25 及更早的 initialize 握手）→ dual-era
  - stdio：换行分隔 JSON-RPC，stdout 只许协议消息，日志走 stderr
  - 所有 result 必带 resultType；列表类必带 ttlMs / cacheScope
  - 版本不支持 → -32022 UnsupportedProtocolVersionError（带 supported 列表）
  - 资源不存在 → -32602（2026-07-28 起从 -32002 改）

为什么手写而不装 SDK：
  mcp 2.3.0 会把 starlette 升到 1.9.x，与 fastapi 0.112 的 <0.38 冲突，
  可能破坏本机其他 Python 项目（已实测并回滚）。stdio 协议层成本低于依赖风险。

身份（P1-c，2026-10-24）：**本进程里没有任何"当前身份"状态。**
------------------------------------------------------------------
2026-10-23 23:01:55 的真实事故：审计里出现 `actor=fish-main, session_actor=fish-主AI`，
而那个会话名是**另一个会话**在 17:36:06 调 register 时写进本进程的。根因不是代码马虎，
是结构错了：**DSH 一个宿主只起一个 MCP server 进程（pid 37192），所有会话共享它**——
于是任何"进程内存里的当前身份"都只能等于"最后一个 register 的人"。

所以本轮把 `self._actor` / `self._session_actor` 整类状态删掉，改为**每次调用带 token**：
  1. 工具入参多一个**可选** `token`（旧调用不带也必须照旧能跑完 → required 一个字都不加）；
  2. token → `session_tokens` 查表（唯一判定入口 `fish.validate_session_token`）；
  3. 不带 token → `owner='unattributed'`（明确的"未归属"）+ 固定提示，**绝不冒充别人**
     （评审 R-2 的教训：没有证据时编一个身份，比没有身份糟得多）；
  4. whoami 无 token → 只说"身份未核"，不再回答"我是谁"。

依据：`18-身份层实现设计-P1b.md` §7、`16-最终架构-v1-整合裁决.md` §2.2。
"""
import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fish  # noqa: E402
import fish_corpus  # noqa: E402

MODERN_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2024-11-05"]
SERVER_INFO = {"name": "fish", "version": "0.3.0"}
META_PREFIX = "io.modelcontextprotocol/"

# ------------------------------------------------------------ 身份：每次调用带 token
# 归属缺失时的**明确取值**（不是 None、不是空串）：库里读出来的 owner 必须能自解释，
# 空值会让"没归属"和"老数据没这列"看起来一模一样。
UNATTRIBUTED = "unattributed"

# 一次调用能落到哪一档身份。valid 以外的每一档都有自己的提示文案，
# 因为它们对应的**下一步动作**不同（换新凭证 / 别用这条 / 补带 token）。
IDENTITY_VALID = "valid"          # token 有效 → 有公民
IDENTITY_NONE = "none"            # 没带 token
IDENTITY_INVALID = "invalid"      # 带了，但表里查不到（假 token / 打错字）
IDENTITY_REVOKED = "revoked"      # 带了，表里有，但会话已 end（revoked_at 非空）
IDENTITY_EXPIRED = "expired"      # 带了，表里有，但已过期（expires_at 已过）

# 一次调用的身份解析结果。**纯值对象**：不挂在 self 上，函数返回就不存在了——
# 这正是本轮要的"进程里没有身份状态"。第 4 个字段 status 单独存在，是因为
# "没带" 与 "带了但无效" 要给出不同的下一步提示（设计稿的三元组里第 3 位
# 在无效分支装的是状态字符串，这里拆成 evidence + status，语义才不混）。
Identity = collections.namedtuple("Identity", "citizen_id session_key evidence status")

IDENTITY_ANONYMOUS = Identity(None, None, None, IDENTITY_NONE)
IDENTITY_REJECTED = Identity(None, None, None, IDENTITY_INVALID)

# 归属提示（三种状态各一条，措辞固定：判据 B/C/D 直接认这几句）。
# 2026-10-25 改（外部评估 `EVAL-261025-04` / `-05`）：
#   原文只有一句「请先跑 fish session start」——而**MCP 面根本没有这个工具**，
#   AI 只能离开 MCP 去跑 CLI；更糟的是 CLI 那边的会话键与 MCP 不同，
#   不带 `--citizen` 会**新建一个公民**，归属从此对不上且无法合并。
#   现在把最短路径写全：**提示必须是可执行的下一步，而不是一个可能错误的归因**。
IDENTITY_HINTS = {
    IDENTITY_NONE: (
        '⚠ 本次调用未带会话凭证，归属记为“未归属”（unattributed）。\n'
        "   拿凭证：**直接调 `session_start` 工具**（本进程内即可，不用跑 CLI）：\n"
        "     · 已登记过 → session_start(citizen=\"<你的 citizen_id>\", "
        "session_name=\"<稳定名字>\")\n"
        "     · 还没登记 → 先 register 拿 citizen_id，再按上面调\n"
        "   拿到 token 后，之后每次调用带 token=...（明文只出现一次）。\n"
        "   ⚠ 为什么不建议跑 CLI：CLI 的会话键与 MCP 不同，"
        "会建成另一个公民，两条通道的归属从此对不上且无法合并。"),
    IDENTITY_INVALID: (
        '⚠ 凭证无效（session_tokens 表里没有这条 token），归属记为“未归属”（unattributed）。\n'
        "   调 `session_start`（带同一个 session_name）换一条新凭证：公民不变，只换 token。"),
    IDENTITY_REVOKED: (
        '⚠ 凭证已失效（被 `fish session end` 吊销），归属记为“未归属”（unattributed）。\n'
        "   调 `session_start`（带同一个 session_name）换一条新凭证：公民不变，只换 token。"),
    IDENTITY_EXPIRED: (
        '⚠ 凭证已失效（超过 TTL 自动过期），归属记为“未归属”（unattributed）。\n'
        "   调 `session_start`（带同一个 session_name）换一条新凭证：公民不变，只换 token。"),
}

# 已经自己讲清身份状况的工具：不再追加通用提示（否则同一句话出现两遍）。
# `session_start` 加进来的理由：它的全部产出就是"身份状况 + 凭证"，
# 再追一句"你没带凭证"就成了自我矛盾。
HINT_SELF_EXPLAINED = frozenset({"whoami", "register", "session_start"})

# 工具入参名。常量化的理由：11 个工具的 token 必须**同名同义**，
# 手抄 11 遍就一定会有一份写成 credential / session_token。
TOKEN_ARG = "token"
TOKEN_PROPERTY = {
    "type": "string",
    "description": ("可选：会话凭证（由 `fish session start` 签发）。带上它，本次调用"
                    "的归属才确定；不带则归属记为 'unattributed'（未归属），"
                    "系统不会猜你的身份。"),
}

# T05：知识库工具面的词表与回执头（`docs/架构设计-B1B2B3-v0.1.md` §3.3）。
# 与 TOKEN_ARG 同理：词表散在 if/elif 里时，"少写一个分支"会变成运行期才发现的失败，
# 而集中成常量后，新加一个 action 只改一处、判据也只认这一处。
KB_TOOL_ACTIONS = ("vaults", "tree", "open", "search")
KB_WRITE_ACTIONS = ("create", "append", "update")
KB_SOURCE_LINE = "source=知识库（未建索引）"   # 回执第 1 行（判据认这句字面量）
KB_TOOL_MAX_CHARS = 8000                      # 与 `kb` schema 的 max_chars 默认值同源

# T03：对话语料的**只读**面。三个动作刻意**不含** ingest/forget/rebuild——
# 扫盘采集与删除都会改数据面，只给 CLI（人来敲），模型不该随手触发。
CORPUS_TOOL_ACTIONS = ("search", "read", "stats")
CORPUS_SOURCE_LINE = "source=语料（corpus.sqlite · 原文投影，已脱敏）"


def _attach_token(tools):
    """给每个工具补上**可选** token 入参，然后原样返回工具表。

    两条纪律：
      · 只写 properties，**绝不碰 required** —— 旧调用不带 token 必须照旧能跑完；
      · 已存在 token 字段就直接报错（不是幂等覆盖）：那说明有人手抄了一份定义，
        而"两份 token 定义"迟早会有一份语义不同。
    """
    for tool in tools:
        props = tool["inputSchema"].setdefault("properties", {})
        if TOKEN_ARG in props:
            raise RuntimeError(f"工具 {tool['name']} 已经有 {TOKEN_ARG} 入参了："
                               "token 只允许由 _attach_token 统一补")
        props[TOKEN_ARG] = dict(TOKEN_PROPERTY)
    return tools

TOOLS = [
    {
        "name": "remember",
        "title": "记录记忆",
        "description": "记录一条记忆。编码由系统分配，不要自己编。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目前缀，如 ANYI"},
                "title": {"type": "string", "description": "一句话标题（≤80 字）"},
                "body": {"type": "string", "description": "正文（Markdown）"},
                "mem_type": {"type": "string",
                             "enum": ["identity", "rule", "fact", "episode", "lesson", "artifact"],
                             "description": "记忆类型；rule/identity 永不衰减"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "cause": {"type": "string", "description": "为什么记这条"},
                "occurred_at": {
                    "type": "string",
                    "description": "这件事什么时候发生（ISO8601 UTC，如 2026-10-22T04:00:00Z）；"
                                   "缺省=现在。用于补记过去的事，与记录时间区分",
                },
                "supersedes": {
                    "type": "array", "items": {"type": "string"},
                    "description": "可选：这条新记忆替代哪些旧编码（旧条目会被标记 superseded）",
                },
                "attachments": {
                    "type": "array",
                    "description": "附件（图片/文档等）的本地绝对路径；"
                                   "系统会复制进 blobs/ 并按 SHA256 去重",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "本地绝对路径"},
                            "caption": {"type": "string", "description": "说明文字"},
                        },
                        "required": ["path"],
                    },
                },
            },
            "required": ["project", "title"],
        },
    },
    {
        "name": "recall",
        "title": "检索记忆",
        "description": "按编码 / 标题 / 正文关键词检索（关键词匹配，非语义）。"
                       "召回会刷新衰减（被想起即唤醒）。回包标注匹配来源。\n"
                       "不给 query/code 而给 project 时走**列出模式**：按项目（含别名展开）"
                       "+ tags 过滤、时间倒序，首行声明“按什么过滤、共几条”。\n"
                       "只给 tags 不报错，会回一句引导（tags 只作附加过滤，需要 project 限定）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "编码、编码片段、标题或正文关键词；支持中文子串"},
                "code": {"type": "string", "description": "精确编码"},
                "project": {"type": "string",
                            "description": "只查该项目前缀；不给 query/code 时=列出该项目全部条目"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "标签过滤（需同时满足）；**只作附加过滤**，"
                                        "单独用会回引导（它没有范围可列）"},
                "scope": {"type": "string", "enum": ["all", "title", "body"],
                          "default": "all", "description": "检索范围，默认 all"},
                "limit": {"type": "integer", "default": 10,
                          "description": "检索默认 10；列出模式默认 20（不传则用各自的默认）"},
                "include_superseded": {"type": "boolean", "default": False,
                                       "description": "连已取代/已作废的条目一起给（默认只给 active）"},
            },
            "required": [],
        },
    },
    {
        "name": "neighbors",
        "title": "邻近回忆",
        "description": "给定编码，返回上下文相邻的记忆。策略：code/time/tag/type/mixed。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "n": {"type": "integer", "default": 3},
                "strategy": {"type": "string",
                             "enum": ["code", "time", "tag", "type", "mixed"]},
            },
            "required": ["code"],
        },
    },
    {
        "name": "resolve_code",
        "title": "校验编码",
        "description": "校验并解释一个编码：存在吗、是什么、衰减到哪一步。",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "read_body",
        "title": "读记忆正文",
        "description": "按编码读取一条记忆的完整正文（含附件清单）。"
                       "resolve_code 只回元数据；要看内容用这个。",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "记忆编码，如 ANYI-261022-10"}},
            "required": ["code"],
        },
    },
    {
        "name": "add_project",
        "title": "注册项目前缀",
        "description": "注册新的项目前缀（namespace）。注册后才能用该前缀 remember。"
                       "前缀不能含 '-'，长度 ≤20。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "前缀，如 ANYI / 企业智能"},
                "namespace": {"type": "string", "description": "可读名称，缺省同前缀"},
            },
            "required": ["prefix"],
        },
    },
    {
        "name": "supersede",
        "title": "标记替代",
        "description": "把旧记忆标记为被新记忆替代。之后 recall 默认不再返回旧条目，"
                       "resolve_code 会提示去看新的。用于同主题两条并存的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "旧的（要被替代的）编码"},
                "by": {"type": "string", "description": "新的（替代它的）编码"},
                "reason": {"type": "string", "description": "为什么替代"},
            },
            "required": ["code", "by"],
        },
    },
    {
        "name": "whoami",
        "title": "我是谁",
        "description": "返回**你**的身份（系统签发的行动者身份）与系统主人。"
                       "注意：返回的是当前行动者，不是主人；不要自称主人。\n"
                       "回执固定带一行「身份来源」：token（session_tokens）/ "
                       "register 登记（不参与归属）/ 未归属——"
                       "它回答的是“这条回执凭什么算身份”，不是“你登记过没有”。\n"
                       "`all=true` 时另列**相关公民**（同会话键 / 同名 / 同账号，三者去重），"
                       "用于排查“同一个 AI 在库里有两套身份”——那不是全库清单。",
        "inputSchema": {
            "type": "object",
            "properties": {
                # 2026-10-26（FB-03 · `EVAL-261026-06`）：同名/同会话两个公民并存时，
                # 单看 whoami 只能看到"其中一条"，看不出"还有另一条"。这个开关就是
                # 让这条关系**可见**的入口（只列相关项，不做全库导出）。
                "all": {"type": "boolean",
                        "description": "true = 附带相关公民清单（会话/同名/账号三种关联，"
                                       "按 id 去重；需要一个有效 token 才有锚）"},
            },
        },
    },
    {
        "name": "register",
        "title": "登记身份",
        "description": "登记你在这个系统里的身份（成为公民）。同名则复用（幂等）。"
                       "登记后你的记忆会归属到这个身份。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "display_name": {"type": "string", "description": "你的名字，如 workbuddy-main"},
                "kind": {"type": "string", "enum": ["visitor", "role", "resident"],
                         "description": "居留形态：visitor 会话级 / role 项目级 / resident 永久"},
                "role": {"type": "string", "description": "角色描述，如 reviewer"},
                "host": {"type": "string", "description": "来源宿主，如 workbuddy"},
                "session_name": {
                    "type": "string",
                    "description": "可选：会话级身份名（如 anyi-dev / anyi-review）。"
                                   "同一宿主下多个会话用它可以区分彼此的记录。",
                },
            },
            "required": ["display_name"],
        },
    },
    {
        "name": "rename_citizen",
        "title": "认领名字",
        "description": "把**你自己**的显示名改成指定名字。只改标签，不改身份："
                       "citizen_id、凭证、证据级别都不动，历史记忆的归属也不变。"
                       "当用户明确说「你是 X」时用它把名字落到系统里——"
                       "否则那个名字在系统里无处安放，只能变成一条孤儿公民。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "新名字，如 fish-主AI-2"},
                "reason": {"type": "string",
                           "description": "可选：为什么改名（写进事件，便于追溯）"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "close_project",
        "title": "闭合项目",
        "description": "把一个项目标记为**已完成**（closed）。不删任何东西、不动记忆归属，"
                       "只是不再接收新记忆；随时可用 reopen_project 回来继续写。"
                       "闭合 ≠ 删除（决策 16）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "项目前缀"},
                "reason": {"type": "string", "description": "为什么闭合（写进事件）"},
            },
            "required": ["prefix"],
        },
    },
    {
        "name": "reopen_project",
        "title": "重开项目",
        "description": "把 closed 的项目改回 active，之后可继续写入。"
                       "闭合是可逆的状态，不是锁死（用户 2026-10-25 拍板）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "项目前缀"},
                "reason": {"type": "string", "description": "为什么重开（写进事件）"},
            },
            "required": ["prefix"],
        },
    },
    {
        "name": "inject",
        "title": "权威当下",
        "description": "生成注入包（L0 身份 + L1 状态 + L2 索引，硬预算裁剪）。"
                       "L1「当前项目」按 显式 project → 会话档期 → 最近更新兜底 解析；"
                       "走兜底时会在输出里标注“据最近更新推断”，不会把猜测说成事实。\n"
                       "包尾两行自解释：`本次截断…/本次无截断`（砍了几条）+ `as-of …`"
                       "（数据截至时刻）——不必再猜这份包完不完整、是几点的事实。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "budget": {"type": "integer", "description": "token 预算"},
                # R1（EVAL-261024-02）：项目归属从"最近更新者猜测"改成"有证据的声明"。
                # **可选**入参：不破坏任何既有调用（required 一个字都不加）。
                "project": {"type": "string",
                            "description": "当前项目前缀（不传则按会话档期推断，无则标注为推断）"},
                # 2026-10-26（FB-02）：段选择。不传 = 全段（旧行为一字不变）。
                "sections": {
                    "type": "array",
                    "items": {"type": "string",
                              "enum": list(fish.INJECTION_SECTIONS)},
                    "description": "只构建这些段（缺省=全段）。可取值："
                                   "status（当前项目坐标）/ board（看板）/ rules（规矩）/ "
                                   "attention（该注意什么）/ lessons（错题本）/ push（候选）/ "
                                   "index（L2 索引）/ recent（最近三件事）。"
                                   "**L0 身份段永远保留**（不可关闭，设计如此）。",
                },
                "include_board": {
                    "type": "boolean",
                    "description": "`sections` 的便捷写法：false = 去掉看板段"
                                   "（等价于 sections 里不含 board）；缺省 true。"
                                   "与 sections 同时给时以 false 为准（不叠加）。",
                },
            },
        },
    },
    {
        "name": "healthcheck",
        "title": "系统自检",
        "description": "断链、休眠、事件数。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "session_start",
        "title": "签发会话凭证",
        "description": "在 MCP 面直接签发一条会话凭证（token），**明文只返回这一次**。\n\n"
                       "为什么需要它（2026-10-25 加，外部评估 `EVAL-261025-05`）："
                       "在此之前 MCP 面**没有任何口子能产出 token**——AI 只能离开 MCP 去跑 CLI，"
                       "而 CLI 那侧的会话键与 MCP 不同，会建成**另一个公民**，"
                       "两条通道的归属从此对不上且无法合并。\n\n"
                       "两种用法：\n"
                       "　· 已登记过 → 传 citizen（你在 register 回执里拿到的 citizen_id）\n"
                       "　· 还没登记 → 传 display_name，本工具会建公民并直接签发\n"
                       "**session_name 强烈建议传**：它是幂等锚，不传每次都建新公民。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "citizen": {"type": "string",
                            "description": "绑到**已存在**的公民（register 回执里的 citizen_id）"},
                "display_name": {"type": "string",
                                 "description": "新公民的名字（与 citizen 二选一）"},
                "session_name": {"type": "string",
                                 "description": "会话标识（幂等锚：同一 AI 重复调用应传同一个值）"},
                "project": {"type": "string",
                            "description": "可选：顺带开一个项目档期"},
                "kind": {"type": "string", "enum": ["visitor", "role", "resident"],
                         "description": "居留形态（默认 visitor）"},
            },
            "required": ["session_name"],
        },
    },
    {
        # 2026-10-25 加（用户要求补）：看板原来**看得见、动不了**——
        # 注入包会说「你还没签到」，但 MCP 面没有任何工具能签到，只能去跑 CLI。
        # 签到闭环断在这里，R5-2 的"未登记时提示"因此只是半条链路。
        "name": "board",
        "title": "看板",
        "description": "看板回答的是「**此刻谁在动哪块**」——不是发生了什么（那是记忆）。\n\n"
                       "三个动作：\n"
                       "　· action=show（默认）：这个项目今天有谁在、在做什么、占了哪些面、"
                       "**谁跟你撞了**（占用面重叠会单独警告）\n"
                       "　· action=checkin：登记你自己（doing 必填，surface 强烈建议："
                       "它是防撞车的唯一依据）\n"
                       "　· action=post：往今日动态写一条（kind=progress/done/note）——"
                       "**做完用 done 标掉**，否则看板只剩在场表，丢掉\"动到哪了\"那一半\n\n"
                       "**未登记 = 未发生**：看板上没有你的行，别人就无法判断会不会跟你撞车。"
                       "开工前先 checkin，是这个系统的协作协议，不是礼貌。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["show", "checkin", "post", "simulate"],
                           "description": "show 看板 / checkin 登记自己 / post 写动态（默认 show）"},
                "project": {"type": "string",
                            "description": "看哪个项目的板（不传则用根看板）"},
                "doing": {"type": "string",
                          "description": "checkin 必填：一句话说明你在做什么（≤80 字）"},
                "surface": {"type": "string",
                            "description": "你占用的面（文件/目录前缀，逗号分隔）。"
                                           "支持目录前缀：写 `backend/routes` 也算占用其下所有文件。"
                                           "simulate 时用它做假设（不写库）"},
                "kind": {"type": "string", "enum": ["progress", "done", "note"],
                         "description": "post 用：进度 / 做完了 / 备注"},
                "text": {"type": "string", "description": "post 必填：动态内容（≤200 字）"},
                # 2026-10-25（`EVAL-261025-14`）：`status='left'` 在 DDL 里写着，
                # 但 CLI 与 MCP 都没有入口——**想收工的人下不了板**，行只能自然变 idle。
                # 补上之后，"做完了"才有一个正式动作。
                "assume_surface": {"type": "string",
                                   "description": "simulate 用：**假设**另一个人占着这些面"
                                                  "（逗号分隔）。不给就只对比真实在场者，"
                                                  "单人时可能对比 0 人"},
                "status": {"type": "string", "enum": ["active", "left"],
                           "description": "checkin 用：active=在场（默认）/ "
                                          "left=收工下板（之后不再计入重叠检测）"},
            },
        },
    },
    {
        # T05（2026-10-26）：B3 知识库的**读**面（裁决 C2：读/写拆成两个工具）。
        # schema 与 `docs/架构设计-B1B2B3-v0.1.md` §3.3 逐字一致——**不含 token**，
        # 由 `_attach_token` 统一补（它有 import 期自检，手抄一份 token 定义会当场炸）。
        "name": "kb",
        "title": "知识库",
        "description": "只读访问已登记的知识库（Obsidian vault）：看目录、读单文件、在知识库范围内检索。返回内容来自「知识库」，不是 fish 记忆，也不进 recall / 全文索引；知识库默认整库跳过检索，只有你显式调用才会读到。找 fish 记忆请用 recall，找编码请用 resolve_code。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["vaults", "tree", "open", "search"]},
                "vault": {"type": "string",
                          "description": "vault 名字或 id；缺省=默认 vault"},
                "path": {"type": "string",
                         "description": "vault 内相对路径，用 / 分隔；open 必填；tree 可选起点"},
                "query": {"type": "string",
                          "description": "search 必填；字面匹配（非语义），大小写不敏感"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
                "max_chars": {"type": "integer", "default": 8000},
                # R15（安全 F4）：敏感名单命中的文件（凭证/密钥/.env…）默认只回
                # 文件名+大小+建议；要正文必须显式给 true（二次授权，不是默认行为）。
                # S8（第四轮）：给 true 还**必须带有效 token**——自授权不是授权。
                "allow_sensitive": {"type": "boolean", "default": False,
                                    "description": "open 时允许读敏感文件正文"
                                                   "（凭证/密钥/.env 命中名单时；"
                                                   "需同时带有效 token=…）。"
                                                   "只认明确的 true/false（1/0 亦可，"
                                                   "字符串 'true'/'false' 等价）；"
                                                   "其它写法按 bad_usage 拒绝"},
            },
            "required": ["action"],
        },
    },
    {
        # T05：B3 的**写**面。三条纪律写在描述里（判据认行为，描述是给人的第一道闸）：
        # 必须 mode=rw / update 必带 base_sha256 / 每次写前自动备份可 rollback。
        "name": "kb_write",
        "title": "写知识库",
        "description": "在已登记的知识库里新建/追加/显式更新一篇 Markdown（Obsidian 兼容：UTF-8 无 BOM、frontmatter 只增改给定键、[[双链]] 不改写）。每次写入前自动备份，可用 fish kb rollback <write_id> 撤回。不允许删除/移动/重命名；update 必须带 base_sha256（先 kb open 拿到）；vault 必须是 mode=rw；同名笔记先报冲突，不自动改写别人的文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "append", "update"]},
                "vault": {"type": "string"},
                "path": {"type": "string",
                         "description": "vault 内相对路径，用 / 分隔（必填）"},
                "content": {"type": "string",
                            "description": "Markdown 正文（UTF-8，不含 frontmatter）；"
                                           "create/update 必填"},
                "frontmatter": {"type": "object",
                                "description": "只增/改这些键（值必须是标量）；已有未知键保留"},
                "base_sha256": {"type": "string",
                                "description": "update 必填：你读到的文件 sha256（乐观锁）"},
                "reason": {"type": "string",
                           "description": "为什么写（进 kb_access.meta.reason，≤200 字）"},
                # S6（第四轮 · 安全 N3）：敏感目标（凭证/.env…）默认拒绝改写。
                # 写面本来就要求 token，所以这里的 `true` 是"已归属的显式二次授权"。
                "allow_sensitive": {"type": "boolean", "default": False,
                                    "description": "允许改写敏感名单命中的文件"
                                                   "（默认拒绝：凭证交 Obsidian/人工；"
                                                   "需有效 token）。只认明确的 true/false"
                                                   "（1/0 亦可）；其它写法按 bad_usage 拒绝"},
            },
            "required": ["action", "path"],
        },
    },
    {
        # T03（2026-10-27）：对话语料**只读**面（设计 §6/§9-7）。工具数 18 → 19，
        # 旧工具的名字/描述/参数一个字节都没动（判据 `test_corpus_search` 锁这份清单）。
        "name": "corpus",
        "title": "对话语料",
        "description": "检索原始对话语料（历史会话的**原话**，不是记忆）：search 找段、"
                       "read 按门牌读回原文、stats 看采集读数。语料库独立于记忆库"
                       "（corpus.sqlite），不进 recall / 注入包 / 全文索引。门牌 `log:` 可直接"
                       "粘进对话；`conv:` 是推送用的短址（同一个段）。source.missing=true 表示"
                       "源日志已不在，正文来自投影——**不猜、不摘要顶替**。"
                       "【隔离声明 · 2026-10-27】本机是单用户环境，**语料面不做项目/身份隔离**："
                       "同一个 MCP 面可以看到本机全部宿主的全部段（这是设计选择，不是疏漏）。"
                       "需要按项目收窄时请显式传 project（传空串 = 只看未归属）；"
                       "按 citizen 收窄列为**待拍板**项（见 docs/依赖说明-corpus.md §7）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(CORPUS_TOOL_ACTIONS)},
                "query": {"type": "string",
                          "description": "search 必填：关键词（中文按 bigram 切词，"
                                         "与索引侧同一套切法）"},
                "pointer": {"type": "string",
                            "description": "read 必填：log:<host>:<sid>#seq=123[.1] "
                                           "或 conv:<host>:<sid8>#seq=123"},
                "host": {"type": "string", "description": "过滤：dsh / workbuddy"},
                "project": {"type": "string",
                            "description": "过滤：项目前缀（传空串=只要未归属）"},
                "role": {"type": "string", "enum": ["user", "assistant"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 32, "default": 8},
                "max_chars": {"type": "integer", "default": 8000},
                "ranker": {"type": "string",
                           "description": "排序算法（缺省 default；未登记的名字会响亮失败）"},
            },
            "required": ["action"],
        },
    },
]

# P1-c：11 个工具统一补可选 token 入参（工具数量与名字都不变，只多一个可选字段）。
TOOLS = _attach_token(TOOLS)

RESOURCES = [
    {
        "uri": "fish://me",
        "name": "当前公民的注入包",
        "description": "宿主可预加载：AI 不用问就知道自己是谁、在哪",
        "mimeType": "text/markdown",
    },
    {
        "uri": "fish://index",
        "name": "编码索引摘要",
        "description": "全部编码清单（上限 200 条）",
        "mimeType": "text/markdown",
    },
]


def _inject_sections_arg(args):
    """把 `inject` 的 `sections` / `include_board` 归一成 `build_injection_pack` 的入参。

    2026-10-26（评审 🟠3）：**归一逻辑搬到了 `fish.normalize_inject_sections_arg`**
    ——CLI 的 `inject --sections/--no-board` 要与之同口径。这里只做 dict 拆包：
    两个入口各写一份归一，必然出现"一个说不要看板、另一个包里还有看板"。
    返回值语义不变：`None` = 全段（不传就不改变既有行为）；列表 = 只要这些段。
    """
    return fish.normalize_inject_sections_arg(args.get("sections"),
                                              args.get("include_board"))


class FishServer:

    def __init__(self, root: Path):
        self.root = root
        self.conn = fish.connect(root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        # **这里刻意什么都不记。**
        # 本进程被宿主的所有会话共享（2026-10-23 串号事故的根因），所以任何
        # "当前身份 / 当前会话名 / 本会话提示只显示一次"的状态都是错的：
        #   · self._actor        —— register 写进来的公民（谁最后 register 谁生效）
        #   · self._session_actor —— FISH_SESSION_ACTOR 或 register 的 session_name
        #   · self._hint_shown    —— "每进程只提醒一次"：第一个会话会把提示吃掉，
        #                            后面的会话永远收不到（同一个根因的另一种表现）
        # 身份改由**每次调用**的 token 解析（`_resolve_identity`），用完即弃。

    def _audit(self, kind, name, ok=True, started=None, ident=None):
        """轻量审计：写到 <root>/_audit/tool-calls.jsonl。

        为什么需要：inject / resources/read 不留痕迹时，无法区分
        「AI 真的读了 fish」与「AI 从自己宿主日志里回忆」。审计失败不影响主流程。

        字段（2026-10-24 改）：身份三件套改成**逐次调用**从 token 解析（ident），
        并新增 `identity_status`——审计里出现 `identity_status=none` 就是"这次调用
        没归属"，而不是像事故那天那样出现一个从别的会话串过来的名字。
        **token 明文绝不进这里**（只可能有 citizen_id / session_key）。
        """
        try:
            d = self.root / "_audit"
            d.mkdir(exist_ok=True)
            ident = ident or IDENTITY_ANONYMOUS
            import datetime
            now = datetime.datetime.now().astimezone()
            row = {"ts": now.isoformat(timespec="seconds"),
                   "ts_utc": fish.now_utc(),
                   # 未归属时 actor 就是 None：宁可空着，也不填"看起来像谁"的值。
                   "actor": ident.citizen_id,
                   "actor_registered": ident.citizen_id is not None,
                   "session_actor": ident.session_key,
                   "identity_status": ident.status,
                   "evidence": ident.evidence,
                   "kind": kind, "name": name, "ok": ok,
                   "duration_ms": int((time.time() - started) * 1000) if started else None}
            with open(d / "tool-calls.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:      # noqa: BLE001 —— 审计缺数会让复盘得出错误结论
            fish.warn_derived(self.conn, "tool-calls.jsonl", exc)

    def _resolve_identity(self, token):
        """把本次调用带的 token 解析成 Identity（**纯函数：不写任何进程状态**）。

        三条实现纪律（18 §7 / 16 §2.2）：
          1. **有效性判定只有一处**：`fish.validate_session_token`。这里不许再写一遍
             `revoked_at / expires_at` 的 SQL——两处实现必然漂，而"什么算失效"是
             凭证体系里最不该漂的东西；
          2. 失效原因（吊销 / 过期）只用来选**提示文案**，不参与判定；
          3. token 明文只在入参里过一遍：不打印、不写审计、不进返回值。
        """
        if not token:
            return IDENTITY_ANONYMOUS
        citizen_id = fish.validate_session_token(self.conn, token)
        info = fish.lookup_session_token(self.conn, token)
        if info is None:
            return IDENTITY_REJECTED
        if not citizen_id:
            reason = IDENTITY_REVOKED if info["revoked_at"] else (
                IDENTITY_EXPIRED if info["expired"] else IDENTITY_INVALID)
            return Identity(None, None, info["evidence"], reason)
        # session_key / evidence 取**凭证行里的值**，不取环境变量：
        # 环境是进程级的（一个进程服务所有会话），凭证才是会话级的。
        return Identity(citizen_id, info["session_key"], info["evidence"], IDENTITY_VALID)

    @staticmethod
    def _identity_hint(ident):
        """这一档身份该给对方的那句提示（valid 时没有提示）。"""
        return IDENTITY_HINTS.get(ident.status)

    def _credential_state(self, a):
        """本次调用带的凭证**处于什么状态**（FB-07，一行四档）。

        与 `ident` 的分工：`ident.status` 回答"这次能不能署名"，判定在
        `fish.validate_session_token`（全仓只此一处）；这一行走得更远——把四种
        状态各说清楚，AI 才分得清「身份未核」到底是哪一种未核。

        **措辞与判定都在 `fish.credential_state_line`**（2026-10-26 · F3）：
        CLI 的 `fish whoami` 也要同样两行，两个入口各写一份必然漂——所以文案收在
        底层，这里只做入参解包。
        """
        return fish.credential_state_line(self.conn, (a or {}).get(TOKEN_ARG))

    @staticmethod
    def _identity_source_line(ident, *, registered=False):
        """一行写明**这条回执的身份是从哪来的**（FB-03 · `EVAL-261026-06`）。

        为什么非要有这一行：同一次会话里，`register` 回执说"role / 公民 A"，
        带 token 的 `whoami` 说"visitor / 公民 B"，两边都不报错——读的人无法判断
        哪一个是"会被写进记忆归属"的那一个。把来源写在明面上，分歧当场可见。

        三档措辞收在 `fish.identity_source_line`（2026-10-26 · F3）：CLI 的 whoami
        要用同一份口径，两边各写一份就会漂。判定仍不在这一层——`ident.status` 由
        `_resolve_identity` → `validate_session_token` 算出。
        """
        valid_citizen = ident.citizen_id if (ident and ident.status == IDENTITY_VALID) else None
        return fish.identity_source_line(valid_citizen, registered=registered)

    def _same_name_warnings(self, ident):
        """同名告警：**本 token 的公民**在库里还有没有同名活着的另一个兄弟。

        为什么只对"带有效 token 的 whoami"说（2026-10-26 · FB-03）：
          没有凭证时连"我是谁"都答不了，去谈"和你同名的还有谁"只会更糊涂。
          而且这条告警的落点是"本次写入记在谁名下"——没有凭证就没有落点。

        文案里的三件事缺一不可：命中了谁（id + 形态 + 最近活跃）、系统的动作
        （**不合并**）、以及最关键的后果（**这次写入会记到我的 id 名下**）。
        只报"检测到同名"而不说落点，读者会以为系统自动合并或者平均分配。
        """
        if not ident or ident.status != IDENTITY_VALID or not ident.citizen_id:
            return []
        me = fish.load_citizen(self.conn, ident.citizen_id) or {}
        name = (me.get("display_name") or "").strip()
        if not name:
            return []
        rows = fish.related_citizens(self.conn, display_name=name,
                                     exclude_citizen_id=ident.citizen_id)
        out = []
        for it in rows:
            # 只看**活跃**同名者：已退役的同名公民不构成"另一套在用的身份"。
            if "name" not in it["relations"] or it["status"] != "active":
                continue
            out.append(f"⚠ 检测到同名公民 {it['citizen_id']}"
                       f"（{it['kind'] or 'unknown'}，"
                       f"最近活跃 {it['last_seen_at'] or '未记录'}）"
                       f"——系统不合并；本次写入记在 {ident.citizen_id} 名下")
        return out

    def _related_citizens_lines(self, ident):
        """`whoami all=true` 的相关公民清单（不含自己，不是全库清单）。

        2026-10-26（评审 🟠3）：**渲染搬到了 `fish.related_citizens_lines`**，
        与 CLI 的 `fish whoami --all` 共用同一份文案。这里只剩"把 ident 翻成
        citizen_id"这一件事——判定仍归 `ident.status`（由 `validate_session_token`
        算出）。两个入口各写一段渲染，是本轮修掉的三处漂移的共同来源。
        """
        valid = ident.citizen_id if (ident and ident.status == IDENTITY_VALID) else None
        return fish.related_citizens_lines(self.conn, valid)

    def _owner_value(self, ident):
        """写进 code_index.owner 的值：有身份用公民 ID，没有就 'unattributed'。

        为什么不是 None：owner 是"这条记忆归谁"的唯一字段，None 会被读侧当成
        "老数据没这一列"（同一个 NULL 两种含义 = 审计毒药）。'unattributed' 是
        明确的"当时没凭证"，能被筛选、能被补归属。
        """
        return ident.citizen_id or UNATTRIBUTED

    # ------------------------------------------------------------ 协议包装

    @staticmethod
    def _ok(rid, result, modern=True, ttl_ms=None, cache_scope=None):
        result.setdefault("resultType", "complete")
        if ttl_ms is not None:
            result.setdefault("ttlMs", ttl_ms)
        if cache_scope is not None:
            result.setdefault("cacheScope", cache_scope)
        if modern:
            result.setdefault("_meta", {})[META_PREFIX + "serverInfo"] = SERVER_INFO
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _err(rid, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": rid, "error": err}

    # ------------------------------------------------------------ 业务
    # 每个工具的签名都是 `(self, a, ident)`：
    #   a     = 调用方参数（含可选 token，但工具本身不解析它——解析只在 _resolve_identity）
    #   ident = 本次调用的身份（Identity）。**显式传参，不读 self**：读 self 就等于
    #           把"当前身份"写回进程，而进程是共享的（本轮要根治的就是这件事）。

    def tool_remember(self, a, ident):
        # 查重提示的出口（2026-10-25 · 阶段 C）：传一个空 list 进去，写入后由
        # `record_memory` 把相似项填进来。**不传（None）= 零开销**，所以这是一个
        # 纯增量参数——旧调用路径一个字都不变。
        # 为什么是"写入后提示"而不是"写入前拦截"：用户定过硬约束——每次写入都弹确认，
        # AI 会绕过系统直接写文件，那比不查重糟糕得多。所以这里只提示、绝不阻断。
        hints = []
        code, rel = fish.record_memory(
            self.conn, self.root,
            project=a["project"], title=a["title"], body=a.get("body", ""),
            mem_type=a.get("mem_type", "fact"), tags=a.get("tags") or [],
            visibility="shared",
            # 归属来源只有一处：token 解析出的 citizen_id（没有就 'unattributed'）
            owner=self._owner_value(ident),
            session_actor=ident.session_key,
            attachments=a.get("attachments") or [],
            supersedes=a.get("supersedes"),
            occurred_at=a.get("occurred_at"),
            cause=a.get("cause"),
            hints=hints)
        extra = f"，会话 {ident.session_key}" if ident.session_key else ""
        # 写入本身不拒绝（记忆比归属重要），但归属的真相必须跟着回执一起走：
        # 无凭证 / 凭证失效时由 call_tool 统一追加提示，这里不再维护"每进程说一次"的开关。
        #
        # 2026-10-25（`EVAL-261025-10`）：**把「软门」真正接上**。
        # 在此之前 `board_checkin_or_remind` 是**死代码**——全仓唯一调用点是测试，
        # 生产零调用，于是"写操作前提示未签到"这条从设计到实现都是空的。
        # 比"没有"更坏的是它**看起来存在**：读代码的人会以为有软门。
        #
        # 三条纪律：
        #   1. **绝不阻断**（记忆比仪式重要；硬拦会让 AI 绕过系统直接写文件）
        #   2. **签到后自动不再提**（presence 有行 → 不提示），不用另做会话级去重
        #   3. 失败静默（软门是增强项，不该把 remember 打挂）
        # 2026-10-25（`EVAL-261025-16` 建议 3）：**兜底 presence**。
        # 「很多人不 checkin 不是因为不愿意，是记不住」——所以在写记忆时顺手登记一条，
        # 让看板不再"0 人"。三条红线见 `board_auto_presence`：只在无行时写、
        # 标注来源、不编造 surface。
        try:
            if ident.citizen_id:
                # 同一个会话键必须一路传到底：不传的话这次兜底会落到 `''` 行，
                # 于是"自动行"和"签到行"变成同公民的两行（看板上多一行幽灵）。
                fish.board_auto_presence(self.conn, ident.citizen_id, a.get("project"),
                                         session_key=(ident.session_key or ""))
        except Exception as exc:      # noqa: BLE001 —— 兜底失败要出声（静默清零）
            fish.warn_derived(self.conn, "board_auto_presence", exc)
        tip = ""
        try:
            ok, msg = fish.board_checkin_or_remind(
                self.conn, ident.citizen_id, a.get("project"))
            if not ok and msg:
                tip = "\n" + msg + "\n"
        except Exception:          # noqa: BLE001 —— 见上第 3 条
            pass
        return f"{code}\n{rel}{extra}\n" + fish.format_dup_hint(hints) + tip

    def tool_recall(self, a, ident):
        """检索记忆（ADR-005）：编码 / 标题 / 正文统一走 fish.search_memories。

        与 CLI 共用同一个读入口——2026-10-22 的教训：`record_memory` 曾经有过两份
        实现，读路径不许重演这件事。

        2026-10-26（FB-01）：补上"列出模式"。三种调用形态，各有各的回包：
          · 有 `query` / `code` → 检索（未命中仍给"最近的条目"，回退应用 tags）；
          · 无 `query` / `code`，有 `project` → **列出**（项目 + 标签过滤，时间倒序），
            首行声明"按什么过滤、共几条"——**过滤条件必须写在回包里**，
            否则读者无法判断这些是"全部"还是"筛过的子集"；
          · 只有 `tags` → **引导**而不是报错（tags 是附加过滤，没有范围可列；
            报错只说"不行"，调用方真正缺的是"那该怎么写"）。
        """
        q = a.get("query")
        code = a.get("code")
        project = a.get("project")
        tags = a.get("tags")
        include_superseded = bool(a.get("include_superseded"))
        if not q and not code:
            if not project:
                if tags:
                    # 不抛错：这是"用法缺一半"，不是"参数非法"
                    return fish.TAGS_ONLY_HINT + "\n"
                raise ValueError("query / code / project 至少要给一个")
            # 列出模式的 limit 默认 20（比检索的 10 宽）：没有关键词时
            # "多看几条"本身就是这次调用的目的。
            limit = int(a.get("limit") or fish.LIST_DEFAULT_LIMIT)
            rows = fish.list_memories(self.conn, project=project, tags=tags,
                                      limit=limit,
                                      include_superseded=include_superseded)
            total = fish.count_memories(self.conn, project=project, tags=tags,
                                        include_superseded=include_superseded)
            head = fish.format_list_declaration(project=project, tags=tags,
                                                shown=len(rows), total=total)
            if not rows:
                # 评审 ⚪11：没给 tags 时不许说"去掉 tags"——那句话描述的是
                # 调用方**没做过的动作**，读者只会去找一个不存在的参数。
                given = [t for t in ([tags] if isinstance(tags, str) else (tags or []))
                         if t]
                hint = ("去掉 tags 再列一次，或换 project" if given
                        else "该项目下还没有记忆（可换 project，或先 remember 一条）")
                return head + "\n（该项目下没有符合筛选条件的条目；" + hint + "）\n"
            fish.record_access(self.conn, [r[0] for r in rows], "recall",
                               actor=ident.citizen_id)
            self._log_access([r[0] for r in rows], "recall", ident)
            lines = [head, fish.RECALL_DIMENSION_NOTE]
            for c, t, p, m, s, fp, d, src in rows:
                lines.append(f"- {c} {fish.format_status_tag(m, s, d)} {t}"
                             f"  ← 匹配:{src}｜项目:{p}｜正文用 read_body")
            return "\n".join(lines) + "\n"
        fish.ensure_fts(self.conn, self.root)
        rows, _ = fish.search_memories(
            self.conn, query=q, code=code, project=project,
            tags=tags, scope=a.get("scope", "all"),
            limit=int(a.get("limit", 10)),
            include_superseded=include_superseded)
        label = q or a.get("code") or a.get("project") or ""
        if not rows:
            # 回退列表必须遵守同样的过滤（2026-10-22 反馈 ANYI-261022-114：
            # 旧实现取全库最近 5 条，导致 project=ANYI 的查询串出台到别的项目）
            cand = fish.recent_fallback(self.conn, project=a.get("project"),
                                        tags=a.get("tags"))
            head = f"未找到「{label}」"
            if a.get("project"):
                head += f"（已限定 project={a['project']}）"
            return head + "。最近的条目：\n" + "\n".join(
                f"- {c} {t}" for c, t in cand) + "\n"
        fish.record_access(self.conn, [r[0] for r in rows], "recall",
                           actor=ident.citizen_id)
        # 按人访问记录（13 §2.1）：每条命中的编码各写一行，via=recall
        self._log_access([r[0] for r in rows], "recall", ident)
        # 2026-10-26（FB-10）：**第一行先交代维度**，再列条目。旧输出把 dormant
        # 挂在方括弧外面（`[fact/active] (休眠)`），一个空格之隔，长行里几乎看不见，
        # 读者容易把「(休眠)」当成另一个 status 值——而它其实是独立的衰减位。
        # 状态列改走 `fish.format_status_tag`：MCP 与 CLI 共用一处渲染，两处各写
        # 一份必然漂（`recent_fallback` 那次跨项目串台就是这么来的）。
        lines = [fish.RECALL_DIMENSION_NOTE,
                 "（关键词匹配，不是语义检索；同义词查不到——见 ADR-006）"]
        for c, t, p, m, s, fp, d, src in rows:
            lines.append(f"- {c} {fish.format_status_tag(m, s, d)} {t}"
                         f"  ← 匹配:{src}｜正文用 read_body")
        return "\n".join(lines) + "\n"

    def tool_add_project(self, a, ident):
        """注册新的项目前缀。补 ANYI-261022-01 的缺口：之前只能走 CLI。"""
        prefix = (a.get("prefix") or "").strip()
        if not prefix:
            raise ValueError("prefix 不能为空")
        # EVAL-261025-17：**先查库、再校验字符**（2026-10-25 修）。
        # 存量前缀可能是治理直接建出来的（如 anyi-studio 含 `-`）——字符规则
        # 只约束"新前缀"。拿它拦已落地的存量，会把"已存在"误报成"非法"，
        # 使用者就会换个名字重建重复项目（正好是治理要防的）。
        row = self.conn.execute("SELECT prefix,namespace FROM code_registry WHERE prefix=?",
                                (prefix,)).fetchone()
        if row:
            return f"前缀已存在：{row[0]} → {row[1]}（可直接使用）\n"
        if "-" in prefix:
            raise ValueError("prefix 不能含 '-'（编码用它分隔段，如 ANYI-261022-01；"
                             "含 '-' 的存量前缀不适用本工具）")
        if len(prefix) > 20:
            raise ValueError("prefix 太长（≤20 字符）")
        ns = (a.get("namespace") or prefix).strip()
        # 写前查重（2026-10-25 · 阶段 C）：只提示不拦。精确重名上面已经挡掉，
        # 这里补的是"可能是同一个项目"的三种形态（大小写 / 包含 / 描述同义）。
        hints = fish.find_similar_projects(self.conn, prefix, namespace=ns)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,?,?,?)", (prefix, ns, "", 0, "active"))
        self.conn.commit()
        fish.append_event(self.conn, "project.added",
                          # evidence_level 如实写：MCP 面不签发 machine（评审 R-2），
                          # 有凭证就写凭证的证据级别，没凭证就 'none'——绝不冒充 machine。
                          {"citizen_id": self._owner_value(ident),
                           "evidence_level": ident.evidence or "none"},
                          {"kind": "project", "id": prefix},
                          {"prefix": prefix, "namespace": ns})
        return (f"已注册前缀：{prefix} → {ns}\n"
                f"现在可以 remember(project='{prefix}', ...) 写入。\n"
                + fish.format_dup_hint(project_hints=hints))

    def tool_supersede(self, a, ident):
        """把旧编码标记为被新编码替代。"""
        msg = fish.supersede_memory(self.conn, a["code"], a["by"],
                                    reason=a.get("reason", ""),
                                    actor=ident.citizen_id)
        return msg + "\n"

    def tool_read_body(self, a, ident):
        """读记忆正文全文。补 ANYI-261022-31 的缺口：之前没有任何工具能读到正文。"""
        code = a["code"]
        row = self.conn.execute("SELECT file_path FROM code_index WHERE code=?",
                                (code,)).fetchone()
        renamed_note = ""
        if not row:
            # 2026-10-25 修（`EVAL-261025-06`）：跟随一次改名重定向。
            # 正文明明在，报「不存在」会让 AI 判定记忆丢失并重记一条（制造重复）。
            _new = fish.resolve_renamed(self.conn, code)
            if _new:
                row = self.conn.execute("SELECT file_path FROM code_index WHERE code=?",
                                        (_new,)).fetchone()
                if row:
                    renamed_note = (f"<!-- 该编码已改名：{code} → {_new}"
                                    f"（2026-10-25 项目改名迁移，映射永久保留） -->\n\n")
                    code = _new
        if not row:
            raise ValueError(f"记忆不存在：{code}")
        # 2026-10-25（用户要求「人类会自由修改文件，系统不能崩」）：
        # **正文读取统一走 `fish.read_body_path`**——它永远返回 (text, info)，
        # 不抛异常，并且会按编码兜底搜索 + 自愈（人类改名/挪位置都能救回来）。
        # 旧实现这里 `raise ValueError("文件缺失")`，一个文件被删就让整条调用链断掉——
        # 那是把"局部数据问题"升级成"系统不可用"。
        text, binfo = fish.read_body_path(self.conn, self.root, code)
        if binfo["state"] == "missing" or not text:
            return binfo.get("hint") or f"正文缺失：{code}\n"
        relocated_note = ""
        if binfo.get("healed"):
            relocated_note = (f"<!-- 文件位置变了（人类改名/移动过），已自动修回记录："
                              f"→ {binfo['path']} -->\n\n")
        # 读了就算访问：刷新衰减，让"被读过的记忆"不容易沉睡
        self._touch([code], ident, "read_body")
        # 16 §5.6：这条被推过、现在被读了 → 回填 accepted（不改读正文的任何行为）
        self._accept_push(code, ident)
        size = len(text)
        # `binfo["path"]` 而不是 `row[0]`：文件可能被人类挪过，
        # 实际读到的位置才是真话（`row[0]` 是库里的旧记录）。
        return (f"{renamed_note}{relocated_note}{text}\n\n"
                f"<!-- {code} · {size} 字符 · {binfo['path']} -->\n")

    def tool_neighbors(self, a, ident):
        strategy = a.get("strategy") or fish.get_config(self.conn, "neighbors.strategy")
        n = int(a.get("n") or fish.get_config(self.conn, "neighbors.n"))
        res = fish.neighbors(self.conn, a["code"], n=n, strategy=strategy)
        if res is None:
            return f"编码不存在：{a['code']}\n"
        lines = [f"策略={res['strategy']} n={n}"]
        if "prev" in res:
            lines.append("前：")
            lines += [f"- {c['code']} {c['title']}" for c in reversed(res["prev"])]
            lines.append(f"← {a['code']}")
            lines.append("后：")
            lines += [f"- {c['code']} {c['title']}" for c in res["next"]]
        else:
            if not res["hits"]:
                lines.append("（没有符合条件的邻近项）")
            lines += [f"- {c['code']} {c['title']}"
                      + (f" [{c['score']}]" if "score" in c else "") for c in res["hits"]]
        return "\n".join(lines) + "\n"

    def tool_resolve(self, a, ident):
        code = a["code"]
        row = self.conn.execute(
            "SELECT code,title,project,mem_type,status,file_path,occurred_at,recorded_at,"
            "attachments,session_actor FROM code_index WHERE code=?", (code,)).fetchone()
        renamed_note = ""
        if not row:
            # 2026-10-25 修（外部评估 `EVAL-261025-06`）：**先查改名映射再判不存在**。
            # 旧实现直接回「编码不存在」，而正文明明在——改名只重写了内部引用，
            # 本地工作空间（MEMORY.md / daily / artifacts）里还有大量旧编码写法。
            # 后果不是"查不到"这么轻：AI 会判定它已丢失，然后**重记一条**，制造重复。
            _new = fish.resolve_renamed(self.conn, code)
            if _new:
                row = self.conn.execute(
                    "SELECT code,title,project,mem_type,status,file_path,occurred_at,"
                    "recorded_at,attachments,session_actor FROM code_index WHERE code=?",
                    (_new,)).fetchone()
                if row:
                    renamed_note = (f"⚠ 该编码已改名：{code} → {_new}\n"
                                    f"  （2026-10-25 项目改名迁移；旧编码不再存在，"
                                    f"映射永久保留，见 `code.renamed.batch` 事件）\n")
                    code = _new
        if not row:
            # 2026-10-24 修：回退建议必须只给**能用**的编码。
            # 旧实现 `ORDER BY code DESC LIMIT 5` 会把 withdrawn/superseded 的也报出来，
            # 等于建议 AI 去看已作废的东西（与 search_memories/recent_fallback 的口径不一致）。
            cand = self.conn.execute(
                "SELECT code FROM code_index WHERE status='active' "
                "ORDER BY code DESC LIMIT 5").fetchall()
            if not cand:
                return f"编码不存在：{code}\n（库里没有 active 记忆可作参考）\n"
            return f"编码不存在：{code}\n最近的编码（仅 active）：" + ", ".join(c[0] for c in cand) + "\n"
        c, t, p, m, s, fp, o, r, atts, sess = row
        # 按人访问记录（13 §2.1）：校验编码也算"被想起"，via=resolve
        self._log_access([c], "resolve", ident)
        # 16 §5.6：AI 顺着推送来校验编码 = 被回应，回填 accepted
        self._accept_push(c, ident)
        cfg = fish.load_decay_cfg(self.conn)
        decay, state = fish.compute_decay(m, None, r, cfg)
        hl = cfg.get(f"decay.{m}_days")
        hl_txt = "永不衰减" if (hl is None or int(hl) <= 0) else f"{int(hl)} 天"
        extra = ""
        if sess:
            extra += f"- 会话：{sess}\n"
        if s == "superseded":
            sb = self.conn.execute("SELECT superseded_by FROM code_index WHERE code=?",
                                   (c,)).fetchone()
            if sb and sb[0]:
                extra += f"- ⚠ 已被 {sb[0]} 替代，当前结论请看新条目\n"
        elif s == "withdrawn":
            # 2026-10-24 加：withdrawn 是「主动作废」（与 superseded 的「被替代」不同），
            # 旧实现只判 superseded，导致查已作废条目时不给任何提示——
            # AI 会把作废内容当当前结论用。作废原因在 memory.retired 事件的 payload 里。
            reason = ""
            try:
                ev = self.conn.execute(
                    "SELECT payload FROM events WHERE type='memory.retired' "
                    "AND json_extract(subject,'$.id')=? ORDER BY ts DESC LIMIT 1", (c,)).fetchone()
                if ev and ev[0]:
                    reason = json.loads(ev[0]).get("reason") or ""
            except Exception:
                reason = ""
            extra += f"- ⛔ 此条已作废（withdrawn），**不要当当前结论用**"
            extra += f"\n- 作废原因：{reason}\n" if reason else "\n"
        if atts:
            try:
                meta = json.loads(atts)
                extra += "- 附件：\n" + "".join(
                    f"    · {x['name']} → {x['blob']} "
                    f"({x['size']} B, sha256:{x['sha256'][:12]}…)\n" for x in meta)
            except Exception:
                extra += f"- 附件（原始）：{atts}\n"
        return (renamed_note
                + f"{c}\n- 标题：{t}\n- 项目：{p}\n"
                f"- 类型：{m}（半衰期 {hl_txt}，以 config 为准）\n"
                f"- 状态：{s}（衰减 {decay:.2f}/{state}）\n"
                f"- 发生：{o}\n- 记录：{r}\n- 文件：{fp}\n"
                f"- 正文：用 read_body 读全文\n"
                + extra)

    def tool_whoami(self, a, ident):
        """我是谁：**只认 token**，不认进程内存、也不认环境变量。

        为什么连 FISH_ACTOR 都不认（2026-10-24 的决定）：宿主只起一个 MCP 进程，
        `FISH_ACTOR` 是这个进程的启动参数——它对所有会话都是同一个值。把它当"我是谁"
        回答，等于告诉第二个会话"你是第一个会话"。没有凭证时唯一诚实的答案是"身份未核"。

        2026-10-26（FB-03 · `EVAL-261026-06`）：回执不能只报一个名字，它必须交代
        **这个名字是从哪来的**——同一会话里 `register` 说"公民 A"、带 token 的
        `whoami` 说"公民 B"，两边都不报错，写入却只记在 B 名下。三件事一并补上：
          ① 一行 `身份来源：…`（token / register 登记 / 未归属），见 `_identity_source_line`；
          ② 同名告警，见 `_same_name_warnings`；
          ③ `all=true` 的相关公民清单，见 `_related_citizens_lines`。
        判据（C1/C3/C4）读的都是**印出来的文本**，所以三段都必须在回执里。
        """
        lines = []
        if ident.status == IDENTITY_VALID:
            row = self.conn.execute(
                "SELECT display_name,kind FROM citizens WHERE citizen_id=?",
                (ident.citizen_id,)).fetchone()
            name, kind = (row[0], row[1]) if row else (ident.citizen_id, "unknown")
            lines.append(f"登记身份：{name}（公民 {ident.citizen_id}，形态 {kind}）")
            lines.append(f"凭证：会话 {ident.session_key}｜证据 {ident.evidence}"
                         "（来自 session_tokens 表，本次调用现查，不存进程内存）")
            lines.append("说明：这是你在 fish 的账户名（宿主级），与你的宿主身份不冲突。")
        else:
            lines.append("身份未核：本次调用没有可用的会话凭证，无法回答“我是谁”。")
            lines.append("（fish 不再保留进程内身份：一个 MCP 进程被所有会话共享，"
                         "“记住最后一个 register 的人”正是 2026-10-23 串号事故的根因。）")
            lines.append(self._identity_hint(ident))
        # 2026-10-26（FB-07）：`ident.status` 只回答"这次调用能不能署名"，不回答
        # "我手上这条凭证还能用多久"。AI 看到「身份未核」时无法区分**没带凭证**、
        # **带了一条已吊销的**、**带了一条过期的**，于是没法决定该不该重签——
        # 而重签正是唯一出口。所以单独一行把四种状态说清楚。
        lines.append(self._credential_state(a))
        # 2026-10-26（FB-03）：紧挨着凭证状态——两行合起来回答"我手上这条凭证
        # 是什么状态、它算不算本次归属的依据"。这是 register 与 token 两套视图的分界线。
        lines.append(self._identity_source_line(ident))
        lines.extend(self._same_name_warnings(ident))
        owner = fish.resolve_owner(self.conn)
        if owner:
            ro = self.conn.execute(
                "SELECT display_name,kind FROM citizens WHERE citizen_id=?",
                (owner,)).fetchone()
            if ro:
                lines.append(f"主人：{ro[0]}（公民 {owner}，形态 {ro[1]}）")
        else:
            lines.append("主人：未设置（fish config set owner_citizen <id>）")
        # 2026-10-26（FB-03）：`all=true` 的相关公民清单。放在主人之后、CLI 提示之前
        # ——它是"还有谁和你占着同一个上下文"的诊断面，属于身份信息的一部分。
        # 不传 = 一个字都不多（旧调用与旧判据完全不受影响）。
        if a.get("all"):
            lines.append("")
            lines.extend(self._related_citizens_lines(ident))
        # 2026-10-26（FB-13）：装好 MCP 的 AI 手上有工具、没有命令。它想跑
        # `fish session start` 时只会得到"不是内部或外部命令"，然后**放弃并绕路**
        # ——绕路的具体形态就是长期停在 self-reported。一行入口的成本换掉一次静默降级。
        lines.append(fish.cli_entry_hint())
        return "\n".join(lines) + "\n"

    def tool_register(self, a, ident):
        """登记一个公民（幂等）。**不再把身份写进本进程**。

        语义变化（P1-c）：register 只负责"在库里建一个公民"，它不再是"我现在的身份"。
        身份归属由 `fish session start` 签发的 token 决定——因为进程是共享的，
        任何"本进程当前身份"都等于"最后一个 register 的人"（2026-10-23 串号事故）。

        2026-10-26（FB-03）：回执多两段话，专治"两套视图"的误解——
          · `身份来源：…`：这次回执凭什么算身份（register 的回执**不参与归属**）；
          · 边界句：本次只登记公民、**不绑定会话**，写入归属由 token 决定。
        为什么必须写在回执里（而不是文档里）：读回执的人只有回执；看不到的边界，
        等于不存在——用户实测就是这么把"register 了就应该记在我名下"当成事实的。
        """
        name = (a.get("display_name") or "").strip()
        if not name:
            raise ValueError("display_name 不能为空")
        kind = a.get("kind", "visitor")
        if kind not in fish.KINDS:
            raise ValueError(f"kind 只能是 {fish.KINDS}")
        role = a.get("role")
        # host 只从入参取：FISH_ACTOR 是进程级启动参数，用它当"来源宿主"会把
        # 所有会话的公民都记成同一个宿主，等于换了个字段继续串号。
        host = a.get("host") or "unknown"

        row = fish.find_citizen_by_name(self.conn, name)
        if row:
            return (f"已存在，直接使用：{row[0]}\n"
                    f"{row[1]} / {row[2]}\n"
                    + self._identity_source_line(ident, registered=True) + "\n"
                    + self._register_boundary_note(ident)
                    + self._credential_unmoved_note(ident, name)
                    + self._register_next_step(a, row[0])
                    + fish.cli_entry_hint() + "\n")

        cid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, name, kind, role, host, "active", fish.now_utc()))
        self.conn.commit()
        fish.append_event(self.conn, "citizen.registered",
                          # MCP 面不签发 machine（评审 R-2）：这里没有任何东西可校验，
                          # 写 self-reported 才是如实；有凭证就沿用凭证的证据级别。
                          {"citizen_id": cid, "evidence_level": ident.evidence or "self-reported"},
                          {"kind": "citizen", "id": cid},
                          {"display_name": name, "kind": kind, "role": role, "host": host})
        return (f"{cid}\n{name} / {kind}\n"
                + self._identity_source_line(ident, registered=True) + "\n"
                + self._register_boundary_note(ident)
                + self._credential_unmoved_note(ident, name)
                + self._register_next_step(a, cid)
                + fish.cli_entry_hint() + "\n")

    def _register_boundary_note(self, ident):
        """register 的边界声明（FB-03）：**只登记公民，不绑定会话**。

        为什么单列一句而不是塞进 `_credential_unmoved_note`：
        那个函数只在"名字不同"时说话（名相同就该闭嘴，判据 A 钉着），
        而边界是**无条件**的事实——register 自己那个名字时同样"不绑定会话"。
        把两者混在一个条件分支里，就会出现"名相同时连边界都不说"的漏。

        `当前 token → 公民 X` 这一段读的是本次调用的 ident（现查），
        而不是刚登记的那个 citizen_id：两者常常不是同一个，**这正是要显示的信息**。
        """
        cid = ident.citizen_id if (ident and ident.status == IDENTITY_VALID) else None
        now = (f"当前 token → 公民 {cid}" if cid
               else "当前 token → 无（本次没带有效凭证）")
        return ("本次只登记公民，**不绑定会话**：写入归属由 token 决定"
                f"（{now}；未带凭证 → unattributed）。\n"
                "  register 不参与归属——它只建行 / 复用行，不改变任何已有凭证的归属。\n")

    def _credential_unmoved_note(self, ident, name):
        """register 的硬警示：**这只建了公民，凭证没换**。名不同才说，否则是噪音。

        为什么必须有这句（2026-10-24 真实踩坑）：一个会话 `register(anyi1)` 之后
        `whoami` 仍返回 `dsh-session-7ca3`，人以为 register 坏了，花了 1 小时才搞清
        "register = 建公民"与"凭证 = 本会话身份"是两件事。**行为一个字都不改**，
        只把这条事实写在回执里——看不见的语义，等于没有语义。

        判据 A（test_onboarding）：名不同 → 含「仍指向」；名相同 → 一个字都不多。
        所以提示的条件是**两个显示名的比较**，不是一个无条件的固定句：
        无条件加的话，register 自己那个名字时也会出现"仍然指向你自己"，纯属噪音。

        取当前公民的名字走 `fish.load_citizen`（已声明的读口），**不在本文件里新写
        一条 citizens SQL**：P1-a 判据 G 逐条冻结了 mcp_server.py 的 2 条 citizens
        语句，多一条就是"动了查询面却没声明"。
        """
        if not ident or ident.status != IDENTITY_VALID or not ident.citizen_id:
            return ""                      # 没凭证/凭证失效：由 call_tool 的统一提示负责
        citizen = fish.load_citizen(self.conn, ident.citizen_id)
        current = (citizen or {}).get("display_name") or ident.citizen_id
        if current == name:
            return ""                      # 就是你自己：不用警示（变异体在这一行红）
        return ("⚠ 这只建了公民：**当前会话的凭证仍指向 "
                f"{current}**（公民 {ident.citizen_id}）。\n"
                f"  要把本会话归到 {name}，必须重签凭证："
                f"`fish session start --alias {name}`\n"
                "  （MCP 面做不了这件事——P1-c 后身份只认 token，register 不再改归属。）\n")

    def _register_next_step(self, a, citizen_id):
        """register 的回执尾巴：告诉调用方"接下来靠什么拿到身份"。

        这段文案是判据的一部分（表里写着"返回提示：接下来请用 session start 拿凭证"）：
        公民 ≠ 身份，"登记了名字"不等于"之后的调用会归到你名下"。
        """
        lines = ["接下来调 `session_start` 工具拿会话凭证（token）——"
                 "**就在 MCP 面，不用去跑 CLI**。之后每次调用带 token=... 才会把归属记到这个公民。"]
        # 2026-10-25 加（`EVAL-261025-05`）：把 citizen 与 session_name 一起写出来。
        # 只写"去调 session_start"还是不够：不传 citizen 它会新建一个公民，
        # 那样 MCP 侧的身份与签发出来的凭证照样对不上。
        if citizen_id:
            lines.append(f"⚠ 调用时**两个参数都要给**：\n"
                         f"     session_start(citizen=\"{citizen_id}\", "
                         f"session_name=\"<你的稳定名字>\")")
            lines.append("   只给 session_name 会新建一个公民，只给 citizen 没有幂等锚。")
        if a.get("session_name"):
            lines.append(f"（session_name={a['session_name']} 已不再写入进程身份："
                         "进程被所有会话共享，会话区分改由 token 的 session_key 承担。）")
        if citizen_id:
            lines.append(f"（公民 ID：{citizen_id}；与 token 解出的 citizen_id 一致即同一身份。）")
        return "\n".join(lines) + "\n"

    def tool_rename_citizen(self, a, ident):
        """认领名字：把**当前凭证的公民**改成 name。

        为什么只允许改自己（2026-10-25）：
          P1-c 之后 MCP 面能确定"我是谁"（token → citizen_id），但**不能**确定
          "别人该叫什么"。给别人改名是主人的事（CLI `fish citizen rename`）——
          在这里开口子等于把"名字"变成可以远程改别人标签的通道。

        为什么必须有凭证：
          名字是挂在公民上的。没有身份就没有可改的对象；凭空建一条公民去承载
          这个名字，正是"孤儿公民"的来源（生产库 30 个公民里 13 个无凭证）。
          所以这里响亮拒绝，并给出去哪拿凭证的具体命令。
        """
        name = (a.get("name") or "").strip()
        if not name:
            raise ValueError("name 不能为空")
        if not ident or ident.status != IDENTITY_VALID or not ident.citizen_id:
            raise ValueError(
                "这条调用没有可用的会话凭证，无法确定要改谁的显示名。\n"
                "先跑 `fish session start` 拿 token，再带 token=... 调用本工具。\n"
                "（名字挂在公民上：没有身份就没有可改的对象，也不该为此新建一条公民）")

        res = fish.rename_citizen(self.conn, ident.citizen_id, name,
                                  actor=ident.session_key, reason=a.get("reason") or "")
        if not res["changed"]:
            return (f"名字本来就是 {name}，无需改动。\n"
                    f"（公民 {res['citizen_id']}）\n")

        lines = [f"已改名：{res['old_name']} → {res['new_name']}",
                 f"（公民 {res['citizen_id']}；citizen_id / 凭证 / 历史记忆归属均未改变）"]
        if res["same_name_exists"]:
            lines.append(f"⚠ 已有同名公民：{', '.join(res['same_name_exists'])}"
                         "（按决策 14，同名视作两个公民，不合并）")
        lines.append("这个名字现在可被 `whoami` / `find_citizen_by_name` 检索到。")
        return "\n".join(lines) + "\n"

    def _project_state_change(self, a, ident, target):
        """close / reopen 共用的实现。

        为什么合并成一份：两者**只差目标状态**。分开写必然有一处忘了"拒绝 retired"
        或忘了写事件——那正是"两份实现互相漂移"的老毛病（`record_memory` 曾经吃过
        一次同样的亏）。
        """
        prefix = (a.get("prefix") or "").strip()
        if not prefix:
            raise ValueError("prefix 不能为空")
        info = fish.resolve_name(self.conn, "project", prefix)
        name = info["canonical"] or prefix
        if not info["found"]:
            raise ValueError(f"项目不存在：{prefix}（闭合/重开只作用于已登记的前缀；"
                             "要在新名字上工作，先用 add_project）")
        if info["status"] == "retired":
            raise ValueError(
                f"{name} 已退役（retired）。退役是终态，不能再 close/reopen——"
                "要让新记忆进入，得开一个新前缀（ADR-003 下旧前缀永不回收）。")
        if info["status"] == target:
            return f"{name} 已经是 {target}，无需改动（幂等）。\n"
        res = fish.apply_project_status(self.conn, name, target,
                                        reason=a.get("reason") or "",
                                        actor=ident.citizen_id)
        if target == "closed":
            tail = "（不再接收新记忆；要再写，用 reopen_project 或 `fish project reopen`）\n"
        else:
            tail = "（可以继续写入；已有记忆一条都没动过）\n"
        return f"已把 {res['prefix']} 置为 {res['status']}。\n" + tail

    def tool_close_project(self, a, ident):
        return self._project_state_change(a, ident, "closed")

    def tool_reopen_project(self, a, ident):
        return self._project_state_change(a, ident, "active")

    def _log_access(self, codes, via, ident, payload=None):
        """写一条「按人」访问记录（13-推送系统设计 §2.1 的五个写入点都走它）。

        **失败静默**：日志是派生数据，写不进去最多是画像不准；若让它把工具调用
        打挂，AI 会直接放弃整条路径——那比数据缺失严重得多。
        citizen_id / session_key 由**本次调用的身份**提供（ident，来自 token）；
        无凭证时两者都是 None = 未归属——空着比编一个诚实。
        """
        try:
            fish.append_access_log(
                self.conn, codes, via=via,
                citizen_id=ident.citizen_id,
                session_key=ident.session_key,
                payload=payload)
        except Exception:
            pass

    # _touch 的 source → memory_access_log.via。push 与 inject 必须分开：
    #   inject = "这个 AI 主动调工具要注入包"
    #   push   = "注入包里展示了这条记忆"（被展示，不等于它主动要过）
    # 混成一个值，画像就分不清"主动来看"和"系统塞给它看的"（13 §2.1 的取舍）。
    _TOUCH_VIA = {"inject": "push", "read_body": "read_body"}

    def _log_push(self, codes, channel, ident, budget_tokens=None):
        """写一条「这次推送批次」的行（16 §5.6；论文 §6.3 说的那个缺口）。

        与 `_log_access(codes, "push")` 是两个维度，不是重复：
          那边记"这条被展示了"（谁看见的），这边记"这次推送里有什么"（批次与预算）。
        一次 inject 推 3 条只读 1 条，两处合起来才算得出采纳率。

        **失败静默**：与 _log_access 同理——派生数据写不进去最多是无数据可学，
        若让它把 inject 打挂，AI 会直接放弃注入包，那比缺数据严重得多。
        candidate_id 恒为 None：候选池还没实现，不编。
        """
        try:
            fish.append_push_log(
                self.conn, codes, channel=channel,
                citizen_id=ident.citizen_id, session_key=ident.session_key,
                budget_tokens=budget_tokens)
        except Exception as exc:      # 采纳率缺数 → 出声（静默清零）
            fish.warn_derived(self.conn, "append_push_log(mcp)", exc)

    def _accept_push(self, code, ident):
        """回填「用户接受了这次推送」（16 §5.6）：读了正文 / 校验了编码 = 被接受。

        判据（同一会话、24h 窗口内、尚未判过）在 fish.mark_push_accepted 里；
        这里只负责"失败静默"——读正文 / 校验编码的主流程一步都不许被日志拖住。

        无凭证（session_key=None）时不会误判：mark_push_accepted 只认 session_key
        相同的行，NULL 只匹配 NULL——"不知道是谁"不等于"谁都是"。
        """
        try:
            fish.mark_push_accepted(self.conn, code, session_key=ident.session_key)
        except Exception as exc:      # 采纳记录失败 → 出声（静默清零）
            fish.warn_derived(self.conn, "mark_push_accepted(mcp)", exc)

    def _touch(self, codes, ident, source="inject"):
        """注入包展示过的记忆 = 被看见 → 刷新 last_accessed_at（不增 access_count）。

        设计取舍：被反复注入说明有价值，应该抗衰减；但不等于"被主动查询"，
        所以不增加 access_count（那是 recall / resolve 的语义）。

        2026-10-22（修 07 D-12）：改为统一走 `fish.record_access`，顺带写
        `memory.accessed` 事件——访问状态从此可重放，不再只存在于投影里。
        2026-10-24（13 §2.1）：再加写按人访问记录。read_body 走这里时 via 是
        read_body 而不是 push——"读了正文"和"被注入包展示"是两回事。
        """
        try:
            fish.record_access(self.conn, codes, source, counted=False,
                               actor=ident.citizen_id)
        except Exception as exc:      # 访问热度缺数 → 出声（静默清零）
            fish.warn_derived(self.conn, "record_access(mcp)", exc)
        via = self._TOUCH_VIA.get(source)
        if via:
            self._log_access(codes, via, ident)

    def tool_inject(self, a, ident):
        # R1（EVAL-261024-02）：project 是**可选**的显式声明，直接透传给
        # build_injection_pack 的最高优先档；不传时由档期/兜底决定，并在 L1 里标明来源。
        # 刻意**不**在 MCP 侧猜 cwd：本进程被宿主所有会话共享，进程 cwd 不是调用方
        # 的工作目录，拿它做 workspace_map 匹配等于把"服务器启动目录"说成"AI 在哪儿干活"。
        #
        # 2026-10-26（FB-02）：`sections`（只构建列出的段）+ `include_board`（便捷布尔）。
        # 归一在 `_inject_sections_arg` 里做一次——`sections` 与 `include_board` 都写时
        # 只能有一个结果，两处各自处理必然出现"说不要看板、包里有看板"。
        pack, meta = fish.build_injection_pack(
            self.conn, self.root, ident.citizen_id, a.get("budget"),
            # P0-3：把真实会话键透传下去——看板快照按会话记，同公民多窗口互不吞推送
            session_key=getattr(ident, "session_key", None),
            project=a.get("project"), sections=_inject_sections_arg(a))
        recent = meta.get("recent_codes") or []
        # 13 §2.1：inject 是"主动调工具"，push 是"注入包里展示了这条"——两条都要记
        self._log_access(recent, "inject", ident)
        self._touch(recent, ident)
        # 16 §5.6：再记「这次推送批次」。**凡是在包里出现过的编码都要记**——
        # 只记 L1 会让 ③规矩 / ④注意 / ⑤错题本 里展示过的记忆永远算不出采纳率，
        # 而这三段比 L1 更该记：它们带标题、是 AI 真会读的实质内容。
        # 2026-10-25 · P1-a 修正：原实现是 `recent + index_codes`，而 index_codes 来自
        # 「今日编码」段（只有编码没有标题，是坐标不是信息，已按 `31` §2 删除）——
        # 结果真正该记的 ③④⑤ 反而漏了。这里一并修正。
        # 去重保序：同一条编码可能同时出现在多段，一次展示只记一行。
        shown = list(dict.fromkeys(
            list(recent)
            + list(meta.get("rules_codes") or [])
            + list(meta.get("attention_codes") or [])
            + list(meta.get("lesson_codes") or [])
            # 段⑥ 候选（2026-10-25 · P1-a）：它们也是"在包里出现过"，
            # 漏记会让候选类的采纳率永远算不出来（而这正是要度量的对象）。
            + [c["code"] for c in (meta.get("push_candidates") or [])]))
        self._log_push(shown, "inject", ident, budget_tokens=meta.get("tokens"))
        # 2026-10-26（FB-03）：注入包的回执也要有那一行「身份来源」。
        # 位置刻意放在包正文之后、`<!-- tokens/budget -->` 元信息注释**之前**：
        #   · 包正文（含 L0 身份段）一个字都不动 —— 既有格式与判据全部保持；
        #   · 它属于"这次注入包凭什么代表我"的正文事实，不是元信息。
        return (pack + "\n" + self._identity_source_line(ident) + "\n"
                + f"<!-- {meta['tokens']}/{meta['budget']} token -->\n")

    def tool_session_start(self, a, ident):
        """在 MCP 面签发一条会话凭证 —— 明文只在这**一次**返回。

        ## 为什么要有这个工具（2026-10-25 · 外部评估 `EVAL-261025-05`）

        在此之前，MCP 面 11 个工具里**没有任何一个能产出 token**：
        `whoami` / `inject` 有 token **入参**，但签发口在 CLI 的 `session start`。
        于是 AI 只能离开 MCP 去跑命令行——而两边 `host_session_key` 不同，
        `find_or_create` 会**新建一个公民**，两条通道的归属从此对不上、且无法合并。

        实测代价（`EVAL-261025-05` 亲历）：先试 add_project（被拒）→ 试 healthcheck
        （无 token 信息）→ 最后才想到 CLI；拿到后发现 `citizen_id` 与 MCP 侧不是同一个。

        ## 为什么这里能拿到 `session_key`（CLI 能、MCP 不能的那个东西）

        MCP 进程的 env 被宿主 scrub 掉了，所以 `build_session_identity` 的
        `machine` 证据这一档永远走不到。但它的第二档（`alias` → `self-reported`）
        是**调用方显式给的**——所以 `session_name` 就是这里的幂等锚：
        同一个 AI 传同一个 `session_name`，拿到的永远是同一个公民。

        **required 里只放 `session_name`**：它是唯一必须的（不传就每次建新公民，
        那正是这个工具要治的病）。`citizen` / `display_name` 二选一，
        都不给时用 `session_name` 当显示名。
        """
        session_name = (a.get("session_name") or "").strip()
        if not session_name:
            raise ValueError(
                "session_name 必填——它是幂等锚：不传的话每次调用都会建一个新公民，"
                "而这正是本工具要解决的问题。建议用能唯一标识你的一条稳定名字"
                "（如 your-name-dev / your-name-review）。")
        citizen = (a.get("citizen") or "").strip() or None
        display_name = (a.get("display_name") or "").strip() or session_name
        project = (a.get("project") or "").strip() or None
        kind = a.get("kind") or "visitor"
        if kind not in fish.KINDS:
            raise ValueError(f"kind 只能是 {fish.KINDS}")

        try:
            # `display_name` 不单独传：`start_session` 里它会由 `alias`（= session_name）
            # 决定，或者在没给 alias 时自动生成。多传一个参数只会制造第二处真相。
            _ = display_name          # 只在下面的回执里用（说明"你的显示名会是这个"）
            res = fish.start_session(
                self.conn,
                projects=[project] if project else None,
                citizen=citizen, alias=session_name, kind=kind)
        except ValueError as e:
            # `--citizen` 指向不存在/已归档的公民会在这里响亮失败——
            # 静默绑错比不绑更糟（它看起来成功了）。
            raise ValueError(f"签发失败：{e}") from e

        # 2026-10-26（FB-04）：`expires_at=None` 是「永不过期」的既有语义
        # （`fish._new_expires_at` 里 ttl<=0 → None，与 decay.*=0 同一套约定）。
        # 旧实现直接把它塞进 f-string，于是永久 token 的回执印成
        # `有效期至 None`——**把"没有到期日"说成"到期日是个 None"**，读的人会以为
        # 签发坏了。空值必须翻译成人话；有值则原样给 ISO（脚本要能解析，不做换算）。
        exp_txt = (f"有效期至 {res['expires_at']}" if res.get("expires_at")
                   else "永不过期（随会话 end 吊销）")
        lines = [
            f"已签发会话凭证（公民 {res['citizen_id']}"
            + ("，**本次新建**" if res.get("created") else "，复用已有") + "）。",
            "",
            f"token: {res['token']}",
            "",
            "⚠ 明文**只出现这一次**：库表里只存 sha256，丢了就重跑本工具"
            "（同一个 session_name → 同一个公民，只换凭证）。",
            f"　之后每次调用工具都带上 token=... 。{exp_txt}。",
            "",
            f"- 会话键：{res['session_key']}（来自 session_name={session_name}）",
            f"- 显示名：{res['display_name']}（形态 {kind}）",
        ]
        # 2026-10-26（FB-08）：同名在用公民。文案与 CLI 共用 `fish.same_name_notice`
        # ——两个入口各写一版，就会出现"CLI 说可用 --citizen、MCP 说没这回事"的分裂。
        for it in res.get("name_conflicts") or []:
            lines.append(fish.same_name_notice(it["citizen_id"], it["kind"]))
        if res.get("project"):
            lines.append(f"- 已开档期：{res['project']}")
        # 2026-10-26（T03）：会话开场自动签到（自动行算登记，用户拍板）。
        # 三条纪律：失败静默（签发是主流程）、不写进程状态、不重复建行。
        try:
            checkin = fish.board_auto_checkin(
                self.conn, res["citizen_id"],
                project=(res.get("project") or None),
                session_key=res.get("session_key"), source="session_start")
            if checkin.get("created"):
                lines.append("- 看板已自动登记（自动行；补一句 doing 别人才能判断撞车）")
            elif checkin.get("refreshed"):
                lines.append("- 看板已刷新你的在场时间")
        except Exception as exc:          # noqa: BLE001 —— 看板失败不影响签发，但要出声
            fish.warn_derived(self.conn, "board_auto_checkin", exc)
        if res.get("created"):
            lines.append("- 这是新建的公民：**下次务必带同一个 session_name**，"
                         "否则会再建一个（归属会分裂）。")
        for s in (res.get("stints") or []):
            lines.append(f"- 档期：{s['project']}（{s['action']}）")
        return "\n".join(lines) + "\n"

    def tool_board(self, a, ident):
        """看板：`show` 看谁在动哪块，`checkin` 登记自己。

        这是 2026-10-25 补的最后一个闭环件。在此之前看板**看得见、动不了**：
        注入包会说「你还没签到」，但 MCP 面 15 个工具里没有一个是能签到的。
        用户裁定补上（工具数 15 → 16）。

        ## 为什么 checkin 必须要凭证

        签到的产出是 `presence` 表里**一行带公民名的记录**——"谁在这里"是这个表的
        全部意义。没有凭证就只能记成 `unattributed`，那比不签到更糟：
        看板上会出现一个"有人在动但不知道是谁"的幽灵行，别人照样无法判断会不会撞车。

        ## 为什么 show 不需要凭证

        看是只读的。要求凭证才能看，等于逼 AI 在"先看"和"先签"之间二选一——
        而正确顺序本来就是**先看有没有人占着**，再签。
        """
        action = (a.get("action") or "show").strip()
        project = (a.get("project") or "").strip() or None
        scope, key = ("project", project) if project else ("root", "")

        if action == "checkin":
            if ident.status != IDENTITY_VALID or not ident.citizen_id:
                raise ValueError(
                    "签到需要会话凭证（看板上要记'是谁'）。先调 `session_start` 拿 token，"
                    "这次调用带上 token=... 再 checkin。")
            doing = (a.get("doing") or "").strip()
            if not doing:
                raise ValueError(
                    "doing 必填——看板的核心价值就是让别人靠这一句判断会不会跟你撞车；"
                    "没有 doing 的在场行等于没签到。")
            # 2026-10-26（撞车自撞修复 T02 · 设计附录 A）：会话键**从 token 解析**，
            # 不做成入参——手填就是一个人可以撒谎的身份字段。`agent_id` 同步带上，
            # 因为外部读者（含历史数据）一直把 agent_id 当"会话/宿主侧标识"读。
            board = fish.board_checkin(
                self.conn, ident.citizen_id, doing=doing,
                surface=(a.get("surface") or "").strip(), scope=scope, scope_key=key,
                status=(a.get("status") or "active"),
                session_key=(ident.session_key or ""),
                agent_id=(ident.session_key or None))
            st = a.get("status") or "active"
            fish.board_post(self.conn, board, ident.citizen_id,
                            "checkin" if st == "active" else "left", doing)
            path, size, split = fish.board_render(self.conn, self.root, board)
            # 签到后立刻把"撞车"告诉它——这是 checkin 的第二个价值
            # （第一个是让别人看见你）。不提的话，AI 要自己想到去 show 才看得见。
            ovs = fish.board_overlaps(self.conn, board) if st == "active" else []
            warn = ""
            if ovs:
                # 2026-10-26（T03）：重叠警告用**可读名**（原来打 `o['a']/o['b']` 的 ULID，
                # 人看到的是两串 26 位机器码，等于没告诉"该去找谁"）。
                warn = "\n⚠ 注意：你的占用面与这些人重叠——\n" + "\n".join(
                    f"  · `{o['at']}` ← {o.get('a_label', o['a'])} ↔ "
                    f"{o.get('b_label', o['b'])}" for o in ovs[:5])
            verb = "已签到" if st == "active" else "已下板（之后不再计入重叠检测）"
            labels = fish.citizen_labels(self.conn, [ident.citizen_id])
            return (f"{verb}：{board['path']}（{size} B"
                    + ("，已按字节切分" if split else "") + "）\n"
                    f"　{labels.get(ident.citizen_id) or ident.citizen_id} 在做「{doing}」\n"
                    "　（下次开工前重跑一次 checkin，把 doing 更新成新任务）" + warn)

        if action == "post":
            # 2026-10-25（`EVAL-261025-10`）：**MCP 面必须有 post**。
            # 旧实现只有 CLI 有，而 MCP 是主路径（没人会去跑 CLI）——
            # 于是今日动态"永远只有 checkin 行"，任务做完没法在看板上标 done，
            # 看板退化成纯在场表，丢掉"谁在动哪块"里"**动到哪了**"那一半。
            if ident.status != IDENTITY_VALID or not ident.citizen_id:
                raise ValueError("写动态需要会话凭证。先调 `session_start` 拿 token。")
            text = (a.get("text") or "").strip()
            if not text:
                raise ValueError("text 必填——动态内容不能为空。")
            kind = (a.get("kind") or "note").strip()
            if kind not in ("progress", "done", "note"):
                raise ValueError(f"kind 只能是 progress/done/note，收到 {kind!r}")
            board = fish.board_ensure(self.conn, scope, key)
            if board["status"] == "closed":
                raise ValueError(f"看板已归档（closed），不再接收动态：{board['path']}")
            pid = fish.board_post(self.conn, board, ident.citizen_id, kind, text)
            if pid is None:
                raise ValueError("动态内容为空（去掉空白后没有内容）")
            path, size, split = fish.board_render(self.conn, self.root, board)
            return (f"已记录 {kind}：{board['path']}（{size} B" 
                    + ("，已按字节切分" if split else "") + "）\n"
                    f"　内容：{text[:80]}" + ("…" if len(text) > 80 else ""))

        if action == "simulate":
            # 2026-10-25（`EVAL-261025-16` 建议 2）：让撞车检测**可自助验证**。
            # 只有一个会话时永远测不出来，于是"以为有保护"——那是最危险的状态。
            surface = (a.get("surface") or "").strip()
            if not surface:
                raise ValueError(
                    "simulate 需要 surface：给一组假设的占用面（逗号分隔），"
                    "我告诉你会不会跟在场的人撞。**不写库**。")
            board = fish.board_ensure(self.conn, scope, key)
            res = fish.board_simulate(
                self.conn, board, surface,
                exclude_citizen=(ident.citizen_id if ident.status == IDENTITY_VALID
                                 else None),
                assume_surface=(a.get("assume_surface") or "").strip() or None)
            if not res["my_surface"]:
                return "surface 解析后为空，没法模拟。\n"
            lines = [f"模拟：如果你占 {'、'.join(res['my_surface'][:3])}"
                     f"{'…' if len(res['my_surface']) > 3 else ''}",
                     f"（对比了 {res['checked']} 个有效在场行，**未写库**）", ""]
            if res["hits"]:
                lines.append("⚠ 会撞上：")
                for h in res["hits"][:7]:
                    lines.append(f"  · `{h['at']}` ← {h['who']}"
                                 f"（{h['doing'] or '未写 doing'}）")
                lines.append("")
                lines.append("→ 真撞车时 checkin 就是这个样子。"
                             "现在看到的说明**检测在工作**。")
            else:
                lines.append("✅ 不会撞：当前没人声明与这组面重叠的面。")
                if res["checked"] == 0:
                    lines.append("")
                    lines.append("⚠ 但**对比了 0 个人**，所以这次什么也没验证。"
                                 "想当场确认检测真的在工作，加 `assume_surface` 假设一个占用者：")
                    lines.append(f'  board(action="simulate", surface="{res["my_surface"][0] if res["my_surface"] else "p0"}", '
                                 'assume_surface="' + (res["my_surface"][0] if res["my_surface"] else "p0") + '/somefile.py")')
                else:
                    lines.append("（这**不代表**检测坏了——是真没人占。"
                                 "想确认它工作，用 `assume_surface` 假设一个占用者。）")
            return "\n".join(lines) + "\n"

        # show（默认）
        info = fish.board_show(self.conn, scope, key)
        # P0-2：看了看板 = 接受了"看板有更新"类推送（按前缀回填，同会话/窗口内）。
        # 只在有凭证时回填——没有会话键的回填会把"别人看的"算到 unattributed 头上。
        try:
            if ident.status == IDENTITY_VALID:
                fish.mark_push_accepted_prefix(
                    self.conn, "board:%", session_key=ident.session_key)
        except Exception as exc:       # noqa: BLE001 —— 回填失败要出声（静默清零）
            fish.warn_derived(self.conn, "mark_push_accepted_prefix(board)", exc)
        return fish._render_one(self.conn, info["board"], info["posts"]) + "\n"

    # ------------------------------------------------------------ 知识库（B3 · T05）

    def _kb_header(self, third):
        """回执固定三行头（§3.3）：`source=` / `as_of=` / 第三行由调用方给。

        为什么头放在 MCP 层而不是 `fish.kb_*` 里：CLI 的回执是给人看的（可以有标题、可以长），
        MCP 的回执是给模型看的，**AI 必须第一眼就知道这段内容来自哪里、什么时候、代价多大**。
        三行是判据认的最小集，多一个字都不该是这一层的事。
        """
        return [KB_SOURCE_LINE, f"as_of={fish.iso_local()}", third]

    def _kb_tool_vault(self, a):
        """解析 `vault` 入参（名字或 id；缺省=默认库）。取不到 → `KBError('vault_missing')`。"""
        key = (a.get("vault") or "").strip() or None
        return fish.kb_vault_get(self.conn, key)

    def _kb_int_arg(self, a, name, *, default, lo, hi):
        """读一个整数入参并夹到 `[lo, hi]`；非整数 → ValueError（工具错误，不崩服务）。

        为什么要夹：schema 里写了 minimum/maximum，但 **schema 不是判据**——手写 JSON 的
        客户端可以不遵守它。depth=100 会让一次调用炸成几万行，limit=999 会把回执撑爆，
        而这两件事都不会报错。
        """
        raw = a.get(name)
        if raw in (None, ""):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须是整数，收到 {raw!r}")
        return max(lo, min(value, hi))

    def tool_kb(self, a, ident):
        """知识库**只读**面：vaults / tree / open / search（§3.3 / §4.1-4.3）。

        三件必须做到的事（判据认它们）：
          ① 回执头固定三行 `source=` / `as_of=` / `scan=`（open 为 truncated/shown）；
          ② 每次读都落 `kb_access`（`via='mcp:kb'`；无 token → `'unattributed'`）；
          ③ **不打印正文以外的隐私、不把 query 原文写进审计**（§5.6 白名单）——
             `fish.kb_search` 只写 `query_hash`，本层也不把 args 交给 `_audit`。

        为什么与 `kb_write` 分成两个工具：读与写是**两个权限位**（裁决 C2）。
        合成一个之后，"只想看目录"的调用也会拿到一份写权限的说明书，而说明书就是引子。
        """
        action = (a.get("action") or "").strip()
        if action not in KB_TOOL_ACTIONS:
            raise ValueError(f"action 只能是 {'/'.join(KB_TOOL_ACTIONS)}，收到 {action!r}")
        citizen = ident.citizen_id          # None → kb_log_access 里映射成 'unattributed'
        max_chars = self._kb_int_arg(a, "max_chars", default=KB_TOOL_MAX_CHARS,
                                     lo=1, hi=200000)

        if action == "vaults":
            vaults = fish.kb_vault_list(self.conn)
            files = sum(int(v.get("last_scan_files") or 0) for v in vaults)
            total = sum(int(v.get("last_scan_bytes") or 0) for v in vaults)
            lines = self._kb_header(f"scan={files} files / {total} bytes / 0 ms") + [
                "", f"# 知识库清单（{len(vaults)} 个）", ""]
            if not vaults:
                lines.append("- （还没有登记任何知识库：`fish kb vault add --name <n> "
                             "--path <绝对路径> --mode ro`）")
            for vault in vaults:
                flags = ["default"] if vault.get("is_default") else []
                flags.append("rw" if vault.get("mode") == "rw" else "ro")
                lines.append(
                    f"- {vault['name']}｜mode={vault['mode']}｜{'/'.join(flags)}"
                    f"｜`{vault['path']}`"
                    f"｜最近扫描 {vault.get('last_scan_at') or '（未扫描）'}"
                    f"｜vault_id={vault['vault_id']}")
            lines += [
                "",
                "（这次只列注册行，**一个目录都没扫**：scan= 里的数字来自 last_scan 旧读数，"
                "要现读目录请用 action=tree。）",
                "（知识库不进 recall / inject / 全文索引；找 fish 记忆请用 recall。）",
            ]
            # 审计失败静默（K4）：列清单写不进流水不该让"看一眼有哪些库"失败。
            fish._kb_log_quiet(self.conn, None, rel="", action="vaults",
                               citizen_id=citizen, via="mcp:kb",
                               meta={"reason": "list_vaults"})
            return "\n".join(lines) + "\n"

        vault = self._kb_tool_vault(a)
        # P0-2：读了知识库 = 接受了"知识库命中"类推送（按前缀回填；同会话/窗口内）。
        # 与 board 同一口径：有凭证才回填，避免把别人的读取算成 unattributed。
        try:
            if ident.status == IDENTITY_VALID:
                fish.mark_push_accepted_prefix(
                    self.conn, "kb:%", session_key=ident.session_key)
        except Exception as exc:       # noqa: BLE001 —— 回填失败要出声（静默清零）
            fish.warn_derived(self.conn, "mark_push_accepted_prefix(kb)", exc)

        if action == "tree":
            res = fish.kb_tree(self.conn, vault, rel=(a.get("path") or "").strip(),
                               depth=self._kb_int_arg(a, "depth", default=2, lo=1,
                                                      hi=fish.KB_TREE_MAX_DEPTH),
                               max_chars=max_chars, citizen_id=citizen, via="mcp:kb")
            scan = res["scan"] or {}
            if scan.get("stale") or scan.get("files") is None:
                third = ("scan=0 files / 0 bytes / 0 ms（离线：只有上次扫描读数 "
                         f"{vault.get('last_scan_at') or '无'}）")
            else:
                third = (f"scan={scan['files']} files / {scan['bytes']} bytes / "
                         f"{scan['ms']} ms")
            lines = self._kb_header(third) + ["", res["text"], ""]
            lines.append(f"（chars={res['chars']} tokens={res['tokens']} "
                         f"truncated={str(res['truncated']).lower()} "
                         f"depth={res['depth']}"
                         + ("｜离线：上次扫描数" if scan.get("stale") else "")
                         + "）")
            return "\n".join(lines) + "\n"

        if action == "open":
            rel = (a.get("path") or "").strip()
            if not rel:
                raise ValueError("open 需要 path：vault 内相对路径（用 / 分隔，不能是绝对路径）")
            # S8（第四轮 · 安全 N6）：`allow_sensitive` 是**二次授权**，不能由调用方自授权。
            # 与 kb_write 同口径：没有有效会话凭证就不许读敏感正文——否则"写要凭证、
            # 读凭证反而不要"就是一条口径裂缝（审计行只能记 unattributed，追不到人）。
            # N7（第五轮 · 安全 🟡）：先做**严格**布尔解析——`bool("false") is True` 会把
            # "拒绝"读成"同意"（宿主/模板把布尔渲染成字符串很常见）；`None`（不带键）→
            # False，与 schema 的 default:False 同义；模糊写法（`"yes"`）→ bad_usage。
            allow_sensitive = fish._strict_bool(a.get("allow_sensitive"), "allow_sensitive")
            if allow_sensitive and ident.status != IDENTITY_VALID:
                raise ValueError(
                    "读敏感文件需要会话凭证（否则这次读取只能记成 unattributed、追不到人）。"
                    "先调 `session_start` 拿 token，这次调用带上 token=... 再 open。")
            # MCP 默认 8000 字符（`kb` schema 里声明的默认值）：回执要进模型上下文。
            # 想读更长就**显式**传 max_chars——CLI 的默认是 64000，两个面的口径不同，这里如实说。
            res = fish.kb_open(self.conn, vault, rel, max_chars=max_chars,
                               allow_sensitive=allow_sensitive,
                               citizen_id=citizen, via="mcp:kb")
            third = (f"truncated={str(res['truncated']).lower()} / "
                     f"shown={res['shown']}/{res['limit']} bytes")
            lines = self._kb_header(third) + [""]
            lines += [f"> {s}" for s in res["statements"]]
            lines += ["", f"（rel={res['rel']}｜bytes={res['bytes']}"
                          f"｜sha256={str(res['sha256'])[:12]}"
                          f"｜encoding={res['encoding']}｜eol={res['eol']}）", ""]
            lines.append(res["text"])
            if res["truncated"]:
                lines.append("")
                lines.append(f"（已截断：显示 {res['chars_shown']}/{res['chars_total']} 字；"
                             f"要更多请显式传 max_chars，当前上限 {max_chars} 字）")
            return "\n".join(lines) + "\n"

        # search（唯一剩下的分支；上面已经拦掉未知 action）
        query = (a.get("query") or "").strip()
        if not query:
            raise ValueError("search 需要 query（字面匹配，非语义；大小写不敏感）")
        res = fish.kb_search(self.conn, vault, query,
                             limit=self._kb_int_arg(a, "limit", default=10, lo=1,
                                                    hi=fish.KB_MAX_LIMIT),
                             max_chars=max_chars, citizen_id=citizen, via="mcp:kb")
        scan = res["scanned"]
        third = (f"scan={scan['files']} files / {scan['bytes']} bytes / "
                 f"{scan['ms']} ms")
        lines = self._kb_header(third) + ["", res["text"], ""]
        lines.append(f"（命中 {res['total']} 个文件，显示 {res['shown']} 个"
                     f"｜chars={res['chars']} tokens={res['tokens']} "
                     f"truncated={str(res['truncated']).lower()}）")
        return "\n".join(lines) + "\n"

    def tool_kb_write(self, a, ident):
        """知识库**写**面：create / append / update（§3.3 / §4.4）。

        本方法刻意**不重复实现**写逻辑：备份、原子写、写后重读校验、EOL 沿用、
        frontmatter 合并全在 `fish.kb_write` 里——CLI 与 MCP 走同一条路。
        "两条通道各自实现一遍"是本项目最贵的 bug 形态（同一件事两种语义，谁都不知道哪条对）。

        这里只做三件事：
          ① 工具入参 → `fish.kb_write` 的关键字（类型先校验，错误早于写入暴露）；
          ② 归属折算成 `citizen_id or 'unattributed'`（K3；MCP 侧只做一次映射）；
          ③ 回执整成 CLI 同款三段式（改了什么 / 备份在哪 / 怎么撤）。

        `mode=ro` 的判定**只在 `fish.kb_write` 里**（唯一真相）：它抛
        `KBError('mode_ro', "…是 mode=ro，拒绝写入")`，文案里的 `mode=ro` 原样进 isError。

        ## 身份门槛（R11 · 安全 F3）

        与 `board checkin` 同口径：**没有有效 token 就不许写**。两个理由都不是形式主义：
          ① 写操作必须能回答"是哪个公民的哪个会话改的"——审计只记 `unattributed`
             等于给知识库开了一条无法追责的写通道；
          ② 同一产品里"写看板要凭证、写知识库不要凭证"是口径分裂，不是设计选择。
        CLI 面仍保留"本地可信"（不要求 token）：那是人自己在终端里跑。
        """
        if ident.status != IDENTITY_VALID or not ident.citizen_id:
            raise ValueError(
                "写知识库需要会话凭证（审计要记'是谁、哪个会话改的'）。"
                "先调 `session_start` 拿 token，这次调用带上 token=... 再 write。")
        action = (a.get("action") or "").strip()
        if action not in KB_WRITE_ACTIONS:
            raise ValueError(f"action 只能是 {'/'.join(KB_WRITE_ACTIONS)}，收到 {action!r}")
        rel = (a.get("path") or "").strip()
        if not rel:
            raise ValueError("path 必填：vault 内相对路径（用 / 分隔，不能是绝对路径）")
        frontmatter = a.get("frontmatter")
        if frontmatter in (None, ""):
            frontmatter = {}
        if not isinstance(frontmatter, dict):
            raise ValueError("frontmatter 必须是对象（键 → 标量值），收到 "
                             f"{type(frontmatter).__name__}")
        content = a.get("content")
        if content is not None:
            content = str(content)
        reason = (a.get("reason") or "").strip() or None
        if reason:
            # schema 写的是 ≤200 字；超长**截断**而不是拒绝：理由不该成为写不进去的原因，
            # 但审计里也不能因此长出无界文本（§5.6）。
            reason = reason[:200]
        vault = self._kb_tool_vault(a)
        # N7（第五轮 · 安全 🟡）：写面与读面共用同一解析（`fish._strict_bool`）——
        # `"false"` 是拒绝、模糊写法 `bad_usage`，不是"非空字符串即同意"。
        allow_sensitive = fish._strict_bool(a.get("allow_sensitive"), "allow_sensitive")
        res = fish.kb_write(self.conn, self.root, vault, rel, action=action,
                            content=content, frontmatter=frontmatter,
                            base_sha256=(a.get("base_sha256") or "").strip() or None,
                            reason=reason, allow_sensitive=allow_sensitive,
                            citizen_id=ident.citizen_id,
                            account_id=ident.session_key,
                            via="mcp:kb_write")
        lines = [
            f"① 改了：{res['action']} `{res['rel']}` "
            f"{res['bytes_before'] or 0} → {res['bytes_after']} B"
            f"（sha256 {str(res['sha256_after'])[:12]}，eol={res['eol']}，"
            f"校验={'通过' if res['verified'] else '失败'}）",
            f"② 备份：{res['backup_path']}",
            f"③ 撤销：{res['undo']}",
        ]
        for item in res.get("same_name_warnings") or []:
            lines.append(f"⚠ 同名笔记已存在：`{item['rel']}`（未覆盖，仅提示）")
        for warning in res.get("warnings") or []:
            lines.append(f"⚠ {warning}")
        lines.append(f"（write_id={res.get('write_id')}｜via=mcp:kb_write"
                     f"｜归属={ident.citizen_id}｜会话={ident.session_key}）")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ 对话语料（T03）

    def _corpus_header(self, third):
        """语料回执固定三行头（与 kb 同一形态）：`source=` / `as_of=` / 读数。"""
        return [CORPUS_SOURCE_LINE, f"as_of={fish.iso_local()}", third]

    def tool_corpus(self, a, ident):
        """对话语料**只读**面：search / read / stats（设计 §6）。

        三条纪律（判据认它们）：
          ① 只有三个只读动作——`ingest/forget/rebuild` 属于 CLI（写数据面的事不该由模型
             随手触发，否则一次误调用就是不可解释的删除）；
          ② 回执头三行 `source=` / `as_of=` / 读数（AI 必须第一眼知道这段来自哪里）；
          ③ `read` 成功后按**精确 code** 回填 accepted（D12）：前缀回填会把同窗口内
             其它 conv 推送一并置 accepted，让采纳率虚高（K7）。
        """
        action = (a.get("action") or "").strip()
        if action not in CORPUS_TOOL_ACTIONS:
            raise ValueError(f"action 只能是 {'/'.join(CORPUS_TOOL_ACTIONS)}，收到 {action!r}")
        citizen, session_key = ident.citizen_id, ident.session_key
        if action == "stats":
            res = fish_corpus.stats(root=self.root)
            lines = self._corpus_header(
                f"segments={res['segments']} sessions={res['sessions']} "
                f"degraded={len(res['degraded'])} orphan={len(res['orphan'])}") + [""]
            if res.get("db_missing"):
                lines.append("- （语料库还没建：先跑 `fish corpus ingest`）")
            for host, slot in sorted((res.get("by_host") or {}).items()):
                lines.append(f"- {host}：文件 {slot.get('sources', 0)} / 会话 "
                             f"{slot.get('sessions', 0)} / 段 {slot.get('segments', 0)}")
            lines.append(f"- 滞后 {res.get('lag_sec')} 秒 / 库体积 "
                         f"{(res.get('db_bytes') or {}).get('total', 0)} B")
            for row in res.get("degraded") or []:
                lines.append(f"⚠ 降级 {row['host']} {row['path']} {row['reason_code']}")
            for row in res.get("unmapped_cwds") or []:
                lines.append(f"⚠ 未归属 cwd：{row['workspace']}（{row['sessions']} 会话）")
            return "\n".join(lines) + "\n"
        if action == "search":
            query = (a.get("query") or "").strip()
            if not query:
                raise ValueError("search 需要 query（关键词；中文按 bigram 切词）")
            filters = {}
            for key in ("host", "project", "role"):
                value = a.get(key)
                if value is None:
                    continue
                # M2（2026-10-27）：`project=""` 是**有效过滤值**（只要未归属的语料），
                # 不是"不过滤"。旧写法 `value not in (None, "")` 把空串当缺省，
                # 于是"只看未归属"变成了"看全部"——门面正确、门面之上漏了。
                if key != "project" and value == "":
                    continue
                filters[key] = [str(value)]
            res = fish_corpus.search(
                query, filters=filters or None,
                limit=self._kb_int_arg(a, "limit", default=8, lo=1,
                                       hi=fish_corpus.SEARCH_MAX_LIMIT),
                ranker=(a.get("ranker") or "default"), root=self.root,
                citizen_id=citizen, session_key=session_key, via="mcp:corpus")
            meta = res["meta"]
            lines = self._corpus_header(
                f"hits={meta['returned']} total={meta['total_matched']} "
                f"ranker={meta['ranker']} truncated={str(meta['truncated']).lower()}") + [""]
            if not res["hits"]:
                lines.append("- （没有命中：换个词，或这台机器还没采过语料）")
            for hit in res["hits"]:
                lines.append(f"- {hit['pointer']}｜score={hit['score']:.3f}｜"
                             f"{hit['role']}｜{hit['occurred_at']}｜"
                             f"{hit['project'] or '未归属'}")
                lines.append(f"  {hit['snippet']}")
                lines.append(f"  为什么：{'；'.join(hit['why'])}")
            for item in meta["degraded"]:
                lines.append(f"⚠ {item.get('reason')}：{item.get('note') or ''}")
            return "\n".join(lines) + "\n"
        pointer = (a.get("pointer") or "").strip()
        if not pointer:
            raise ValueError("read 需要 pointer（log:<host>:<sid>#seq=123 或 conv:<host>:<sid8>#seq=123）")
        try:
            res = fish_corpus.read(
                pointer,
                max_chars=self._kb_int_arg(a, "max_chars", default=8000, lo=1, hi=200000),
                root=self.root, citizen_id=citizen, session_key=session_key,
                via="mcp:corpus")
        except fish_corpus.PointerError as exc:
            raise ValueError(f"{exc.code}：{exc}"
                             + (f"（候选：{', '.join(exc.candidates)}）"
                                if exc.candidates else ""))
        # P0-2/D12：读了正文 = 接受了那条推送（精确 code，不用前缀）
        try:
            if ident.status == IDENTITY_VALID:
                fish.mark_push_accepted(self.conn, [res["code"]],
                                        session_key=ident.session_key)
        except Exception as exc:       # noqa: BLE001 —— 回填失败要出声（静默清零）
            fish.warn_derived(self.conn, "mark_push_accepted(kb)", exc)
        lines = self._corpus_header(
            f"role={res['role']} chars={res['chars']} truncated="
            f"{str(res['truncated']).lower()} shadowed={str(bool(res['shadowed'])).lower()}") + [
            f"pointer={res['pointer']}",
            f"code={res['code']}",
            f"会话 {res['session_title'] or res['sid8']}｜项目 {res['project'] or '未归属'}"
            f"｜{res['occurred_at']}｜源文件{'缺失（正文来自投影）' if res['source']['missing'] else '在'}",
            "",
            res["text"],
            "",
        ]
        if res["truncated"]:
            lines.append(f"（已截断；库内 {res['chars']} 字，max_chars 见入参）")
        if res["source"]["missing"]:
            lines.append(f"（{res['source']['note']}）")
        return "\n".join(lines) + "\n"

    def tool_healthcheck(self, a, ident):
        total = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        mem = self.conn.execute("SELECT COUNT(*) FROM code_index WHERE status='active'").fetchone()[0]
        dor = self.conn.execute("SELECT COUNT(*) FROM code_index WHERE dormant=1").fetchone()[0]
        broken = [c for c, fp in self.conn.execute(
            "SELECT code,file_path FROM code_index").fetchall()
            if fp and not (self.root / fp).exists()]
        return f"事件 {total} / 记忆 {mem} / 休眠 {dor} / 断链 {len(broken)}\n"

    def call_tool(self, name, args, ident=None):
        """调一个工具。`ident` 可由协议层传入（避免同一请求解析两次 token）。

        归属提示在**这一层**统一追加：每个工具各自拼提示，迟早会有一个漏掉，
        而"漏掉提示"的表现就是"AI 以为自己已归属"——那正是本轮要根治的东西。
        """
        table = {
            "remember": self.tool_remember,
            "recall": self.tool_recall,
            "neighbors": self.tool_neighbors,
            "resolve_code": self.tool_resolve,
            "read_body": self.tool_read_body,
            "add_project": self.tool_add_project,
            "supersede": self.tool_supersede,
            "whoami": self.tool_whoami,
            "register": self.tool_register,
            "rename_citizen": self.tool_rename_citizen,
            "close_project": self.tool_close_project,
            "reopen_project": self.tool_reopen_project,
            "inject": self.tool_inject,
            "healthcheck": self.tool_healthcheck,
            "session_start": self.tool_session_start,
            "board": self.tool_board,
            # T05（2026-10-26）：知识库读 / 写两条通道分开注册（裁决 C2）。
            "kb": self.tool_kb,
            "kb_write": self.tool_kb_write,
            # T03（2026-10-27）：对话语料只读面（18 → 19 个工具）。
            "corpus": self.tool_corpus,
        }
        if name not in table:
            raise ValueError(f"未知工具：{name}")
        if ident is None:
            ident = self._resolve_identity((args or {}).get(TOKEN_ARG))
        text = table[name](args, ident)
        hint = None if name in HINT_SELF_EXPLAINED else self._identity_hint(ident)
        return text if not hint else text.rstrip("\n") + "\n\n" + hint + "\n"

    def read_resource(self, uri):
        # 资源读取走不了工具入参里的 token（resources/read 的 params 只有 uri/_meta），
        # 所以这里**不解析身份**：按"未归属"渲染，并把这件事明说——不能让 AI
        # 把 fish://me 里的 L0 当成"系统确认了我是谁"。
        ident = IDENTITY_ANONYMOUS
        if uri == "fish://me":
            pack, meta = fish.build_injection_pack(
                self.conn, self.root, ident.citizen_id)
            self._touch(meta.get("recent_codes") or [], ident)
            return (pack + "\n<!-- 资源读取不带会话凭证：本次身份未核；"
                           "带身份请用工具调用并传 token=... -->\n")
        if uri == "fish://index":
            rows = self.conn.execute(
                "SELECT code,title,mem_type FROM code_index ORDER BY code DESC LIMIT 200").fetchall()
            return "# 编码索引\n\n" + "\n".join(
                f"- {c} [{m}] {t}" for c, t, m in rows) + "\n"
        raise KeyError(uri)

    # ------------------------------------------------------------ 分发

    def handle(self, req):
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        meta = params.get("_meta") or {}
        requested = meta.get(META_PREFIX + "protocolVersion")

        # 通知：无响应
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None

        # modern 请求带版本：先校验
        if requested is not None and requested not in SUPPORTED_VERSIONS:
            return self._err(rid, -32022, "Unsupported protocol version",
                             {"supported": SUPPORTED_VERSIONS, "requested": requested})

        modern = requested is not None

        # ---- 规范硬要求：server/discover 必实现 ----
        if method == "server/discover":
            return self._ok(rid, {
                "supportedVersions": SUPPORTED_VERSIONS,
                "capabilities": {"tools": {}, "resources": {}},
                "instructions": "大肥鱼生命系统：记忆编码、身份登记、邻近回忆、"
                                "权威当下注入。用 remember 记，用 recall 找。",
            }, modern=True, ttl_ms=3600000, cache_scope="public")

        # ---- legacy 握手（dual-era 兼容）----
        if method == "initialize":
            pv = params.get("protocolVersion")
            if pv not in SUPPORTED_VERSIONS:
                pv = "2024-11-05"
            return self._ok(rid, {
                "protocolVersion": pv,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": SERVER_INFO,
            }, modern=False)

        # ---- legacy 兼容：ping（2026-07-28 已移除，仅对 legacy 应答）----
        if method == "ping":
            return self._ok(rid, {}, modern=modern)

        if method == "tools/list":
            return self._ok(rid, {"tools": TOOLS}, modern=modern,
                            ttl_ms=300000, cache_scope="public")

        if method == "resources/list":
            return self._ok(rid, {"resources": RESOURCES}, modern=modern,
                            ttl_ms=300000, cache_scope="public")

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            t0 = time.time()
            # 身份解析在协议层做一次，传进 call_tool：审计要记的必须是**这一次**真正
            # 用于写入的身份。让 call_tool 自己再解析一遍，就会出现"审计算出来的是 A、
            # 写入用的是 B"这种最难查的不一致（两次解析之间数据库可能被吊销改掉）。
            # 解析也放在 try 里：查 token 表同样可能失败（库锁/文件损坏），
            # 而"工具错误作为结果返回、不崩服务"这条不能因为多了一步解析就失去。
            ident = IDENTITY_ANONYMOUS
            try:
                ident = self._resolve_identity(args.get(TOKEN_ARG))
                text = self.call_tool(name, args, ident)
                self._audit("tool", name, True, t0, ident)
                return self._ok(rid, {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                }, modern=modern)
            except Exception as e:  # 工具错误作为结果返回，不崩服务
                self._audit("tool", name, False, t0, ident)
                return self._ok(rid, {
                    "content": [{"type": "text", "text": f"错误：{e}"}],
                    "isError": True,
                }, modern=modern)

        if method == "resources/read":
            uri = params.get("uri")
            t0 = time.time()
            # 资源读取没有 token 通道 → 审计如实记 none（不是"某个默认身份"）
            ident = IDENTITY_ANONYMOUS
            try:
                text = self.read_resource(uri)
            except KeyError:
                # 2026-07-28：资源不存在用 -32602（原 -32002）
                self._audit("resource", uri, False, t0, ident)
                return self._err(rid, -32602, f"Resource not found: {uri}")
            except Exception as e:
                self._audit("resource", uri, False, t0, ident)
                return self._err(rid, -32602, str(e))
            self._audit("resource", uri, True, t0, ident)
            return self._ok(rid, {
                "contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}],
            }, modern=modern, ttl_ms=60000, cache_scope="private")

        return self._err(rid, -32601, f"Method not found: {method}")

    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                print(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": f"Parse error: {e}"}}),
                      flush=True)
                continue
            resp = self.handle(req)
            if resp is not None:
                print(json.dumps(resp, ensure_ascii=False), flush=True)


def main():
    ap = argparse.ArgumentParser(prog="fish-mcp")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent / "data"))
    args = ap.parse_args()
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    FishServer(Path(args.root)).run()


if __name__ == "__main__":
    main()
