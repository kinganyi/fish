#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish 宿主接入层（一期：**只读探测**）— p0/fish_hosts.py

## 这个文件解决的问题

「这台机器上装了哪些 Agent 宿主、fish 在哪几个宿主里真的接上了、接的是哪条通道」——这件事
原来散在四个文件里各写一遍（`install_hooks.TARGETS` 两个、`install_mcp` 只认 WorkBuddy、
`fish_naming.SESSION_HOSTS` 五个、`fish_corpus.ADAPTERS` 两个），并且有一个已知的假红：
WorkBuddy 的 `--check` 在 `WORKBUDDY_HOME` 未设置时指向占位符路径，报的是「缺 4 个事件」，
而真实原因是「路径不存在」——**把「判不到」报成了「红」**。

这里把「宿主是什么」收成一张 `HostSpec` 表 + 一个薄探测驱动：

    HOSTS（注册表，唯一来源）
      └─ detect(id)  → HostStatus（现场探测，**不落库、不写盘、不 spawn 宿主**）

## 三层分离（谁该存在哪）

| 层 | 内容 | 存在哪 | 谁改 |
|---|---|---|---|
| **HostSpec** | 宿主是什么、能干什么（配置落点/格式/事件/方言/能力） | **本文件**（程序资产，需 review） | 开发者 |
| **HostStatus** | 探测结果（装没装、配置可读否、fish 接上了哪条通道、最后活跃） | **不落库**，每次现探 | 运行时 |
| **HostConfig** | 用户选择（启用/事件子集/超时/路径覆盖） | 二期才做（`config` 表，键前缀 `host.<id>.*`） | 设置页 |

`HostStatus` 不落库是刻意的：探测结果是「当下」，一旦落库就变成「看起来装了、其实早卸了」。
这条与 fish 既有的「判不到不编值」是同一条纪律。

## 一期**不**做什么（写清楚，免得当成有）

1. **不写任何宿主配置**：本文件没有一处 `open(..., "w")`，不备份、不 apply、不 restore。
   设置页的「启用 hooks / 启用 MCP」是**禁用按钮**，原因写进 `blocked_actions`。
2. **不 spawn 宿主**：版本一律从文件读（读不到就 `None`，不冒充）；不跑 `--version`。
   版本来源有优先级（见 §三·B）：**asar 里的应用 `package.json` > 应用清单 > 文本文件**；
   Electron 安装器的 `version` 文件只进 `electronVersion`，**永远不进 `version`**。
3. **不写库**：只读连接走 `fish_ui.open_readonly` + 一次 `assert_write_rejected`（真试一次写，
   被拒才算过），然后只跑 SELECT。
4. **不做「已写入但与目标不一致」判定**（`configState=stale`）：一期不生成目标配置，没有
   比对基准；`configState` 只有 `ready / missing / unreadable` 三档，另加 `disabledReason` 说明。

## 三套「宿主」词表（不是一套，别合并）

| 词表 | 读者 | 取值来源 | 本文件中的字段 |
|---|---|---|---|
| **protocol**（会话键前缀） | 工作台/看板/撞车判定 | `fish_naming.SESSION_HOSTS`（**唯一真相，这里只读它**） | `HostSpec.protocol` |
| **host id**（安装目标） | 安装器 `--host`、spec 注册表 | 本文件 `HOSTS` | `HostSpec.id` |
| **自报 host** | 公民档案 `citizens.host` | 历史写法五花八门（`ZCode`/`dsh-desktop`/`unknown`） | `canonical_host()` / `host_label()` |

实测（2026-11-23，只读）：`tool_activity.host` 与 `session_key` 前缀一致率极低——那一列是
**账号归属**，不是宿主；前缀才是机器证据。所以设置页必须**同时**显示「协议（前缀）」与
「配置来源（文件）」，不许二选一假装。

## 版本：Electron 版本 ≠ 应用版本（2026-11-24 修的一类真错）

`{install}/version` 这类**安装器文件写的是 Electron 运行时版本**，不是应用版本。实测：

| 宿主 | `version` 文件（Electron） | 应用版本（asar 内 `package.json`） |
|---|---|---|
| DSH | `44.7.0` | `0.2.2-rc.1`（`app.asar` 内 `dsh/package.json`，name=`@deepseek-ai/dsh-desktop-runtime`） |
| WorkBuddy | `37.10.3-43` | `5.11.3`（`resources/app.asar` 顶层 `package.json`，name=`@genie/workbuddy-desktop`） |
| ZCode | （无该文件） | `4.0.0`（`resources/app.asar` 顶层 `package.json`，name=`@zcode/desktop`） |
| Codex CLI | （无） | `0.163.0`（`packages/standalone/current/codex-package.json`） |

把 Electron 版本当宿主版本显示，等于**冒充**：用户会以为 DSH 是 44.7.0。所以
`version` 只收应用版本，Electron 版本单独放 `electronVersion`（UI 明说「非应用版本」）。

## 用

    python -X utf8 p0/fish_hosts.py                    # 打印本机所有宿主的探测读数（JSON）
    python -X utf8 p0/fish_hosts.py --id dsh           # 只探一个
    python -X utf8 p0/fish_hosts.py --db <index.sqlite>  # 连「最后活跃」一起读（只读）
    python -X utf8 p0/tools/verify_hosts.py --api http://127.0.0.1:8790   # 机检

## 边界（如实写）

- 宿主家目录/配置位置是**本机事实**（2026-11-23 实地探测），不是通用约定；换一台机器跑，
  探测结果会变（这正是「现探」的意义）。可覆盖的环境变量见各 spec 的 `home_env`。
- CodeBuddy 的配置目录**探到了家目录**（`~/.codebuddy`，里面只有 logs/diagnostics），但三个
  候选配置文件**都不存在**——`installed=true` + `fishPresence=none`，`configPath=null` 并给原因。
  这就是「空心点」那一档，不许编一个路径假装它有配置。
- Codex 的 `notify = [exe, "turn-ended"]` 只表示「一轮结束」，不含会话标识；fish 在 Codex 上
  只走 MCP 通道（`[mcp_servers.fish]`），所以它的 protocol 是**空**（没有会话键前缀）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fish_naming  # noqa: E402  会话键前缀词表的唯一真相（这里只读，不改）
import fish_ui      # noqa: E402  只读连接纪律：open_readonly / assert_write_rejected

SCHEMA = "fish-hosts/1"

#: 会话键前缀词表的**唯一真相**在 `fish_naming`，本文件只引用、不重打一遍。
PROTOCOLS: tuple[str, ...] = tuple(fish_naming.SESSION_HOSTS)

#: 「没有会话键前缀」的显式取值。**不是** `"unknown"`：`unknown` 是自报面的假宿主，
#: 这里表达的是「这台宿主根本不产生 fish 会话键」（如只有 MCP 通道的 Codex）。
NO_PROTOCOL = ""

#: 最后活跃超过这么久算「停摆」（「正在干活」的时间尺度是分钟，宿主接入是天到月）。
STALL_AFTER = timedelta(hours=24)

CAP_LABELS = {"yes": "已接", "no": "未接", "unverified": "未验证"}

#: 一期唯一允许的动作：重新探测（无副作用）。写类动作全部挡住，原因见 `BLOCKED_REASONS`。
ALLOWED_ACTIONS = ("refresh",)
BLOCKED_ACTIONS = ("toggle-hooks", "toggle-mcp")
BLOCKED_REASONS = {
    "toggle-hooks": "待二期：会写宿主配置",
    "toggle-mcp": "待二期：会写宿主配置",
}

#: 「推送通道」的**源码判据**（任一命中即算这个采集器真的会推送）。
#:
#: **不按文件名判**：WorkBuddy 的 PostToolUse 装的是 `fish_posttool_hook.py`——它的
#: `maybe_push()` 定义在 :313、调用在 :426，走 `select_push_candidates` + `append_push_log`
#: 的同一条推送管线（与 DSH 侧共享核心），文件名里却没有 "push"。旧实现只认
#: `fish_push_hook.py` 这一个名字，于是把 WorkBuddy 的「推送」判成「未接」——一次用户
#: 可见的假否定（docs/105 A2）。
#:
#: 判据落在**脚本源码**上，于是「同一份脚本被两台宿主共用」时结论必然一致：答案只取决于
#: 脚本里有没有推送入口，不取决于宿主 id。
PUSH_ENTRY_MARKERS = ("maybe_push(", "select_push_candidates(", "append_push_log(")


# ══════════════════════════════════════════════════════════════════════════
# 一、spec 的数据结构（20 个字段按「怎么被验证」组织，不是按好看）
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ConfigProbe:
    """一个**配置落点**：这份文件在哪、什么格式、fish 在里面怎么定位。

    `path_template` 用 `{home}` 插值（家目录按环境变量优先、缺省值兜底解析），也可以用
    `%LOCALAPPDATA%` 这类环境变量（Windows）——**只记路径形状，不记这台机器的绝对路径**。
    """

    role: str                       # "hooks" | "mcp"
    label: str                      # 人类面文件名，如 "hooks.json"
    path_template: str              # 含 {home}，如 "{home}/hooks.json"
    format: str                     # "json" | "toml" | "yaml"
    locator: tuple[str, ...] = ()   # json/toml 的键路径，如 ("mcp","servers","fish")
    locator_match: tuple[tuple[str, str], ...] = ()  # yaml: 任一 mapping 命中这组键值
    locator_text: str = ""          # 人类面定位写法，如 "mcp.servers.fish"


