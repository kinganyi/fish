#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**引用"用户拍板"的注释必须能指到原话**——否则它只是一句自述。

## 为什么加这条

2026-11-19，用户当面更正了一件我做错 14 轮的事：

    我把「有 token 只能读自己与未归属段」当成用户拍板，
    而那是我自己写进工作目标的一句**摘要**。

第 38–51 轮全建在那句摘要上；五轮独立评审全部确认"实现与摘要一致"，
**没有一轮问过"摘要是对用户意图的转述吗"**。

> 判据只能验证「实现符合规格」。它验证不了「规格符合意图」。

## 这条判据怎么工作

扫全仓注释/文档串里所有"用户拍板 / 某日决定"的声明，要求**同一处**
能指到原话：`原话` / `原文` / 记忆编码 / `.md` 文档路径 / `见 docs`。

## 为什么不一次把 25 条都补齐

因为那会变成"为了过判据去编原话"——比没有判据更坏。
所以用**棘轮**：存量 26 条**冻结在下面**并逐条标明"待补原话"，
**新增一条没有锚点的声明就红**。存量要一条一条真去补，补完从名单里删。

棘轮方向只许少：名单只能变短，不能变长。
"""
import io
import os
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 触发词：声称"用户拍板/某日决定"
CLAIM_RE = re.compile(
    r"(用户\s*拍板|拍板决定|拍板：|拍板:|\d{4}-\d{2}-\d{2}\s*(用户)?(拍板|决定|裁定))")

# 锚点：能指到原话的东西
ANCHOR_RE = re.compile(
    r"(原话|原文|大肥鱼生命系统-\d{6}-\d+|\.md|见\s*docs|原文见|quote)")

# ---------------------------------------------------------------- 冻结名单
# 键 = `文件名|去空白后的前 60 字`。
# **只许变短**：补齐原话后从名单里删掉，不许往里加。
# 这些是 2026-11-19 那次事故之前就写在代码里的存量声明，它们可能对、
# 也可能是转述——**没有原话就无法判定**，所以这里如实标成"待补"，
# 而不是假装它们已经被核实。
FROZEN = {
    "fish.py|#（用户2026-10-25拍板）。跟着变会让所有外部引用（人写的文档、别的AI的笔记）失效。",
    "fish.py|#③**标题截24字符**（用户拍板）。实测标题平均38.7/最长81，",
    "fish.py|#（用户拍板）。同一份名字集合必须用在**两个地方**：FTS命中的下推过滤，以及",
    "fish.py|#2026-10-26（T03）：会话开场**自动签到**（用户拍板「自动行算登记」）。",
    "fish.py|\"\"\"**自动签到**：SessionStart/写记忆时把这一行建出来或刷新（用户2026-10-26拍板）。",
    "fish.py|**软门不硬拦**（用户2026-10-25拍板）：",
    "fish.py|##契约（用户2026-10-26拍板：不要ULID）",
    "fish_activity.py|##隐私红线（用户2026-10-26拍板+设计§10.3）",
    "fish_activity.py|#修法不是把warn改成输出决策（那等于偷偷开ask，用户拍板不允许），而是走",
    "fish_common.py|#2026-11-18拍板的只有\"无token要被引导获取token\"这一条。",
    "fish_common.py|#自动行算登记（用户2026-10-26拍板）：SessionStart就建/刷自动行。",
    "fish_common.py|#**三态**（用户2026-10-26拍板+主理人决策）：off/warn（默认）/ask。",
    "fish_common.py|#隐私开关（2026-10-29·用户拍板：private是**可选项**、不强制）。",
    "fish_common.py|**写入仍按原名归属**（用户2026-10-25拍板：别名只影响检索，不改已有数据的归属语义）。",
    "fish_dsh_hook.py|#凭证有效期（小时）。用户2026-10-24拍板：24h。",
    "fish_kb.py|#聚合表答不了（回滚是用户拍板的硬需求，只有流水能定位到具体那次写）。",
    "fish_naming.py|##为什么要有它（用户2026-10-26拍板）",
    "fish_pretool_hook.py|##为什么默认是`warn`而不是`ask`（用户2026-10-26拍板+主理人决策）",
    "fish_session_hook.py|#用户拍板）。它只在显式传时写，所以MCP的`tool_inject`不会因为\"打开看板\"",
    "fish_trust.py|#口径是**用户2026-11-11拍板**的，不是实现者的判断：",
    "fish_visibility.py|\"\"\"按`visibility`过滤检索结果（**可选**；用户2026-10-29拍板\"做成开关、不强制\"）。",
    "mcp_server.py|#外部审查🔴6（分级写入鉴权，用户2026-11-11拍板）：",
    "mcp_server.py|#2026-10-26（T03）：会话开场自动签到（自动行算登记，用户拍板）。",
    "mcp_server.py|#无凭证→明确拒绝并指引取token（用户拍板：不静默降级成只看未归属，",
    "mcp_server.py|#fish全功能、读它需要的任何内容。2026-11-18拍板的只有",
}


def _claim_key(fname, line):
    return fname + "|" + re.sub(r"\s+", "", line.strip())[:60]


# ---------------------------------------------------------------- 锚点必须是真的
# **第七轮评审**：第一版的 ANCHOR_RE 里有 `\.md`，于是**任意 `.md` 字样**都算锚点
# ——连文件存不存在都不查。评审官实测：
#   `# 用户拍板：有 token 只能读自己与未归属段（见 notes.md）` -> **绿**
#   同一句去掉 `（见 notes.md）` -> 红
# 它验的是"有没有像引用的东西"，不是"引用说的是不是这回事"。
#
# 现在分三类：
#   ① 真实存在的 .md 文件      -> strong（可自证）
#   ② 记忆编码 大肥鱼生命系统-YYMMDD-NN -> weak（格式对，存在性这里判不到）
#   ③ 原话 / 原文 / quote      -> weak（只是承诺，最弱但明确）
# 不存在的 .md 路径**不算锚点**——那正是评审官用来骗过判据的那一招。

_MEM_CODE_RE = re.compile(r"大肥鱼生命系统-\d{6}-\d+")
_MD_RE = re.compile(r"([\w\u4e00-\u9fff./\\-]+\.md)")
_WEAK_RE = re.compile(r"(原话|原文|quote)", re.I)


def _md_exists(ref):
    """这个 .md 在仓库、工作区、或上层目录里真的存在吗？"""
    ref = ref.replace("\\", "/")
    for base in (HERE, HERE.parent, HERE.parent.parent):
        try:
            if (base / ref).is_file():
                return True
        except OSError:      # A档：畸形路径当不存在
            continue
    if "/" not in ref:
        return any(p.name == ref for p in HERE.glob("*.md"))
    return False


def _anchor_quality(ctx):
    """返回 strong / weak / None。只有 None 算没有锚点。"""
    for ref in _MD_RE.findall(ctx):
        if _md_exists(ref):
            return "strong"
    if _MEM_CODE_RE.search(ctx):
        return "weak"
    if _WEAK_RE.search(ctx):
        return "weak"
    return None


def _scan():
    found = []
    for p in sorted(HERE.glob("*.py")):
        if p.name.startswith(("test_", "mutation_", "_")):
            continue
        lines = p.read_text(encoding="utf-8").split("\n")
        for k, x in enumerate(lines):
            if not CLAIM_RE.search(x):
                continue
            if not (x.lstrip().startswith("#") or '"""' in x
                    or x.lstrip().startswith("*")):
                continue
            ctx = "\n".join(lines[max(0, k - 2):k + 3])
            found.append((_claim_key(p.name, x), p.name, k + 1,
                          _anchor_quality(ctx)))
    return found


