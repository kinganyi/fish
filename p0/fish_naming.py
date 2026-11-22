#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · 可读名（纯函数，无 DB）。

## 为什么要有它（用户 2026-10-26 拍板）

> 看板显示可读 AI 名（如 `anyi-studio-主AI`）+ 项目名（`scope_key`），**不要 ULID**。

在此之前 `who_label()` 返回的是 `名字（短 id）`、没有名字时**只给短 id**。
用户看到的就是一屏 `17929164`——认不出是谁，而看板的全部动作都是
「找到那个人去协调」。所以这是一次**有意的契约变更**：

  · 人类可读面（看板渲染 / 注入包 / MCP `board show` / 撞车原因）→ **永不含 26 位公民 ID**；
  · 机器视图（`--json`、表、审计 JSONL）→ 保留 ID（它就是主键）。

## 三条规则

1. 有名字 → 原样（去空白）；
2. 没名字 → `未命名·<host>`（host 也空 → `未命名`）。**绝不返回 citizen_id 片段**：
   那是机器码，用户点名不要它；而且短 id 在不同库里还会撞前缀。
3. 重名 → `名字·<host>`；host 再重 → `名字·<host>#2/#3`（按 created_at 稳定排序）。
   **永不用 ULID/短 ID 消歧**——两个 `anyi-studio-主AI` 看着一样，说明该改名，
   而不是该把机器码塞回人眼前。

自动名是**缺名兜底**，不是可读名；本模块提供 `is_auto_name` 让调用方能识别它
（当前会话会收到一次「去起名」的可执行提示）。

## 自动名改中文，并让生成器与检测器同源（2026-11-22）

**实测事故**：旧格式是 `<host>-<会话码前 12 位>`，于是生成了 `dsh-session-88f9`
这种名字；而 `AUTO_NAME_RE` 写的是 `[0-9a-z]{6,12}`（不含 `-`），**匹配不上**。
结果：库里 173 个由生成器造出来的名字，**`is_auto_name` 一个都没认出来（100% 漏判）**。
连带后果是「去起名」提示**从来没触发过** —— 机制、接线都在，只有检测器是坏的。

两处修法：

1. **格式改成中文 + 显式前缀**：`未命名·<host>#<短码>`（如 `未命名·dsh#88f9`）。
   它是**占位名**，就该长得像占位名 —— 一眼看出"这还不是真名字"，同时保留
   host 与短码用于追溯。
2. **生成器下沉到本模块**：`auto_session_display_name` 原本在 `fish.py`，与检测器
   分居两地，正是漂移的温床。现在两个函数相邻，`is_auto_name` 可以直接调用生成器
   做**精确比对**，不再靠字符串猜形状。