@dataclass(frozen=True)
class VersionProbe:
    """一个**版本来源**：从文件读，绝不 spawn 宿主。读不到就是 `None`（不冒充）。

    `path_template` 支持三个占位：`{home}`（家目录）、`{install}`（家目录**真实路径**的上一级，
    DSH 桌面端把 home 放在安装根下，于是 `{install}/app/resources/app.asar` 就是安装产物）、
    以及 `%ENV%` 这类环境变量。

    `kind` 的取值，**优先级是结构性的**（见 `VERSION_KIND_ORDER`，不靠书写顺序）：

    | kind | 读什么 | 是不是应用版本 |
    |---|---|---|
    | `asar` | 应用包 `app.asar` 里的 `package.json`（路径见 `inner_path`） | **是**（应用自己声明的那份） |
    | `json` | 应用清单 JSON 的某个键（如 Codex CLI 的 `codex-package.json`） | **是** |
    | `text` | 普通文本文件（第一行就是版本号） | 看文件是什么，**不许拿 Electron 的 `version` 文件冒充** |
    | `electron` | Electron 安装器写的 `version` 文件 | **不是**：那是 Electron 运行时版本 |

    书写顺序只在**同一档**内起作用（如 ZCode 有两个候选 asar 路径，按书写先后试）。

    `electron` 这一档是 2026-11-24 修的真错：`{install}/app/version` 里写的是
    **Electron 44.7.0**，被当成宿主版本显示在设置页——违反「不冒充」。现在它只进
    `electronVersion`（UI 明说「非应用版本」），**不进 `version`**。
    """

    path_template: str
    kind: str = "text"              # "asar" | "json" | "text" | "electron"
    key: str = ""                   # kind=="json" 时的键名
    inner_path: str = "package.json"  # kind=="asar" 时 asar 内的路径（DSH 是 dsh/package.json）


@dataclass(frozen=True)
class HostSpec:
    """宿主是什么、能干什么。**新增宿主 = 加一个 spec 字面量**（不改渲染/探测代码）。"""

    id: str
    display: str
    protocol: str                              # 会话键前缀（∈ PROTOCOLS，或 NO_PROTOCOL）
    aliases: tuple[str, ...] = ()              # 自报面的历史写法（大小写不敏感归一）
    home_env: str | None = None                # 家目录环境变量**名**（不记值）
    home_default: str | None = None            # 缺省家目录（可含占位符；解析不了算判不到）
    config_paths: tuple[ConfigProbe, ...] = ()
    hooks_supported: bool = False
    events: tuple[str, ...] = ()
    matcher_dialect: Mapping[str, str] | None = None
    collector: tuple[str, ...] = ()
    identity: Mapping[str, str] | None = None  # {"channel":..., "state":..., "evidence":...}
    version_probes: tuple[VersionProbe, ...] = ()
    notes: tuple[str, ...] = ()
    pseudo: bool = False                        # 词表保留值（cli / self），不是安装目标

    # ── 派生（都从上面的字段算，不另存）──────────────────────────────
    @property
    def alias_keys(self) -> tuple[str, ...]:
        return tuple({a.strip().lower() for a in (self.id, *self.aliases) if a.strip()})

    @property
    def hooks_configs(self) -> tuple[ConfigProbe, ...]:
        return tuple(c for c in self.config_paths if c.role == "hooks")

    @property
    def mcp_configs(self) -> tuple[ConfigProbe, ...]:
        return tuple(c for c in self.config_paths if c.role == "mcp")


# ── 注册表（所有宿主 id 的唯一来源；顺序 = 设置页的显示顺序）─────────────────
HOSTS: tuple[HostSpec, ...] = (
    HostSpec(
        id="dsh",
        display="DSH",
        protocol="dsh",
        aliases=("dsh-desktop",),
        home_env="DSH_HOME",
        home_default="~/.dsh",
        config_paths=(
            ConfigProbe(
                role="hooks", label="hooks.json",
                path_template="{home}/hooks.json", format="json",
                locator=("hooks",), locator_text="hooks.<Event>",
            ),
            ConfigProbe(
                role="mcp", label="cordis.patch.yml",
                path_template="{home}/profiles/desktop/cordis.patch.yml", format="yaml",
                locator_match=(("serverName", "fish"),),
                locator_text="serverName: fish（dsh-mcp-client 的 config）",
            ),
        ),
        hooks_supported=True,
        events=("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"),
        matcher_dialect={
            "kind": "literal",
            "note": "纯字面备选（Write|write|…）；DSH 工具名是小写，两种写法都要写",
        },
        collector=("fish_dsh_hook.py", "fish_prompt_hook.py",
                   "fish_pretool_hook.py", "fish_push_hook.py"),
        identity={
            "channel": "hook", "state": "yes",
            "evidence": "SessionStart → fish_dsh_hook.py（按 hook payload 的 session_id 签发 token）",
        },
        version_probes=(
            # 应用版本在 asar 里的应用清单；`{install}/app/version` 是 **Electron 44.7.0**。
            VersionProbe(path_template="{install}/app/resources/app.asar", kind="asar",
                         inner_path="dsh/package.json"),
            VersionProbe(path_template="{install}/app/version", kind="electron"),
        ),
        notes=(
            "hooks 在启动时装配：改完配置必须重启桌面端才生效",
            "MCP 子进程的 DSH_* 环境被 scrubbedParentEnv() 删掉，所以身份只能靠 hook 通道",
        ),
    ),
    HostSpec(
        id="workbuddy",
        display="WorkBuddy",
        protocol="codebuddy",
        aliases=(),
        home_env="WORKBUDDY_HOME",
        home_default="~/.workbuddy",
        config_paths=(
            ConfigProbe(
                role="hooks", label="settings.json",
                path_template="{home}/settings.json", format="json",
                locator=("hooks",), locator_text="hooks.<Event>",
            ),
            ConfigProbe(
                role="mcp", label="mcp.json",
                path_template="{home}/mcp.json", format="json",
                locator=("mcpServers", "fish"), locator_text="mcpServers.fish",
            ),
        ),
        hooks_supported=True,
        events=("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"),
        matcher_dialect={
            "kind": "literal",
            "note": "同 Claude Code 方言；工具名首字母大写（Write），大小写两种写法都在 matcher 里",
        },
        collector=("fish_session_hook.py", "fish_prompt_hook.py",
                   "fish_pretool_hook.py", "fish_push_hook.py"),
        identity={
            "channel": "hook", "state": "yes",
            "evidence": "SessionStart → fish_session_hook.py（按 payload 的 session_id 查已有公民，不签发 token）",
        },
        version_probes=(
            # 应用版本在 asar 顶层 package.json；`version` 文件是 **Electron 37.10.3-43**。
            VersionProbe(path_template="%LOCALAPPDATA%/Programs/WorkBuddy/resources/app.asar",
                         kind="asar", inner_path="package.json"),
            VersionProbe(path_template="%LOCALAPPDATA%/Programs/WorkBuddy/version",
                         kind="electron"),
        ),
        notes=(
            "fish 只查已有公民、不签发 token（签发会轮换吊销 MCP 侧刚签的凭证）",
        ),
    ),
    HostSpec(
        id="zcode",
        display="ZCode",
        protocol=NO_PROTOCOL,
        aliases=("ZCode",),
        home_env="ZCODE_HOME",
        home_default="~/.zcode",
        config_paths=(
            ConfigProbe(
                role="hooks", label="cli/config.json",
                path_template="{home}/cli/config.json", format="json",
                locator=("hooks",), locator_text="hooks.events.<Event>（且须 hooks.enabled=true）",
            ),
            ConfigProbe(
                role="mcp", label="cli/config.json",
                path_template="{home}/cli/config.json", format="json",
                locator=("mcp", "servers", "fish"), locator_text="mcp.servers.fish",
            ),
        ),
        hooks_supported=True,
        events=("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
                "PostToolUse", "PostToolUseFailure", "Stop"),
        matcher_dialect={
            "kind": "regex_case_sensitive",
            "note": "永远编译大小写敏感正则；非法正则**静默永不命中**（不报错）",
        },
        collector=(),
        identity={
            "channel": "template", "state": "unverified",
            "evidence": "官方只写 ${CLAUDE_SESSION_ID} 模板变量 + 环境变量注入；stdin payload 字段未验证",
        },
        version_probes=(
            # 应用版本在 asar 顶层 package.json（name=`@zcode/desktop`）。这里**只写标准安装
            # 位置**：本机 ZCode 装在 `I:\app\ZCode`（非标准），读不到就如实判不到，绝不写死
            # 本机绝对路径，也不拿安装根目录里那个旧安装包名（`ZCode-3.16.3-win-x64.exe`）充数。
            VersionProbe(path_template="%LOCALAPPDATA%/Programs/ZCode/resources/app.asar",
                         kind="asar", inner_path="package.json"),
            VersionProbe(path_template="%PROGRAMFILES%/ZCode/resources/app.asar", kind="asar",
                         inner_path="package.json"),
        ),
        notes=(
            "只有 MCP 通道：本机没有装 fish hooks（配置里连 hooks 键都没有）",
            "配置代码钩子默认是关的：hooks.enabled 不为 true，装上也不会触发",
            "本机配置是 mcp.servers.fish（不是 mcpServers），与多数宿主不同",
        ),
    ),
    HostSpec(
        id="codex",
        display="Codex",
        protocol=NO_PROTOCOL,
        aliases=("codex",),
        home_env="CODEX_HOME",
        home_default="~/.codex",
        config_paths=(
            ConfigProbe(
                role="mcp", label="config.toml",
                path_template="{home}/config.toml", format="toml",
                locator=("mcp_servers", "fish"), locator_text="[mcp_servers.fish]",
            ),
        ),
        hooks_supported=False,
        events=("turn-ended",),
        matcher_dialect={"kind": "unsupported", "note": "无事件表、无 matcher：只有顶层 notify"},
        collector=(),
        identity={
            "channel": "token", "state": "no",
            "evidence": "无 hook、无 session_id；只能靠 MCP 参数 token 与 session_name（凭证通道，不是会话身份）",
        },
        version_probes=(VersionProbe(
            path_template="{home}/packages/standalone/current/codex-package.json",
            kind="json", key="version"),),
        notes=(
            "配置是 TOML（不是 JSON）：条目形状也不同（[mcp_servers.fish] 段）",
            "顶层 notify 只表示一轮结束，不含会话标识",
            "只走 MCP 一条通道（FISH_ACTOR=codex-main 决定谁在读写）",
        ),
    ),
    HostSpec(
        id="codebuddy",
        display="CodeBuddy",
        protocol="codebuddy",
        aliases=(),
        home_env="CODEBUDDY_HOME",
        home_default="~/.codebuddy",
        config_paths=(
            ConfigProbe(
                role="hooks", label="settings.json",
                path_template="{home}/settings.json", format="json",
                locator=("hooks",), locator_text="hooks.<Event>",
            ),
            ConfigProbe(
                role="mcp", label="mcp.json",
                path_template="{home}/mcp.json", format="json",
                locator=("mcpServers", "fish"), locator_text="mcpServers.fish",
            ),
            ConfigProbe(
                role="mcp", label="config.json",
                path_template="{home}/config.json", format="json",
                locator=("mcpServers", "fish"), locator_text="mcpServers.fish",
            ),
        ),
        hooks_supported=True,
        events=("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"),
        matcher_dialect={"kind": "literal", "note": "与 WorkBuddy 同协议（hook 脚本里写死 codebuddy: 前缀）"},
        collector=("fish_session_hook.py",),
        identity={
            "channel": "hook", "state": "unverified",
            "evidence": "与 WorkBuddy 同协议；本机没有配置文件，钩子通道未验证",
        },
        version_probes=(),
        notes=(
            "本机装了（家目录有 logs/diagnostics），但三个候选配置文件都不存在",
            "与 WorkBuddy 同协议、不同产品：两者配置目录不同，装的是哪个要看配置在哪",
        ),
    ),
    HostSpec(
        id="cli",
        display="CLI",
        protocol="cli",
        aliases=("cli",),
        home_env=None,
        home_default=None,
        config_paths=(),
        hooks_supported=False,
        notes=("显式「无宿主」（fish session start 直接开档期）：没有配置文件可探测",),
        pseudo=True,
    ),
    HostSpec(
        id="self",
        display="self",
        protocol="self",
        aliases=("self",),
        home_env=None,
        home_default=None,
        config_paths=(),
        hooks_supported=False,
        notes=("会话键词表里的保留值（self: 前缀）：不是安装目标，没有配置文件可探测",),
        pseudo=True,
    ),
)