class DecisionAnchorTest(unittest.TestCase):

    def test_new_claims_carry_an_anchor(self):
        """**新增**的"用户拍板"声明必须能指到原话。"""
        new_unanchored = [f"{f}:{ln}  {key.split('|', 1)[1][:50]}"
                          for key, f, ln, ok in _scan()
                          if ok is None and key not in FROZEN]
        self.assertEqual(
            new_unanchored, [],
            "这些注释声称「用户拍板」却指不到原话——\n"
            "  要么补上原话/记忆编码/文档路径，要么别写「拍板」这两个字：\n  "
            + "\n  ".join(new_unanchored))

    def test_frozen_list_only_shrinks(self):
        """冻结名单只许变短——补齐原话后要把它删掉，不许留在里面。"""
        current = {key for key, _f, _ln, ok in _scan() if not ok}
        stale = sorted(FROZEN - current)
        self.assertEqual(
            stale, [],
            f"这些条目已经不在'无锚点'状态（补好了或删了代码），"
            f"请从 FROZEN 里删掉它们：{stale}")

    def test_scan_is_not_empty(self):
        """失靶守卫：扫不到任何声明说明扫描逻辑坏了，不是通过。"""
        self.assertGreaterEqual(len(_scan()), 20,
                                "只扫到很少的声明——判据大概率失去靶点")


    def test_a_fake_anchor_is_not_an_anchor(self):
        """**第七轮评审**：第一版的 `.md` 匹配让**任意 `.md` 字样**都算锚点。

        评审官实测：往源码里加一句
        `# 用户拍板：有 token 只能读自己与未归属段（见 notes.md）`
        ——`notes.md` **根本不存在**，判据却**绿**。去掉那半就成了红。

        也就是说它验的是"有没有像引用的东西"，不是"引用是不是真的"。
        这条判据直接调 `_anchor_quality`，问三种情况的判定：
        """
        # ① 不存在的 .md -> 不算锚点（这正是骗过判据的那一招）
        self.assertIsNone(
            _anchor_quality("用户拍板：只能读自己（见 notes.md）"),
            "指向不存在的 .md 被当成了锚点——评审官就是这样骗过的")
        # ② 真实存在的 .md -> 算锚点（strong）
        self.assertEqual(
            _anchor_quality("用户拍板：只能读自己（原文见 ../README.md）"),
            "strong", "真实存在的 .md 没被认成锚点——判据会误伤")
        # ③ 记忆编码 -> 弱锚点（存在性这里判不到，不假装验过）
        self.assertEqual(
            _anchor_quality("用户拍板：大肥鱼生命系统-261119-02"),
            "weak", "记忆编码没被认成锚点")

    def test_md_exists_checks_the_file_system(self):
        """`_md_exists` 必须**真的去文件系统问**，不是猜。"""
        self.assertTrue(_md_exists("../README.md"),
                        "存在的 md 被判成不存在")
        self.assertFalse(_md_exists("绝对不存在的文件名-xyz.md"),
                         "不存在的 md 被判成存在")


if __name__ == "__main__":
    unittest.main(verbosity=2)
