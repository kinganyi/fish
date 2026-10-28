#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语料脱敏规则的**唯一一份**（原始对话语料层 · T01 · 设计 §8）。

为什么必须只有一份（PRD C-P0-7 / 设计 §8）
----------------------------------------
脱敏有三道：①落盘前、②查询出口二次、③全库体检。三道各写一份规则的历史结局是
"渲染拦得住、体检看不见"的假绿——生产上真踩过（`fish.py:296-297` 的既有纪律就是
因为这件事立的）。所以这里把规则做成常量 `RULES`，三道全部 import 它。

**不许比 `fish.CREDENTIAL_PATTERNS` 更窄**：那三条是已经生效的既有口径
（`fsh_…` / `token=` / 私钥头，`fish.py:298-302`）。语料侧的判据是"每条都同样命中"，
所以本模块的规则表是它的**超集**，`test_corpus_contract` 逐条比对。

脱敏 ≠ 丢弃（C-P0-7 ③）
----------------------
命中就替换成 `〔已脱敏:<类型>〕` 并计数，**绝不整段删成空串**：段数骤降是比漏脱敏
更糟的故障——它让"这段对话存在过"这件事从库里消失，且没有任何告警。占位符本身
不含任何规则可匹配的形态，所以二次脱敏是幂等的（出口那一道不会重复计数）。