#: id → spec（注册一致性在 import 期就查，重复 id / 越界 protocol 直接报错，不进生产）
REGISTRY: dict[str, HostSpec] = {}
_ALIAS_OWNER: dict[str, str] = {}


def _register(spec: HostSpec) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", spec.id):
        raise ValueError(f"宿主 id 不合规：{spec.id!r}（^[a-z][a-z0-9-]{{1,31}}$）")
    if spec.id in REGISTRY:
        raise ValueError(f"宿主 id 重复：{spec.id!r}（注册表里每个 id 只能出现一次）")
    if spec.protocol and spec.protocol not in PROTOCOLS:
        raise ValueError(
            f"{spec.id}: protocol={spec.protocol!r} 不在 fish_naming.SESSION_HOSTS {PROTOCOLS} 里；"
            "新增会话键前缀要改 fish_naming（本文件不重打这份词表）")
    if not spec.display.strip():
        raise ValueError(f"{spec.id}: display 不能为空")
    REGISTRY[spec.id] = spec
    for alias in spec.alias_keys:
        owner = _ALIAS_OWNER.get(alias)
        if owner and owner != spec.id:
            raise ValueError(f"alias {alias!r} 同时属于 {owner!r} 与 {spec.id!r}")
        _ALIAS_OWNER[alias] = spec.id


for _spec in HOSTS:
    _register(_spec)


# ══════════════════════════════════════════════════════════════════════════
# 二、三套词表的归一（机器面 vs 人类面，别混）
# ══════════════════════════════════════════════════════════════════════════
_NON_HOST = {"", "unknown", "n/a", "na", "null", "none", "-"}


def canonical_host(raw: str | None) -> str:
    """**机器面归并键**：alias → 规范 id；自报哨兵 → `""`；认不出 → 原样小写。

    规则（与设计 §3.2 的 R1–R5 一一对应）：
      R1 空/None → `""`（不是 `"unknown"`）
      R2 只做 `strip().lower()`，不做通用字符串变形
      R3 只认**显式** alias 表（`dsh-desktop → dsh`，但 `dsh-anything` 原样返回）
      R4 `unknown`/`n/a`/`null` 等自报哨兵 → `""`（与空同桶）
      R5 `cli` 保留为独立值（语义 = 显式「无宿主」，与空值不同）
    """
    text = (raw or "").strip().lower()
    if text in _NON_HOST:
        return ""
    return _ALIAS_OWNER.get(text, text)


def host_label(raw: str | None) -> str:
    """**人类面标签**：永远保留原始写法（`ZCode` 显示成 `ZCode`），认不出就明说。"""
    text = (raw or "").strip()
    if not text:
        return "未登记宿主（空）"
    key = canonical_host(text)
    spec = REGISTRY.get(key)
    if spec is not None:
        return spec.display
    return f"未登记宿主 {text}"


def hosts_for_protocol(protocol: str) -> tuple[str, ...]:
    """protocol → 可能的 host id 列表。**反查不唯一时不猜**（R6）。

    `codebuddy` 同时对应 WorkBuddy 与 CodeBuddy 两个产品：从前缀只能知道「是这一族的」，
    返回两个而不是折成一个。
    """
    key = (protocol or "").strip().lower()
    return tuple(spec.id for spec in HOSTS if spec.protocol and spec.protocol == key)


def spec_of(target: str | HostSpec) -> HostSpec:
    if isinstance(target, HostSpec):
        return target
    spec = REGISTRY.get((target or "").strip().lower())
    if spec is None:
        raise KeyError(f"未登记的宿主 id：{target!r}（已登记：{', '.join(REGISTRY)}）")
    return spec


def spec_all() -> tuple[HostSpec, ...]:
    """注册表的只读视图（设置页渲染的唯一真相）。"""
    return HOSTS


# ══════════════════════════════════════════════════════════════════════════
# 三、路径解析与文件读取（只读）
# ══════════════════════════════════════════════════════════════════════════
_PLACEHOLDER = re.compile(r"<[^<>]*[^\x00-\x7f][^<>]*>|<\s*用户名\s*>|<\s*user(name)?\s*>")


def resolve_home(spec: HostSpec) -> tuple[str | None, str]:
    """家目录 → `(path|None, reason)`。占位符未解析时**明确判不到**（不许退化成「配置缺失」）。"""
    if spec.home_env:
        raw = (os.environ.get(spec.home_env) or "").strip()
        if raw:
            return os.path.abspath(os.path.expanduser(raw)), f"env {spec.home_env}"
    if not spec.home_default:
        return None, "spec 没有声明家目录"
    if _PLACEHOLDER.search(spec.home_default):
        return None, (f"缺省路径含未解析占位符 {spec.home_default!r}"
                      f"（{spec.home_env or '无环境变量'} 未设置）")
    return os.path.abspath(os.path.expanduser(spec.home_default)), "缺省路径"