`is_auto_name` 保留旧格式正则，是为了让**老库里的历史名字**仍被认出来。
"""
from __future__ import annotations

import re

# **旧格式**（`dsh-8a83d218` 这类纯字母数字尾巴）。只为兼容老库里的历史名字保留；
# 新生成的自动名不再走它，见下方 `auto_session_display_name`。
AUTO_NAME_RE = re.compile(r"^(dsh|codebuddy|workbuddy|cli)-[0-9a-z]{6,12}(:.*)?$", re.I)

UNNAMED_LABEL = "未命名"
#: 自动占位名的前缀。它同时是**判据**：`is_auto_name` 认前缀即认自动名，
#: 不依赖任何"猜形状"的正则 —— 这样生成器与检测器永远不会再漂。
UNNAMED_PREFIX = "未命名·"


def auto_session_display_name(host, session_key, role=None) -> str:
    """没给别名时的**自动占位名**：`未命名·<host>#<短码>`（子 agent 带 `:<role>`）。

    ## 为什么是中文 + 显式前缀（2026-11-22）

    旧实现在 `fish.py`，产 `<host>-<会话码前 12 位>`（`dsh-session-88f9`）。
    那是**英文形状的占位名**：既不像真名字，也不会被 `is_auto_name` 认出来
    （173 个名字 100% 漏判，连带「去起名」提示从未触发）。

    新格式做三件事：
      · **中文**（用户要求：自动名不该是英文）；
      · **显式前缀 `未命名·`**，一眼看出"这还不是名字"，也让人一眼知道该改名；
      · **保留 host 与短码**（`dsh#88f9`），追溯仍然靠 `host_session_key` 主键。

    没有会话键时退回 `未命名·<host>`；同一宿主多会话仍会重名，由 `disambiguate()`
    加 `#2/#3` 区分（**绝不用 ULID**，那条铁律不变）。
    """
    short = session_short(session_key) or (host or "cli")
    base = f"{UNNAMED_PREFIX}{short}"
    return f"{base}:{role}" if role else base


def is_auto_name(name, *, host=None, session_key=None) -> bool:
    """这个名字是不是「自动占位名」，而不是人起的名字。

    三层判定，**从最可靠到最宽松**：

      ① **前缀**：`未命名·` 开头 → 必然是自动名（新格式，判据稳定）；
      ② **旧格式精确比对**：给了 `host` + `session_key` 时，按旧格式的形状
         （`<host>-<会话码前 12 位>`）算一遍去比。**新格式不需要这一层**
         —— 前缀已经判掉了（这一段最早也写成了"新格式也比"，变异测试证明
         它不可达，已删）；
      ③ **旧格式正则**：兜底老库里那些形如 `dsh-8a83d218` 的历史名字
         （它们没有对应的会话键可比，只能按形状认）。

    为什么需要 ②：旧格式 `dsh-session-88f9` 的尾巴里有 `-`，旧正则
    `[0-9a-z]{6,12}` 匹配不上。只靠正则就是那 100% 漏判。
    """
    text = (name or "").strip()
    if not text:
        return False
    head = text.split(":", 1)[0]
    if head.startswith(UNNAMED_PREFIX):
        return True
    if host and session_key:
        # **只有旧格式需要精确比对**。新格式一条 `未命名·` 前缀就判掉了；
        # 这里原本还写了一段"拿新格式比对"，变异测试 M2 证明它**不可达**
        # （前缀分支先返回）——删掉，免得留一段没法被任何判据触发的死代码。
        tail = (session_key or "").split(":", 1)[-1].split("#", 1)[0]
        if tail and head == f"{host}-{tail[:12]}":
            return True
    return bool(AUTO_NAME_RE.match(text))


def rename_guidance() -> str:
    """「去起名」的引导文案 —— **唯一一份**。

    ## 为什么要单独抽出来

    它要在**两个地方**出现：看板注入包（`_unnamed_hint`）与 `session start` 回执。
    复制两份的话，改一处忘一处就变成两个口径 —— 而这类"提示文案漂移"最难被
    判据抓住（两处各自都对，只是说得不一样）。

    ## 文案为什么这么写（用户 2026-11-22 要求）

    「简单诚恳地强调这个的意义和价值，让 AI 有收获感」。所以除了"怎么做"，
    还写了"改了对你有什么好处"：认不出你是谁，要协调你的人就得先翻会话；
    有名字之后，找你的成本从「翻」变成「看一眼」。
    """
    return "\n".join([
        "**如果你的用户没给你起名，就自己起一个**：结合你大概在做的项目，",
        "起一个简短的、能代表你工作的**中文名**（例如 `CZHY-开发`、`漫剧-剪辑`、",
        "`fish-主AI`）。`rename_citizen(name=\"...\")` 改一次，看板与注入包的",
        "所有行都会用它。",
        "**为什么值得改**：别人在看板上看到 `未命名·dsh#88f9` 时认不出你是谁，",
        "要找你协调就得先翻会话；有了名字，找你的成本从「翻」变成「看一眼」。",
    ])


def base_label(display_name, host, citizen_id) -> str:
    """**不消歧**的基础标签：有名字用名字，没名字用 `未命名·<host>`。

    `citizen_id` 参数**刻意保留但从不使用**：它是旧契约的痕迹，签名留着是为了
    让"这个函数永远不吐 ID"这件事在调用处一眼可见（也免得有人加个 `or citizen_id`）。
    """
    _ = citizen_id                        # 见 docstring：绝不参与取名
    name = (display_name or "").strip()
    if name:
        return name
    h = (host or "").strip()
    return f"{UNNAMED_LABEL}·{h}" if h else UNNAMED_LABEL


def _field(row, key, default=""):
    """从 dict / sqlite3.Row / 普通序列外的映射里取一个字段（缺列返回 default）。"""
    try:
        if isinstance(row, dict):
            value = row.get(key, default)
        else:
            try:
                value = row[key]              # sqlite3.Row
            except (IndexError, KeyError):
                value = getattr(row, key, default)
    except (TypeError, AttributeError):
        return default
    return default if value is None else value


def disambiguate(rows) -> dict:
    """按 `citizen_id` 返回 label；重名规则见模块 docstring。

    `rows` 每项需含 `citizen_id`，可选 `display_name` / `host` / `created_at`
    （缺的按空处理：缺 created_at 时按 citizen_id 稳定排序，仍然确定性）。
    """
    buckets = {}
    for row in rows or ():
        cid = str(_field(row, "citizen_id", "") or "").strip()
        if not cid:
            continue
        name = str(_field(row, "display_name", "") or "").strip()
        host = str(_field(row, "host", "") or "").strip()
        created = str(_field(row, "created_at", "") or "")
        label = base_label(name, host, cid)
        buckets.setdefault(label, []).append((created, cid, host))

    out = {}
    for label, items in buckets.items():
        if len(items) == 1:
            out[items[0][1]] = label
            continue
        by_host = {}
        for created, cid, host in items:
            by_host.setdefault(host, []).append((created, cid))
        for host, entries in by_host.items():
            entries.sort()
            suffix_host = f"·{host}" if host else ""
            for index, (_created, cid) in enumerate(entries, 1):
                out[cid] = f"{label}{suffix_host}" + (f"#{index}" if index > 1 else "")
    return out


# ---------------------------------------------------------------- 会话维度（2026-10-26）
#
# 撞车「自己撞自己」修复（设计 45 §4.1/§4.2）的**唯一**会话短标识实现。
#
# 为什么放在这里而不是 `fish.py`：hook 侧（`fish_activity` / `fish_pretool_hook`）与
# 主程序（`fish`）都要算同一个短标识，而 hook 不许 import fish（400ms 红线）。
# 本模块是纯函数、无 DB、import ~1ms，两个消费者共用一份 → 「看板消歧了、注入包没消歧」
# 这类漂移从结构上消失。
#
# 与「无 ULID 契约」的关系：会话键里的 UUID 是**会话 id**（不是 26 位公民 ULID），
# 而且只取前若干位——人类面永远看不到原始会话键。

# 已知宿主前缀白名单：只有这些前缀才算「合法会话键」。
# 与 `fish_activity.HOST_KEY_PREFIX` / 迁移回填表达式 `_PRESENCE_SESSION_BACKFILL`
# 是同一份口径（判据 S15 钉住回填，判据 S19 钉住渲染）。
SESSION_HOSTS = ("dsh", "codebuddy", "workbuddy", "cli", "self")

SESSION_SHORT_WIDTH = 4          # 缺省宽度（设计 D4：4 位）
SESSION_SHORT_MAX = 8            # 同公民内撞车时逐级加长的上限（**永不回落 ULID**）
SESSION_LABEL_SEP = "·"          # 名字与会话短标识之间的分隔（与重名消歧同一个字符）
NO_SESSION_SHORT = "无会话"       # `session_key=''` 那一行的后缀（明确说"没有会话归属"）

_SESSION_PREFIX_RE = re.compile(r"^session-", re.I)


def split_session_key(session_key):
    """`<host>:<id>` → `(host, id)`；不是合法会话键 → `("", "")`。

    合法 = 恰好一个 `:`、host 在白名单里、id 非空，且**不含** `/`。
    为什么排除 `codebuddy-主AI/ffd54d84`（带 `/` 无 `:`）：它不是会话键形态，
    猜它是会话等于伪造证据——迁移回填（`_PRESENCE_SESSION_BACKFILL`）也用同一条口径。
    """
    text = str(session_key or "").strip()
    if not text or "/" in text:
        return "", ""
    host, sep, ident = text.partition(":")
    if not sep:
        return "", ""
    host = host.strip().lower()
    ident = ident.strip()
    if host not in SESSION_HOSTS or not ident:
        return "", ""
    return host, ident


def is_session_key(value) -> bool:
    """这个值看起来是不是一条**合法会话键**（回填/兜底双读共用同一判据）。"""
    return bool(split_session_key(value)[0])


def session_short(session_key, *, width: int = SESSION_SHORT_WIDTH) -> str:
    """会话短标识：`dsh:session-88f9b6a5-…` → `dsh#88f9`；非法/空 → `''`（不加后缀）。

    三条规则（设计 §4.1 的表）：

      1. host = `:` 之前（小写）；id = `:` 之后，先去掉 `session-` 前缀；
      2. **ASCII id** 取前 `width` 个 `[0-9a-z]`（丢掉 `-`、统一小写）；
      3. **非 ASCII id**（`self:企业微信-主AI`）按**字符**取前 `width` 个——
         切字节会出现半截乱码（R8 的现场）。

    `width` 由 `session_labels` 在"同公民内撞车"时逐级加长用（4 → 6 → 8）。
    """
    host, ident = split_session_key(session_key)
    if not host:
        return ""
    ident = _SESSION_PREFIX_RE.sub("", ident)
    if not ident:
        return ""
    try:
        width = max(1, int(width))
    except (TypeError, ValueError):
        width = SESSION_SHORT_WIDTH
    if ident.isascii():
        body = "".join(ch for ch in ident.lower() if ch.isalnum())[:width]
    else:
        body = ident[:width]
    return f"{host}#{body}" if body else ""


def _short_widths():
    """撞车时的加长阶梯：4 → 6 → 8（`SESSION_SHORT_MAX` 是上限，**绝不回落 ULID**）。"""
    widths = [SESSION_SHORT_WIDTH]
    for candidate in (6, SESSION_SHORT_MAX):
        if candidate > widths[-1]:
            widths.append(candidate)
    return tuple(widths)


def _shorts_for(pairs):
    """同一批 `(citizen_id, session_key)` → `{(cid, sk): short}`，同公民内撞车就加长。

    为什么只在**同一个公民**内判重：不同公民的同名会话短标识不影响可读性
    （它们的名字前缀不同），而把跨公民的比较算进来会让宽度无谓变长。
    """
    pairs = list(pairs or ())
    shorts = {}
    for width in _short_widths():
        shorts = {(cid, sk): session_short(sk, width=width) for cid, sk in pairs}
        buckets = {}
        for (cid, sk), one in shorts.items():
            if not sk:
                continue                    # 无会话归属的行不参与撞车判断
            buckets.setdefault(cid, []).append(one)
        if all(len(set(item)) == len(item) for item in buckets.values()):
            return shorts
    return shorts                           # 8 位仍撞：保持 8 位，绝不拿机器码消歧


def session_labels(rows) -> dict:
    """`{(citizen_id, session_key): label}` —— 会话维度**唯一的取可读名入口**。

    契约（设计 §4.2，判据 S19 钉住）：

      · 该公民在这批行里只有 **1 个会话** → 沿用 `disambiguate()` 的公民名
        （与今天**逐字节相同**，既有字节比对判据因此不会红）；
      · 该公民有 **≥2 个会话** → 名字 + `·` + `session_short`
        （`session_key=''` 的那行写成 `·无会话`——它确实"没有会话归属"，不是某个会话）；
      · 会话短标识同公民内撞车 → 逐级加长到 6/8 位；
      · **永不**使用公民 ULID / 短 id 消歧（沿用 `fish_naming` 既有铁律）。

    `rows` 每项需含 `citizen_id`，可选 `session_key` / `display_name` / `host` /
    `created_at`（缺的按空处理——既有判据会 monkeypatch 造没有新字段的 dict，见 F3）。
    """
    prepared = []
    for row in rows or ():
        cid = str(_field(row, "citizen_id", "") or "").strip()
        if not cid:
            continue
        prepared.append((cid,
                         str(_field(row, "session_key", "") or "").strip(),
                         str(_field(row, "display_name", "") or "").strip(),
                         str(_field(row, "host", "") or "").strip(),
                         str(_field(row, "created_at", "") or "")))
    if not prepared:
        return {}
    # 2026-10-26 QA F1：**先按公民去重**再消歧。
    # 同公民多会话时 prepared 每会话一条，直接喂 disambiguate 会被它当成
    # "同名 N 个公民"，给每一行都加上 #2/#3（假后缀——同板同公民 ≥2 行正是
    # 本轮主场景，所以 42 条新判据全绿却漏掉了它）。
    # 公民名只取决于公民，会话维度由下面的 `·session_short` 表达——两层不许混。
    seen_cids = set()
    unique_rows = []
    for cid, _sk, name, host, created in prepared:
        if cid in seen_cids:
            continue
        seen_cids.add(cid)
        unique_rows.append({"citizen_id": cid, "display_name": name, "host": host,
                            "created_at": created})
    base = disambiguate(unique_rows)
    sessions = {}
    for cid, sk, _name, _host, _created in prepared:
        sessions.setdefault(cid, set()).add(sk)
    shorts = _shorts_for([(cid, sk) for cid, keys in sessions.items() for sk in keys])
    out = {}
    for cid, sk, name, host, _created in prepared:
        label = base.get(cid) or base_label(name, host, cid)
        if len(sessions.get(cid) or ()) >= 2:
            short = shorts.get((cid, sk)) or NO_SESSION_SHORT
            # QA R2-C1：`disambiguate` 可能已经给同名公民加过 `·<host>` 后缀
            # （`AI-甲·dsh`），再拼会话短标识会变成 `AI-甲·dsh·dsh#aaaa`（双层 host）。
            # 若 label 以 `·<会话 host>` 结尾，就把那个 host 吸收进会话短标识。
            sk_host = split_session_key(sk)[0]
            dup = f"{SESSION_LABEL_SEP}{sk_host}" if sk_host else ""
            if dup and label.endswith(dup):
                label = f"{label[:-len(dup)]}{SESSION_LABEL_SEP}{short}"
            else:
                label = f"{label}{SESSION_LABEL_SEP}{short}"
        out[(cid, sk)] = label
    return out


def project_label(scope_key, namespace=None) -> str:
    """项目名的展示写法：`scope_key`（fish 的项目键）优先；namespace 仅当**不同**时附括号。

    为什么不一律用 namespace：`code_registry.namespace` 是描述名、`scope_key` 是项目键，
    两者可能不同（如 `scope_key=大肥鱼生命系统` / `namespace=anyi-studio`）。
    每行印两个名字是纯噪声（看板容量纪律），所以只在根板/注入头这种"汇总处"附一次。

    空 `scope_key` → `（根）`（根看板自己就是这么叫的）。
    """
    key = (scope_key or "").strip()
    if not key:
        return "（根）"
    ns = (namespace or "").strip()
    if ns and ns.lower() != key.lower():
        return f"{key}（{ns}）"
    return key
