#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享原语（`fish.py` 拆包第一轮 · T02 · 设计 §1.2）。

为什么单独成模块
----------------
这几个小工具被**两个**子系统要用：`fish.py`（记忆 / 看板 / 门面）与
`fish_kb.py`（知识库）。它们原来是 `fish.py` 里的函数，`fish_kb` 要用就得
`import fish` —— 而 `fish` 又 `import fish_kb`，那是循环导入。所以先下沉成
"零依赖"的一层：

    fish.py ─┐
             ├─→ fish_common
    fish_kb ─┘

依赖方向是单向的：**这里不许 import fish 或 fish_kb**（判据 J2 静态 + 运行时双查）。
`count_tokens` 里的 tiktoken 是**函数内懒加载**，所以本模块顶层只有 stdlib。

搬了什么（整段搬运，一个字符都没改）
------------------------------------
`ulid`（编码里的时间前缀）、`now_utc`（库里的 UTC 时间戳）、`iso_local`（输出层
本地时区）、`count_tokens` 与它的 `_ENC` 缓存、`_exc_text`（异常一行摘要）；
2026-10-27 起再加切词三函数 `index_tokens` / `fts_text` / `fts_match_expr`
（语料层要按同一套切法建 `corpus_fts`，见下方"中文切词"一节）。
搬走后 `fish.<name>` 仍然可用，且与 `fish_common.<name>` 是**同一个对象**
（`from fish_common import …` 再导出，不是复制实现）。
"""
import datetime
import re
import uuid

def ulid() -> str:
    ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    return f"{ts:013d}{uuid.uuid4().hex[:13].upper()}"

def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def iso_local(dt=None) -> str:
    """本地时区的 ISO8601（`2026-10-26T02:05:11+08:00`）：人读友好、带时区、可回读。

    为什么不直接用 `now_utc()`：数据库里的时间戳一律是 UTC + `Z`（事实层），
    而 as-of 是**输出层**的东西——门面是给人看的，"几点几分"必须是他自己的时区。
    `fromisoformat` 能原样读回带偏移的字符串，所以这个格式不会丢信息。

    为什么无时区的入参按本地时区补：`--as-of 2026-10-26T00:00:00` 是人手写的最常见形态，
    把它当 UTC 会让"今天"整体偏 8 小时（索引月份、最近 30 条都会跟着错）。
    """
    if dt is None:
        dt = datetime.datetime.now().astimezone()
    elif dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")

_ENC = None

def count_tokens(text: str) -> int:
    """优先用 tiktoken 精确计数；不可用时按字符估算（中文≈1.2 token/字）。"""
    global _ENC
    if _ENC is None:
        try:
            import tiktoken
            _ENC = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENC = False
    if _ENC is False:
        return int(len(text) * 1.2)
    return len(_ENC.encode(text))

def _exc_text(exc) -> str:
    return f"{type(exc).__name__}: {exc}"

# ------------------------------------------------------------------ 中文切词（ADR-005）

# 为什么切词也下沉（2026-10-27 · 语料层 T01 · 设计 D09）
# ------------------------------------------------------
# 语料层要自建 FTS（`corpus_fts`），而"索引侧怎么切"与"查询侧怎么切"**必须是同一套**：
# 两套切法的典型症状是"存进去搜不到"——单测各自都绿，因为谁也不查对方建的索引。
# 这次下沉是把唯一一份实现放到两个子系统都能 import 的最低层：
#
#     fish.py ─┐                    ┌─→ memory_fts（记忆）
#              ├─→ fish_common ─────┤
#     fish.py ←┘                    └─→ corpus_fts（语料）
#
# 搬运动作与原拆包第一轮同法：**整段搬运**，`fish.index_tokens` 等旧名字仍然可用，
# 且与这里**是同一个对象**（`from … import` 再导出，不是复制实现）。
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]*")


def index_tokens(text: str) -> list:
    """把文本切成可入 FTS 的 token（ADR-005 的切分规则）。

    中文连续段 → **重叠 bigram**（两字滑窗）。为什么必须自己切：SQLite 内置的
    unicode61 不做中文分词，未切分时「协作约定」是一个 token，查「协作」永远 0 条
    （2026-10-22 实测，见 ADR-005 背景表第 2 条）。
    拉丁 / 数字 / 单号（含 - _ .）→ 原样小写，交给 FTS 再切。
    """
    text = text or ""
    out = [m.group(0).lower() for m in _WORD.finditer(text)]
    for m in _CJK.finditer(text):
        s = m.group(0)
        if len(s) == 1:
            out.append(s)
        else:
            out.extend(s[i:i + 2] for i in range(len(s) - 1))
    return out


def fts_text(text: str) -> str:
    return " ".join(index_tokens(text))


def fts_match_expr(query: str):
    """查询串 → FTS5 MATCH 表达式；没有可查 token 时返回 None。

    **每个 token 必须加双引号**：不加的话 `CR-12` 会被 FTS5 解析成「CR 列包含 12」，
    直接抛 sqlite3.OperationalError: no such column: 12（2026-10-22 实测，ADR-005 §背景 1）。
    """
    toks = [t for t in index_tokens(query) if t]
    if not toks:
        return None
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in toks)