def resolve_path(template: str, home: str | None) -> str:
    """把 `{home}` / `{install}` / `%ENV%` 展开成绝对路径（不检查存在性）。

    `{install}` 用 `realpath(home)` 的上一级：本机 `.dsh` 是指向 `I:\\app\\DSH-Home\\home`
    的 junction，所以**必须**先 realpath 再取上一级——直接 `..` 会得到 `C:\\Users\\15596`。
    """
    install = os.path.dirname(os.path.realpath(home)) if home else ""
    text = template.format(home=home or "", install=install)
    return os.path.abspath(os.path.normpath(os.path.expandvars(text)))


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


# ── §三·B asar：应用版本的真来源（Electron 的 `version` 文件不是）────────────
#: asar 头最多读这么多字节：坏文件不许让我们把 GB 级垃圾读进内存。
ASAR_HEADER_CAP = 64 * 1024 * 1024


def _asar_entry(header: Any, inner_path: str) -> Any:
    """按 `a/b/c` 走 asar 头里的文件树 → 叶子节点（不存在就 `None`）。

    叶子节点的形状：`{"offset": <int|str>, "size": <int>}`；目录是 `{"files": {...}}`；
    内容不在 asar 里的叶子带 `"unpacked": true`；指向别处的带 `"link"`。
    """
    node = header
    for part in str(inner_path).replace("\\", "/").split("/"):
        if not part:
            continue
        files = node.get("files") if isinstance(node, Mapping) else None
        if not isinstance(files, Mapping) or part not in files:
            return None
        node = files[part]
    return node


def _asar_data_bases(header_size: int, file_size: int, header: Any) -> list[int]:
    """数据区起点的**候选**（三个来源都从文件自身算出来，一个都不靠猜）。

    为什么要候选：`@electron/asar` 的读法是把数据起点当 `16 + headerSize`，但本机实测
    三份 asar 里有两份在这个位置**差 2 字节**（头部之后有填充）：

    | 宿主 | `16 + headerSize`（官方口径） | 实测数据起点 |
    |---|---|---|
    | DSH | 5747726 | **5747728** |
    | ZCode | 8363914 | **8363916** |
    | WorkBuddy | 3248752 | 3248752（不差） |

    按官方口径读会多出上一段的尾巴（`}\\n`），JSON 必然解析失败。所以这里给候选，
    **只有真解析出 JSON 才算数**（见 `asar_package_version`），读不对就 `None`：

      ① `16 + headerSize`（官方口径，先试）；
      ② `file_size - max(offset + size)`（**从文件末尾反推**：数据区紧挨着排到 EOF）；
      ③ ①②各自之后 1..7 字节的位移（对齐填充的余数）。
    """
    primary = [16 + header_size]
    ends: list[int] = []

    def walk(node: Any) -> None:
        files = node.get("files") if isinstance(node, Mapping) else None
        if not isinstance(files, Mapping):
            return
        for item in files.values():
            if not isinstance(item, Mapping):
                continue
            if isinstance(item.get("files"), Mapping):
                walk(item)
                continue
            try:
                ends.append(int(item["offset"]) + int(item["size"]))
            except (KeyError, TypeError, ValueError):
                continue

    walk(header)
    if ends:
        derived = file_size - max(ends)
        if 16 <= derived < file_size:
            primary.append(derived)
    candidates = list(primary)
    for base in primary:
        candidates.extend(base + shift for shift in range(1, 8))
    return [base for base in dict.fromkeys(candidates) if 16 <= base < file_size]


def asar_package_version(asar_path: str, inner_path: str = "package.json") -> str | None:
    """从 `app.asar` 里读 `package.json` 的 `version` → `str | None`（**只读**）。

    asar 结构（`@electron/asar`：pickle 头 + JSON 头 + 数据区）：

        偏移 0   : 16 字节前缀，`headerSize = readUInt32LE(12)`
        偏移 16  : `headerSize` 字节 JSON 文件树 `{files: {<name>: {...}}}`
        数据起点 : `16 + headerSize`（本机实测可能再有 ≤7 字节填充，见 `_asar_data_bases`）

    纪律：**读不出就 `None`**。哪怕只是偏移差两字节也返回 `None`——宁可判不到，
    也不猜一个版本号出来（那正是「Electron 版本冒充应用版本」那类错的根源）。
    任何异常（文件不存在/被占用/头坏了/条目是目录、link 或 unpacked）都收敛成 `None`。
    """
    try:
        with open(asar_path, "rb") as handle:
            prefix = handle.read(16)
            if len(prefix) < 16:
                return None
            header_size = struct.unpack("<I", prefix[12:16])[0]
            file_size = os.fstat(handle.fileno()).st_size
            if not 0 < header_size <= min(file_size - 16, ASAR_HEADER_CAP):
                return None
            header = json.loads(handle.read(header_size).decode("utf-8"))
            entry = _asar_entry(header, inner_path)
            if not isinstance(entry, Mapping):
                return None
            if entry.get("unpacked") or "link" in entry:
                return None          # 内容不在 asar 里（或是个链接）：这里判不到
            offset, size = int(entry["offset"]), int(entry["size"])  # offset 可能是字符串
            if offset < 0 or size <= 0:
                return None
            for base in _asar_data_bases(header_size, file_size, header):
                handle.seek(base + offset)
                payload = handle.read(size)
                if len(payload) != size:
                    continue
                try:
                    document = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                value = document.get("version") if isinstance(document, Mapping) else None
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None
    except Exception:  # noqa: BLE001  口径写死：任何异常都返回 None（不猜）
        return None


# ── §三·C YAML：容忍宿主自定义标签（`!!js` 不是坏文件）─────────────────────
_YAML_MODULE: Any = None
_YAML_LOADER: Any = None
_YAML_LOADER_ERROR: str | None = None


def yaml_loader() -> tuple[Any, Any, str | None]:
    """→ `(yaml 模块, Loader 类, 不可用原因)`。构造一次、缓存；**不改 PyYAML 的全局 SafeLoader**。

    为什么需要：`profiles/desktop/cordis.patch.yml` 里 `Authorization: !!js "..."` 是
    **DSH 自己的表达式标签**（宿主 loader 会把它当 JS 求值），PyYAML 不认 →
    `safe_load` 抛 `ConstructorError`，整份配置被判「判不到」。

    容忍的**边界**（写清楚，免得被当成「忽略文件内容」）：
      · 未知标签的值**原样保留**：标量取原始文本，序列/映射按结构构造（不丢字段、不求值）；
      · 已知标签（`!!str` / `!!int` …）仍走 `SafeLoader` 原来的构造器，语义不变；
      · 结构真坏了（缩进错 / 语法错 / 未闭合的引号）照旧抛错 → 调用方仍判「判不到」。
    """
    global _YAML_MODULE, _YAML_LOADER, _YAML_LOADER_ERROR
    if _YAML_LOADER is not None or _YAML_LOADER_ERROR is not None:
        return _YAML_MODULE, _YAML_LOADER, _YAML_LOADER_ERROR
    try:
        import yaml  # noqa: PLC0415  可选依赖：没有它也能跑（退化成文本命中）
    except ImportError as exc:
        _YAML_LOADER_ERROR = f"yaml 模块不可用：{exc}"
        return None, None, _YAML_LOADER_ERROR

    class TolerantSafeLoader(yaml.SafeLoader):
        """`SafeLoader` + 一个「未知标签原样保留」的多构造器（`add_multi_constructor('')`）。"""

    def _keep_raw(loader: Any, tag_suffix: str, node: Any) -> Any:
        """未知标签（`!!js` / `!secret` / `!!js/...`）→ 原样内容，**不求值、不丢弃**。"""
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        value = getattr(node, "value", None)
        return value if isinstance(value, str) else None

    TolerantSafeLoader.add_multi_constructor("", _keep_raw)
    _YAML_MODULE, _YAML_LOADER = yaml, TolerantSafeLoader
    return _YAML_MODULE, _YAML_LOADER, None


#: YAML **值位置**上的自定义标签（`!!js` / `!secret` …）。只用来写说明文案，不参与判定。
_YAML_CUSTOM_TAG = re.compile(r"(?::|^\s*-)\s+(!!?[A-Za-z_][\w./-]*)", re.M)


def _yaml_custom_tags(text: str) -> list[str]:
    """配置文本里出现的自定义标签（去重保序）——只用来在证据里说明「容忍了什么」。"""
    out: list[str] = []
    for match in _YAML_CUSTOM_TAG.finditer(text):
        tag = match.group(1)
        if tag not in out:
            out.append(tag)
    return out