关掉脱敏的开关（C-P0-7 红①）
--------------------------
`FISH_CORPUS_NO_REDACT=1` 时 `redact()` 原样返回。它**只用于造红**（证明体检判据
能失败），出厂默认关闭；体检在开关打开时必须报红，否则那条判据是装饰品。
"""
from __future__ import annotations

import os
import re

ENV_DISABLE = "FISH_CORPUS_NO_REDACT"
PLACEHOLDER = "〔已脱敏:%s〕"

# (类型名, 正则)。**顺序有意义**：具体形态（`fsh_…`）先于泛化形态（`token=`），
# 否则 `token = fsh_xxx` 会先被泛化规则吃掉、具体类型就永远统计不到。
# 类型名刻意不含 `=` / `:` / 20 连长串，保证替换后的占位符不会被任何规则再次命中。
#
# 2026-10-27 三关复审（S1）：旧规则"只掩标签、留下值"——`MY_TOKEN=abc123` 只把
# `token=` 换成占位符，**值还在**；`sk-proj-`/`github_pat_`/JSON `"password":` 整条
# 漏；`GITHUB_TOKEN=` 因大小写敏感漏。所以规则改成"**键+值整体消费**"：
# 命中就把键和值一起换成占位符，值不可能留在库里。族前缀同步放宽。
#
# 代价是**宁可多掩**：`max_token=…` 这类也会被掩（`[a-z0-9_]*` 只匹配 token 之前的
# 字符，所以 `max_tokens=` 反而不命中）。.token 族键名无法穷举，多掩一个词的代价
# 远小于漏一个真凭证。
RULES = (
    ("fish_token", re.compile(r"fsh_[A-Za-z0-9_-]{20,}")),
    # `sk-` 与 `sk-proj-` 同族（后者是 OpenAI 项目级 key 的现行前缀）。
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    # GitHub 五种短前缀 + 细粒度 PAT（`github_pat_<id>_<secret>`）。
    ("github_token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("aws_access_key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    # 外部审查 🔴8（2026-10-29）：旧规则只匹配 `BEGIN ...` 这一行、只替换标志本身，
    # 私钥正文（base64 块）原样留在库里，`doctor` 复扫也查不出（因为标志已没了）。
    # 改成**整段消费**：从 BEGIN 到 END（含）一起替换，正文不可能残留。
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.S)),
    # 无 END 的残缺块（被截断的 PEM）：从 BEGIN 吃到结尾/一大段，宁可多掩。
    ("private_key_orphan", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{40,}", re.S)),
    # 裸格式兜底（`BEGIN RSA PRIVATE KEY` 不带 `-----`）：`test_corpus_contract`
    # 要求"主库 CREDENTIAL_PATTERNS 的每一条，语料侧都必须有同样命中的规则"。
    # 上面两条管完整 PEM；这条保证裸格式也至少被识别（scan 能看见）。
    ("private_key_bare", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    # `Bearer <8 字符以上>`：整体消费，否则只要值是 base64 就漏。
    ("bearer_token", re.compile(r"Bearer\s+\S{8,}", re.IGNORECASE)),
    # 键+值整体消费：`MY_TOKEN=` / `GITHUB_TOKEN=` / `"access_token": "…"` /
    # `client_secret:` / `api_key=` 全在射程内（大小写不敏感）。
    ("token_env", re.compile(
        r"(?i)[a-z0-9_]*(?:token|secret|api[_-]?key)[\"']?\s*[=:：]\s*[\"']?\S+")),
    # 密码族：可选引号（JSON `"password": "…"` 与 shell `password=…` 都要吃掉）。
    ("password", re.compile(
        r"(?i)(?:password|passwd|pwd|密码|口令)[\"']?\s*[=:：]\s*[\"']?\S+")),
)

# 规则清单的只读视图（依赖说明文档与体检回执都印它，避免文档↔代码漂移）。
RULE_NAMES = tuple(name for name, _ in RULES)


class RedactError(ValueError):
    """脱敏层的参数错误（规则表被动过、开关值不可解释等）。"""


def disabled() -> bool:
    """`FISH_CORPUS_NO_REDACT=1` 时返回 True（只用于造红，出厂关闭）。"""
    raw = (os.environ.get(ENV_DISABLE) or "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise RedactError("环境变量 %s 的值无法解释：%r（用 1/0）" % (ENV_DISABLE, raw))


def redact(text, *, force: bool = False) -> tuple:
    """脱敏一段文本，返回 `(脱敏后文本, 命中数, {类型: 次数})`。

    `force=True` 忽略关断开关（体检用它显式要一份"如果开了会怎样"的读数）。

    为什么返回三元组而不是只返回文本：`redacted_count` 要落库（C-P0-7 判据靠它
    区分"这段没命中"和"这段被整段丢了"），而按类型的计数让体检能印出
    "哪一族漏了"，不用人肉 grep 原始日志。
    """
    if text is None:
        return "", 0, {}
    if not isinstance(text, str):
        text = str(text)
    if not force and disabled():
        return text, 0, {}
    hits = {}
    total = 0
    out = text
    for name, pattern in RULES:
        def _sub(match, _name=name):
            hits[_name] = hits.get(_name, 0) + 1
            return PLACEHOLDER % _name
        out, n = pattern.subn(_sub, out)
        total += n
    return out, total, hits


def scan(text, *, force: bool = True) -> list:
    """扫描（**不替换**），返回 `[(类型, 起始, 结束), ...]`，按起始位置排序。

    `force=True`（默认）表示"不管开关怎么说，都把命中的形态列出来"：体检的意义是
    看库里有什么，不是看当前配置允许什么。调用方要复现"关掉脱敏后的入库结果"时，
    传 `force=False` 即可与 `redact()` 同口径。
    """
    if text is None:
        return []
    if not isinstance(text, str):
        text = str(text)
    if not force and disabled():
        return []
    found = []
    for name, pattern in RULES:
        for m in pattern.finditer(text):
            found.append((name, m.start(), m.end()))
    found.sort(key=lambda item: (item[1], item[2]))
    return found


def scan_count(text, *, force: bool = True) -> int:
    """命中条数（体检的硬门槛读数：必须是 0）。"""
    return len(scan(text, force=force))


def describe() -> str:
    """人读的规则清单（写进依赖说明文档与 `corpus doctor` 回执）。"""
    return "\n".join("  - %-15s %s" % (name, pattern.pattern) for name, pattern in RULES)


class Redactor:
    """`Redactor`（设计 §3 类图）：规则表的对象形态。

    三道脱敏都该拿同一个实例（或干脆用模块函数）——类只是为了让 `Ranker`/`Tagger`
    这些同层对象有一致的注入风格，**不复制任何实现**。
    """

    RULES = RULES

    def redact(self, text, *, force: bool = False) -> tuple:
        return redact(text, force=force)

    def scan(self, text, *, force: bool = True) -> list:
        return scan(text, force=force)