def _parse_document(text: str, fmt: str) -> tuple[Any, str | None, bool]:
    """解析配置文本 → `(obj|None, error|None, soft)`。

    `soft=True` 表示「结构没解析成，但可以退化成**文本命中**」——这是「判不到」，不是「坏文件」。
    真实触发点（2026-11-23 / 11-24 实测）：
      · 机器上没装 pyyaml（可选依赖）；
      · YAML 结构真坏了（缩进错、语法错、引号未闭合）。
    这两种都不该把整份配置判成 unreadable——那样 DSH 的 MCP 通道会被误报成「没接」。

    注意：YAML 里出现 `!!js` 这类**宿主自定义标签已不再是** soft 的触发点——见
    `yaml_loader()`：未知标签原样保留成字符串，结构照样真解析（2026-11-24 修）。
    """
    if fmt == "json":
        try:
            return json.loads(text), None, False
        except ValueError as exc:  # JSONDecodeError 是 ValueError 的子类
            return None, f"json 解析失败：{type(exc).__name__}: {str(exc).splitlines()[0][:200]}", False
    if fmt == "toml":
        try:
            import tomllib
            return tomllib.loads(text), None, False
        except ValueError as exc:  # TOMLDecodeError 也是 ValueError
            return None, f"toml 解析失败：{type(exc).__name__}: {str(exc).splitlines()[0][:200]}", False
    if fmt == "yaml":
        yaml_module, loader, loader_error = yaml_loader()
        if loader is None:
            return None, f"{loader_error}：未做结构解析", True
        try:
            return yaml_module.load(text, Loader=loader), None, False
        except Exception as exc:  # noqa: BLE001  解析器的错误面比 YAMLError 宽（含编码/递归）
            head = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            return None, f"yaml 结构未解析：{type(exc).__name__}: {head}", True
    return None, f"未支持的格式 {fmt!r}", False


def _dig(doc: Any, path: Sequence[str]) -> Any:
    node = doc
    for key in path:
        if isinstance(node, Mapping):
            if key not in node:
                return None
            node = node[key]
        elif isinstance(node, list) and key.isdigit():
            index = int(key)
            if index >= len(node):
                return None
            node = node[index]
        else:
            return None
    return node


def _yaml_has_mapping(node: Any, match: Sequence[tuple[str, str]]) -> bool:
    if isinstance(node, Mapping):
        if match and all(str(node.get(key)) == value for key, value in match):
            return True
        return any(_yaml_has_mapping(value, match) for value in node.values())
    if isinstance(node, (list, tuple)):
        return any(_yaml_has_mapping(value, match) for value in node)
    return False


# ── hooks 文档 → command 串 → fish 脚本路径 ────────────────────────────────
_SCRIPT_RE = re.compile(r"[A-Za-z]:[\\/][^\"'\s]+?\.py|[^\"'\s]+?\.py")


def _event_map(doc: Any) -> Mapping[str, Any]:
    """hooks 文档里的「事件表」→ `{Event: [groups]}`。

    认两种形态（`install_hooks` B15 的读侧对应物）：
      · `{"hooks": {"SessionStart": [...]}}`（DSH/WorkBuddy 现状）
      · 裸事件表 `{"SessionStart": [...]}`（历史形态）
    """
    if not isinstance(doc, Mapping):
        return {}
    nested = doc.get("hooks")
    if isinstance(nested, Mapping):
        return nested
    if doc and all(isinstance(value, list) for value in doc.values()):
        return doc
    return {}


def hook_commands(doc: Any) -> list[str]:
    """所有 hook 的 `command` 串（不含 matcher：matcher 不是证据，脚本才是）。"""
    out: list[str] = []
    for groups in _event_map(doc).values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            for hook in group.get("hooks") or []:
                if isinstance(hook, Mapping) and isinstance(hook.get("command"), str):
                    out.append(hook["command"])
    return out


def script_paths(commands: Iterable[str]) -> list[str]:
    """从 command 串里抠出 `.py` 路径（去重、保序）。"""
    seen: list[str] = []
    for command in commands:
        for match in _SCRIPT_RE.findall(command):
            path = match.strip()
            if path and path not in seen:
                seen.append(path)
    return seen


# ══════════════════════════════════════════════════════════════════════════
# 四、单个配置落点的探测（纯读）
# ══════════════════════════════════════════════════════════════════════════
def inspect_config(probe: ConfigProbe, home: str | None) -> dict:
    """一份配置文件的现场读数（存在/可解析/fish 命中/脚本存在）。"""
    path = resolve_path(probe.path_template, home)
    info: dict[str, Any] = {
        "role": probe.role,
        "label": probe.label,
        "path": path,
        "format": probe.format,
        "locator": probe.locator_text or ".".join(probe.locator),
        "exists": os.path.isfile(path),
        "size": None,
        "mtime": None,
        "parsed": None,
        "parseError": None,
        "softParse": False,
        "fishHit": False,
        "detail": "",
        "scripts": [],
        "missingScripts": [],
        "toleratedTags": [],
    }
    if not info["exists"]:
        info["detail"] = "文件不存在"
        return info
    try:
        stat = os.stat(path)
        info["size"] = stat.st_size
        info["mtime"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except OSError as exc:
        info["detail"] = f"读元数据失败：{type(exc).__name__}: {exc}"
        return info

    text = _read_text(path)
    if text is None:
        info["detail"] = "读不出内容（权限或编码问题）"
        return info
    doc, error, soft = _parse_document(text, probe.format)
    if error and soft:
        info["softParse"] = True
        # 「判不到」不折成「坏文件」：按文本命中给一个下限结论，并明说不做结构解析。
        if probe.locator_match:
            hit = any(f"{key}: {value}" in text for key, value in probe.locator_match)
        else:
            hit = bool(probe.locator and f'"{probe.locator[-1]}"' in text)
        info["fishHit"] = bool(hit)
        info["parseError"] = error
        info["detail"] = f"{error}；文本命中 {probe.locator_text}：{'有' if hit else '无'}"
        return info
    if error:
        info["parseError"] = error
        info["detail"] = error
        return info
    info["parsed"] = True
    if probe.format == "yaml":
        tags = _yaml_custom_tags(text)
        if tags:
            info["toleratedTags"] = tags

    if probe.role == "hooks":
        commands = hook_commands(doc)
        fish_commands = [cmd for cmd in commands if "fish" in cmd.lower()]
        scripts = script_paths(fish_commands)
        info["scripts"] = scripts
        info["missingScripts"] = [path for path in scripts if not os.path.isfile(path)]
        info["fishHit"] = bool(fish_commands)
        info["detail"] = (f"{len(_event_map(doc))} 个事件 · {len(commands)} 条 command · "
                          f"{len(fish_commands)} 条指向 fish")
    else:
        if probe.locator_match:
            hit = _yaml_has_mapping(doc, probe.locator_match)
        else:
            hit = _dig(doc, probe.locator) is not None
        info["fishHit"] = hit
        info["detail"] = f"{probe.locator_text}：{'命中' if hit else '未命中'}"
    return info


#: 版本来源的**结构性**优先级：asar（安装包里的应用清单）> json（应用清单文件）> text（普通文本）
#: > electron（Electron 运行时版本）。写成常量而不是「靠 spec 的书写顺序」，是为了让
#: 「Electron 不许冒充应用版本」这条不依赖某个人的记性：哪怕 spec 把 electron 写在第一位，
#: 它也排到最后。同一档内保留书写顺序（如 ZCode 有两个候选 asar 路径）。
VERSION_KIND_ORDER = {"asar": 0, "json": 1, "text": 2, "electron": 3}


def _ordered_probes(spec: HostSpec) -> list[VersionProbe]:
    """`spec.version_probes` 按 `VERSION_KIND_ORDER` 稳定排序 → 实际尝试顺序。"""
    return sorted(spec.version_probes, key=lambda probe: VERSION_KIND_ORDER.get(probe.kind, 9))


@dataclass(frozen=True)
class VersionReading:
    """一次版本探测的**结果**（三态分明：应用版本 / 只有 Electron 版本 / 判不到）。

    `version` 只装**应用版本**；`electron_version` 装 Electron 运行时版本——它**不是**宿主
    版本，单独成字段是为了「可查但不冒充」。
    """

    version: str | None = None
    source: str | None = None
    note: str | None = None
    kind: str | None = None             # "asar" | "json" | "text" | "electron"
    electron_version: str | None = None
    electron_source: str | None = None


def _electron_version(spec: HostSpec, home: str | None) -> tuple[str | None, str | None]:
    """读 Electron 运行时版本 → `(version|None, source|None)`。

    它**不是**宿主版本，但值得读出来（两个小文件、几乎零成本）：设置页可以明确写
    「Electron 44.7.0（非应用版本）」——把「我们知道这个数、但它不代表宿主」说清楚，
    比不显示更能防下次误读。
    """
    for probe in _ordered_probes(spec):
        if probe.kind != "electron":
            continue
        path = resolve_path(probe.path_template, home)
        text = _read_text(path)
        value = text.strip().splitlines()[0].strip() if (text and text.strip()) else ""
        if value:
            return value, path
    return None, None


def read_version(spec: HostSpec, home: str | None) -> VersionReading:
    """版本 → `VersionReading`。**读不到就判不到，不冒充**。

    优先级是**结构性的**（`VERSION_KIND_ORDER`）：`asar` > `json` > `text` > `electron`。
    spec 的书写顺序只在同一档内起作用。三段含义：
      · 命中 `asar` / `json` / `text` → **应用版本**，进 `version`；
      · 只命中 `electron` → `version=None` + `electron_version` 有值 + `note` 明说「非应用版本」；
      · 都没有 → `version=None` + `note` 说清是「没声明来源」还是「都读不出」（带具体原因）。
    `electron_version` **总是**单独读一遍（不影响 `version` 的判定），只为「可查但不冒充」。
    """
    electron_version, electron_source = _electron_version(spec, home)
    problems: list[str] = []

    for probe in _ordered_probes(spec):
        if probe.kind == "electron":
            continue                     # 已单独读过，且它永远不会进 `version`
        path = resolve_path(probe.path_template, home)
        if probe.kind == "asar":
            if not os.path.isfile(path):
                problems.append(f"{path} 不存在")
                continue
            value = asar_package_version(path, probe.inner_path)
            if value:
                return VersionReading(value, f"{path}（asar 内 {probe.inner_path}）", None,
                                      "asar", electron_version, electron_source)
            problems.append(f"{path} 读不出 {probe.inner_path} 的 version")
            continue
        text = _read_text(path)
        if text is None:
            problems.append(f"{path} 不存在或读不出")
            continue
        if probe.kind == "json":
            try:
                payload = json.loads(text)
            except ValueError:
                problems.append(f"{path} 不是合法 JSON")
                continue
            value = _dig(payload, (probe.key,)) if probe.key else payload
            if isinstance(value, str) and value.strip():
                return VersionReading(value.strip(), path, None, "json",
                                      electron_version, electron_source)
            problems.append(f"{path} 里没有可用的 {probe.key or 'version'} 字段")
            continue
        value = text.strip().splitlines()[0].strip() if text.strip() else ""
        if not value:
            problems.append(f"{path} 是空文件")
            continue
        return VersionReading(value, path, None, "text", electron_version, electron_source)

    if electron_version:
        return VersionReading(
            None, None,
            f"只读到 Electron 运行时版本 {electron_version}（{electron_source}）——"
            "那不是应用版本，不当作宿主版本显示",
            "electron", electron_version, electron_source)
    if not spec.version_probes:
        return VersionReading(note="spec 没有声明可读的版本来源（不 spawn 宿主去问版本）")
    note = "声明的版本来源都读不出"
    if problems:
        note += "：" + "；".join(problems[:3])
    return VersionReading(note=note)


# ══════════════════════════════════════════════════════════════════════════
# 五、库里的「最后活跃」（只读；一次连接、一次写拒绝证明）
# ══════════════════════════════════════════════════════════════════════════
_READONLY_PROVEN = False


def open_readonly_db(db_path: str) -> sqlite3.Connection:
    """只读连接。**第一次**开连接时真试一次写（被拒才算过），之后复用结论。"""
    global _READONLY_PROVEN
    conn = fish_ui.open_readonly(db_path)
    if not _READONLY_PROVEN:
        fish_ui.assert_write_rejected(conn)   # 被拒绝是期望路径；能写会抛错并大声失败
        _READONLY_PROVEN = True
    return conn


def _object_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone()
    return row is not None


def last_seen(conn: sqlite3.Connection | None, spec: HostSpec,
              buckets: dict | None = None) -> tuple[str | None, list[dict]]:
    """该宿主的最后活跃时间 → `(max_at|None, sources[])`。

    口径（**不把三套词表混成一个数**）：
      · `tool_activity.host` 是**账号归属**（workbuddy 那 2.6 万行的前缀其实是 codebuddy）；
      · `session_key` 前缀是机器证据，但**只在前缀唯一对应一个宿主时**才用来归属（R6）；
      · `citizens_all.host` 是自报值（`ZCode`/`dsh-desktop` 这类历史写法靠 alias 归一）。

    `buckets` 由 `activity_buckets()` **一次扫描**算好（7 个宿主各扫一遍 2.6 万行要 1.2 s，
    一次分桶只要 ~0.2 s）；不传就现场算一次（单宿主场景够用）。
    """
    if conn is None:
        return None, []
    table = buckets if buckets is not None else activity_buckets(conn)
    sources: list[dict] = []

    def add(source: str, item: dict | None) -> None:
        if item and item.get("at"):
            sources.append({"source": source, "at": item["at"], "rows": item["rows"]})

    for alias in spec.alias_keys:
        add("tool_activity(host 列)", table["byHost"].get(alias))
    if spec.protocol and len(hosts_for_protocol(spec.protocol)) == 1:
        add(f"tool_activity(前缀 {spec.protocol}:)", table["byPrefix"].get(spec.protocol))
        add(f"presence(前缀 {spec.protocol}:)", table["byPresencePrefix"].get(spec.protocol))
    for alias in spec.alias_keys:
        add("citizens_all(自报 host)", table["byCitizenHost"].get(alias))

    if not sources:
        return None, []
    sources.sort(key=lambda item: item["at"], reverse=True)
    return sources[0]["at"], sources


_PREFIX_SQL = ("CASE WHEN instr(coalesce({col}, ''), ':') > 1 "
               "THEN lower(substr({col}, 1, instr({col}, ':') - 1)) ELSE '' END")


def activity_buckets(conn: sqlite3.Connection | None) -> dict:
    """一次扫描把「每个宿主能用的活跃证据」分好桶（**只读**）。

    四张桶：`byHost`（tool_activity.host 列）/ `byPrefix`（tool_activity.session_key 前缀）/
    `byPresencePrefix`（presence.session_key 前缀）/ `byCitizenHost`（citizens_all.host 自报）。
    缺表不是错误：对应桶为空，由 `detect` 的 `stalledNote` 说明「库里没有记录」。
    """
    empty: dict[str, dict] = {"byHost": {}, "byPrefix": {}, "byPresencePrefix": {},
                              "byCitizenHost": {}}
    if conn is None:
        return empty
    out: dict[str, dict] = {key: {} for key in empty}

    def fill(bucket: str, sql: str) -> None:
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            key = row[0]
            if key is None or key == "":
                continue
            out[bucket][str(key).strip().lower()] = {
                "at": row[1], "rows": int(row[2] or 0)}

    if _object_exists(conn, "tool_activity"):
        fill("byHost", "SELECT lower(host), MAX(seen_at), COUNT(*) FROM tool_activity "
                       "GROUP BY lower(host)")
        fill("byPrefix", "SELECT " + _PREFIX_SQL.format(col="session_key") + " AS p, "
                         "MAX(seen_at), COUNT(*) FROM tool_activity GROUP BY p")
    if _object_exists(conn, "presence"):
        fill("byPresencePrefix",
             "SELECT " + _PREFIX_SQL.format(col="session_key") + " AS p, "
             "MAX(last_seen_at), COUNT(*) FROM presence GROUP BY p")
    if _object_exists(conn, "citizens_all"):
        fill("byCitizenHost", "SELECT lower(host), MAX(last_seen_at), COUNT(*) "
                              "FROM citizens_all GROUP BY lower(host)")
    return out


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════════
# 六、探测驱动：spec → HostStatus
# ══════════════════════════════════════════════════════════════════════════
def _cap(state: str, evidence: str) -> dict:
    return {"state": state, "label": CAP_LABELS[state], "evidence": evidence}


def detect(target: str | HostSpec, *, db_path: str | None = None,
           conn: sqlite3.Connection | None = None, buckets: dict | None = None,
           now: datetime | None = None) -> dict:
    """探一个宿主 → `HostStatus`（plain dict，可直接 JSON）。

    `target` 收 `str` 也收 `HostSpec`：收 spec 是给**变异自证**用的测试缝——把
    `config_paths` 指向不存在的路径，`installed` 必须变 false，否则判据没有判别力。

    传 `db_path` 才查「最后活跃」；不传则 `lastSeenAt=null` 并明说「未提供库路径」——
    **判不到不是 0**，更不是「没在用」。批量探测请用 `detect_all()`（它只开一次连接、
    只扫一遍活跃桶）。"""
    spec = spec_of(target)
    moment = now or datetime.now(timezone.utc)
    home, home_reason = resolve_home(spec)
    home_exists = bool(home and os.path.isdir(home))

    evidence: list[dict] = []
    configs: list[dict] = []
    for probe in spec.config_paths:
        info = inspect_config(probe, home)
        configs.append(info)
        evidence.extend(_config_evidence(info))
    evidence.insert(0, {
        "id": "home",
        "label": "家目录",
        "ok": home_exists if home else None,
        "detail": (f"{home}（{home_reason}）" if home else home_reason),
    })

    reading = read_version(spec, home)
    version, version_source, version_note = reading.version, reading.source, reading.note
    evidence.append({
        "id": "version",
        "label": "版本可判（应用版本）",
        # 三态分明：拿到应用版本 = true；只有 Electron 版本 / 什么都读不到 = null（判不到）。
        "ok": True if version else None,
        "detail": (version_source if version else version_note),
    })

    existing = [info for info in configs if info["exists"]]
    primary = existing[0] if existing else (configs[0] if configs else None)
    config_path = primary["path"] if (primary and primary["exists"]) else None
    if primary is None:
        config_state = "missing"
        config_reason = "该宿主没有声明任何配置落点（不是安装目标）"
    elif not primary["exists"]:
        config_state = "missing"
        config_reason = f"配置不存在：{primary['path']}"
    elif primary["parseError"] and not primary["softParse"]:
        config_state = "unreadable"
        config_reason = primary["parseError"]
    else:
        config_state = "ready"
        config_reason = None

    hooks_hit_configs = [info for info in configs if info["role"] == "hooks" and info["fishHit"]]
    mcp_hit_configs = [info for info in configs if info["role"] == "mcp" and info["fishHit"]]
    fish_presence = ("both" if hooks_hit_configs and mcp_hit_configs
                     else "hooks" if hooks_hit_configs
                     else "mcp" if mcp_hit_configs
                     else "none")

    hooks_commands = [cmd for info in configs if info["role"] == "hooks"
                      for cmd in (hook_commands(_parse_config(info)) if info["exists"] else [])]
    push_hit = any(PUSH_HOOK_SCRIPT in cmd for cmd in hooks_commands)

    installed = bool(home_exists or existing or version or reading.electron_source)

    capabilities = _capabilities(spec, hooks_hit=bool(hooks_hit_configs),
                                 mcp_hit=bool(mcp_hit_configs), push_hit=push_hit,
                                 config_exists=bool(existing))

    evidence.append({
        "id": "fish-presence",
        "label": "fish 接入通道",
        "ok": fish_presence != "none",
        "detail": {
            "both": "hooks + MCP 两条通道都在配置里",
            "hooks": "只有 hooks（事件流）",
            "mcp": "只有 MCP（工具通道）",
            "none": "两条通道都没在配置里找到 fish",
        }[fish_presence],
    })

    last_at, last_sources = _last_seen_resolved(spec, db_path=db_path, conn=conn,
                                                buckets=buckets)
    stalled: bool | None = None
    stalled_note: str | None = None
    if last_at is None:
        stalled = None
        if conn is None:
            stalled_note = "未提供库路径：这次只做了文件探测，没查「最后活跃」"
        else:
            stalled_note = "库里没有该宿主的行为记录（判不到，不等于停摆）"
    else:
        moment_at = _parse_ts(last_at)
        if moment_at is None:
            stalled = None
            stalled_note = f"库里的时间戳读不出来：{last_at!r}"
        else:
            stalled = (moment - moment_at) > STALL_AFTER
            stalled_note = (f"最近一条行为 {last_at}"
                            if not stalled else f"最近一条行为 {last_at}，已超过 24 小时")

    notes = list(spec.notes)
    if spec.protocol == NO_PROTOCOL:
        notes.append("无会话键前缀：这台宿主不产生 fish 会话键（身份另走凭证通道）")
    elif len(hosts_for_protocol(spec.protocol)) > 1:
        # R6：协议层不区分产品 → 只报「还可能是谁」，绝不断言「就是它」。
        others = [REGISTRY[host].display for host in hosts_for_protocol(spec.protocol)
                  if host != spec.id]
        notes.append(f"前缀 {spec.protocol}: 同族还有 {'/'.join(others)}（协议层不区分产品）")
    if config_path is None and spec.config_paths:
        notes.append(f"没有可读的配置文件：{config_reason}")
    if not spec.config_paths:
        notes.append("没有配置文件可探测（不是安装目标）")

    return {
        "id": spec.id,
        "display": spec.display,
        "protocol": spec.protocol,
        "protocolNote": _protocol_note(spec),
        "aliases": list(spec.aliases),
        "pseudo": spec.pseudo,
        "installed": installed,
        "home": home,
        "homeExists": home_exists,
        "configPath": config_path,
        "configExists": bool(config_path),
        "configFormat": primary["format"] if primary else None,
        "configState": config_state,
        "configReason": config_reason,
        "fishPresence": fish_presence,
        "hooksSupported": spec.hooks_supported,
        "events": list(spec.events),
        "matcherDialect": dict(spec.matcher_dialect) if spec.matcher_dialect else None,
        "collector": list(spec.collector),
        "capabilities": capabilities,
        "configs": [_public_config(info) for info in configs],
        "evidence": evidence,
        "version": version,
        "versionKind": reading.kind,
        "versionSource": version_source,
        "versionNote": version_note,
        "electronVersion": reading.electron_version,
        "electronSource": reading.electron_source,
        "lastSeenAt": last_at,
        "lastSeenSources": last_sources,
        "stalled": stalled,
        "stalledNote": stalled_note,
        "allowedActions": list(ALLOWED_ACTIONS),
        "blockedActions": list(BLOCKED_ACTIONS),
        "blockedReasons": dict(BLOCKED_REASONS),
        "notes": notes,
    }


def _parse_config(info: dict) -> Any:
    """把 `inspect_config` 读过的文件再解析一次（只在算 push 通道时用，量很小）。"""
    text = _read_text(info["path"]) if info["exists"] else None
    if text is None:
        return None
    doc, _error, _soft = _parse_document(text, info["format"])
    return doc


def _public_config(info: dict) -> dict:
    """给响应用的配置读数（不含脚本绝对路径全集之外的内部字段）。"""
    return {
        "role": info["role"],
        "label": info["label"],
        "path": info["path"],
        "format": info["format"],
        "locator": info["locator"],
        "exists": info["exists"],
        "size": info["size"],
        "mtime": info["mtime"],
        "parsed": bool(info["parsed"]),
        "parseError": info["parseError"],
        "parseSoft": info["softParse"],
        "fishHit": info["fishHit"],
        "detail": info["detail"],
        "scripts": info["scripts"],
        "missingScripts": info["missingScripts"],
        # 解析时容忍过的宿主自定义标签（如 `!!js`）：值原样保留、未求值，只用于说明。
        "toleratedTags": list(info.get("toleratedTags") or []),
    }


def _config_evidence(info: dict) -> list[dict]:
    """一份配置 → 逐条证据（存在 / 可解析 / fish 命中 / 脚本存在）。"""
    label = f"{info['label']}"
    items = [{
        "id": f"config:{info['role']}:{label}",
        "label": f"{label} 存在",
        "ok": info["exists"],
        "detail": (f"{info['path']}（{info['size']} B，mtime {info['mtime']}）"
                   if info["exists"] else f"{info['path']} 不存在"),
    }]
    if info["exists"]:
        items.append({
            "id": f"parse:{info['role']}:{label}",
            "label": f"{label} 可解析",
            # `softParse` = 结构没解析成但可退化文本命中（缺 pyyaml / YAML 结构真坏）：
            # 那是「判不到」，不是「不可解析」——ok 必须是 null，不能报成 false（三态分明）。
            "ok": None if info["softParse"] else bool(info["parsed"]),
            "detail": info["parseError"] or (
                f"{info['format']} 解析成功"
                + (f"（容忍宿主自定义标签 {'、'.join(info['toleratedTags'])}：值原样保留，未求值）"
                   if info.get("toleratedTags") else "")
            ),
        })
        items.append({
            "id": f"fish:{info['role']}:{label}",
            "label": f"{label} 里能定位到 fish",
            "ok": bool(info["fishHit"]),
            "detail": info["detail"],
        })
        if info["role"] == "hooks" and info["scripts"]:
            ok = not info["missingScripts"]
            items.append({
                "id": f"scripts:{info['role']}:{label}",
                "label": "fish 脚本存在",
                "ok": ok,
                "detail": (f"{len(info['scripts'])} 个脚本都在" if ok
                           else f"缺脚本：{'、'.join(info['missingScripts'])}"),
            })
    return items


def _protocol_note(spec: HostSpec) -> str | None:
    if spec.protocol == NO_PROTOCOL:
        return "未登记（这台宿主没有会话键前缀）"
    owners = hosts_for_protocol(spec.protocol)
    if len(owners) > 1:
        return f"{spec.protocol}: 同族（{len(owners)} 个产品共用）"
    return None


def _capabilities(spec: HostSpec, *, hooks_hit: bool, mcp_hit: bool,
                  push_hit: bool, config_exists: bool) -> dict:
    """能力矩阵（hooks / MCP / events / identity / push）。

    纪律：`state=="yes"` 必须给得出 `evidence`（**不许无证据声称支持**）；
    判不到的写 `unverified`（渲染成「未验证」），不许折成「未接」。
    """
    hooks_evidence = (
        f"hooks 配置里 {len(spec.events)} 个事件、command 指向 fish 脚本（见 evidence 逐条）"
        if hooks_hit else
        ("有 hooks 配置文件，但里面没有 fish 脚本" if config_exists and spec.hooks_configs
         else "没有可读的 hooks 配置（fish 未安装 hooks）")
    )
    mcp_evidence = ("MCP 配置里定位到 fish 条目" if mcp_hit
                    else "MCP 配置里没有 fish 条目（或没有 MCP 配置）")
    identity = dict(spec.identity or {"channel": "none", "state": "unverified",
                                      "evidence": "spec 未声明身份通道"})
    if identity.get("channel") == "hook":
        identity_state = "yes" if hooks_hit else ("unverified" if not config_exists else "no")
    else:
        identity_state = identity.get("state", "unverified")
    return {
        "hooks": _cap("yes" if hooks_hit else "no", hooks_evidence),
        "mcp": _cap("yes" if mcp_hit else "no", mcp_evidence),
        "events": _cap("yes" if hooks_hit else "no",
                       "事件流来自 hook：hooks 不在就没有事件上报" if not hooks_hit
                       else f"{len(spec.events)} 个 hook 事件 → 事件流"),
        "identity": _cap(identity_state, identity.get("evidence", "")),
        "push": _cap("yes" if push_hit else "no",
                     f"PostToolUse → {PUSH_HOOK_SCRIPT}（推送通道）" if push_hit
                     else "没有装推送采集器（fish_push_hook.py）"),
    }


def _last_seen_resolved(spec: HostSpec, *, db_path: str | None,
                        conn: sqlite3.Connection | None,
                        buckets: dict | None) -> tuple[str | None, list[dict]]:
    """拿到 conn（或按 db_path 临时开一个）→ 查最后活跃。单次探测不会漏关连接。"""
    if conn is not None:
        return last_seen(conn, spec, buckets)
    if not (db_path and os.path.isfile(db_path)):
        return None, []
    try:
        owned = open_readonly_db(db_path)
    except sqlite3.Error:
        return None, []
    try:
        return last_seen(owned, spec, activity_buckets(owned))
    finally:
        owned.close()


def detect_all(*, db_path: str | None = None, now: datetime | None = None) -> list[dict]:
    """按 `HOSTS` 顺序探所有宿主（共用一个只读连接、只扫一遍活跃桶）。"""
    moment = now or datetime.now(timezone.utc)
    conn: sqlite3.Connection | None = None
    if db_path and os.path.isfile(db_path):
        try:
            conn = open_readonly_db(db_path)
        except sqlite3.Error:
            conn = None
    try:
        buckets = activity_buckets(conn)
        return [detect(spec, conn=conn, buckets=buckets, now=moment) for spec in HOSTS]
    finally:
        if conn is not None:
            conn.close()


def installed_statuses(*, db_path: str | None = None,
                       now: datetime | None = None) -> list[dict]:
    """只留**探测到安装痕迹**的宿主（`GET /api/hosts` 的列表口径）。"""
    return [item for item in detect_all(db_path=db_path, now=now) if item["installed"]]


# ══════════════════════════════════════════════════════════════════════════
# 七、与另两份词表的一致性（**不改那两个文件**；不成立只报一条 warning，不阻断）
# ══════════════════════════════════════════════════════════════════════════
_ADAPTER_CACHE: tuple[str, ...] | None = None
_ADAPTER_ERROR: str | None = None


def corpus_adapter_hosts() -> tuple[tuple[str, ...] | None, str | None]:
    """语料层已登记的宿主 id（延迟 import，缓存一次）。

    为什么延迟：`fish_corpus` 会拖进 zstd/解析链，而 hooks 安装与探测都**不该**依赖它
    （设计 §2.3 C 档）。这里只在「一致性自查」时读一次它的 `ADAPTERS` 键，读到就缓存。
    """
    global _ADAPTER_CACHE, _ADAPTER_ERROR
    if _ADAPTER_CACHE is not None or _ADAPTER_ERROR is not None:
        return _ADAPTER_CACHE, _ADAPTER_ERROR
    try:
        import fish_corpus  # noqa: PLC0415  延迟 import（见上）
    except Exception as exc:  # noqa: BLE001  导入失败本身就是一条「判不到」
        _ADAPTER_ERROR = f"{type(exc).__name__}: {exc}"
        return None, _ADAPTER_ERROR
    _ADAPTER_CACHE = tuple(str(key).strip().lower() for key in fish_corpus.ADAPTERS)
    return _ADAPTER_CACHE, None


def consistency() -> dict:
    """三套词表的一致性自查 → `{ok, warnings[], checked{}}`。

    两条断言（**只报不拦**）：
      ① `set(SESSION_HOSTS) ⊆ set(HOSTS ids)`——会话键词表里的每个前缀都要有宿主登记，
         否则「库里出现了一个没人认识的宿主」；
      ② `set(ADAPTERS) ⊆ set(HOSTS ids)`——语料层能采的宿主必须是接入层认识的目标。
    """
    warnings: list[str] = []
    ids = [spec.id for spec in HOSTS]
    session_hosts = set(fish_naming.SESSION_HOSTS)
    missing = sorted(session_hosts - set(ids))
    if missing:
        warnings.append(
            f"fish_naming.SESSION_HOSTS 有 {missing} 未在 fish_hosts.HOSTS 登记"
            "（会话键会出现「没人认识的宿主」）")
    adapters, adapter_error = corpus_adapter_hosts()
    if adapters is None:
        warnings.append(f"判不到：fish_corpus 导入失败（{adapter_error}），无法核对 ADAPTERS ⊆ HOSTS")
        gap: list[str] = []
    else:
        gap = sorted(set(adapters) - set(ids))
        if gap:
            warnings.append(
                f"fish_corpus.ADAPTERS 有 {gap} 未在 fish_hosts.HOSTS 登记"
                "（语料层能采、接入层不认识）")
    return {
        "ok": not warnings,
        "warnings": warnings,
        "checked": {
            "sessionHosts": sorted(session_hosts),
            "adapters": sorted(adapters) if adapters is not None else None,
            "hosts": ids,
        },
    }


# ══════════════════════════════════════════════════════════════════════════
# 八、自用 CLI（只读；给人工核对与脚本对拍）
# ══════════════════════════════════════════════════════════════════════════
def _fingerprint(path: str) -> dict | None:
    """给「探测不写盘」这条判据用的指纹（mtime 纳秒 + 字节数 + sha256）。"""
    try:
        stat = os.stat(path)
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None
    return {"path": path, "mtimeNs": stat.st_mtime_ns, "size": stat.st_size, "sha256": digest}


def config_fingerprints() -> dict[str, dict]:
    """所有宿主**已存在**配置文件的指纹（`POST refresh` 前后必须逐字节相同）。"""
    out: dict[str, dict] = {}
    for spec in HOSTS:
        home, _reason = resolve_home(spec)
        for probe in spec.config_paths:
            path = resolve_path(probe.path_template, home)
            item = _fingerprint(path)
            if item:
                out[path] = item
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fish_hosts.py",
        description="fish 宿主接入层（只读探测）：装没装 / 接没接 / 接的哪条通道",
    )
    parser.add_argument("--id", default=None, help="只探一个宿主（缺省全部）")
    parser.add_argument("--db", default=None, help="主库路径（给了才查「最后活跃」，只读）")
    parser.add_argument("--installed", action="store_true", help="只打印探测到安装痕迹的宿主")
    parser.add_argument("--fingerprints", action="store_true", help="只打印配置文件指纹")
    parser.add_argument("--json", action="store_true", help="整份 JSON 输出（缺省摘要一行）")
    args = parser.parse_args(argv)

    if args.fingerprints:
        print(json.dumps(config_fingerprints(), ensure_ascii=False, indent=2))
        return 0

    if args.id:
        items = [detect(args.id, db_path=args.db)]
    elif args.installed:
        items = installed_statuses(db_path=args.db)
    else:
        items = detect_all(db_path=args.db)

    if args.json:
        print(json.dumps({"schema": SCHEMA, "items": items,
                          "consistency": consistency()}, ensure_ascii=False, indent=2))
        return 0

    print(f"# {SCHEMA} · 探测时刻 {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    for item in items:
        presence = {"both": "hooks+MCP", "hooks": "hooks", "mcp": "仅 MCP",
                    "none": "未接入"}[item["fishPresence"]]
        print(f"{item['id']:<10} {item['display']:<10} installed={str(item['installed']):<5} "
              f"presence={presence:<9} protocol={item['protocol'] or '(无)':<9} "
              f"config={item['configPath'] or '(无)'}")
        if item["version"]:
            print(f"{'':<10} 版本 {item['version']}（{item['versionSource']}）")
        elif item["electronVersion"]:
            print(f"{'':<10} 版本 判不到应用版本；Electron {item['electronVersion']}"
                  f"（{item['electronSource']}，**不是**应用版本）")
        elif item["versionNote"] and not item["pseudo"]:
            print(f"{'':<10} 版本 判不到（{item['versionNote']}）")
        if item["lastSeenAt"]:
            print(f"{'':<10} 最后活跃 {item['lastSeenAt']}"
                  f"（{item['lastSeenSources'][0]['source']}）")
    report = consistency()
    print(f"# 一致性 {report['ok'] and 'ok' or 'warn'}：{report['warnings'] or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
