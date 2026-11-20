#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""撞车判定的仓库维度（P5-2 · J7/J8 · 2026-11-20）。

## 这份测试守的两条边界

方案 §6 缺口三给了三条规则：

```
同 repo_id      → 按现有 surface_hit 判分量边界（逻辑不变）
不同 repo_id    → 一律不冲突
repo_id 未知    → 退化为相对路径匹配，且必须标注「未锚定仓库，可能是误报」
```

前两条对应 **J7（跨仓库同名不冲突）** 与 **J8（同仓库嵌套正确归并）**，
第三条对应「诚实标注」——它同样是判据，因为**不标注的后果就是把假撞车
当真撞车**，而「警告掺假就会被学会忽视」是这个模块里已有的原话。

## 为什么用构造的快照而不是真跑 hook

`find_collisions` 是**纯函数**（吃 snapshot dict、吐 hits list）。给它构造一个
快照就能完整覆盖判定逻辑，不需要起进程、不需要写库。真跑 hook 的价值在
「快照是怎么被生产出来的」，那由 `test_git_identity.py` 的 build_snapshot 部分守。
"""
import sys
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish_activity  # noqa: E402


def _entry(session_key, repo_id, surface, *, label="agent", citizen="c1",
           minutes_ago=2):
    """造一条快照 entry。`minutes_ago` 用固定 now 反推，避免时钟漂移。"""
    return {
        "session_key": session_key, "citizen_id": citizen, "label": label,
        "project": f"repo{repo_id}", "repo_id": repo_id,
        "cwd": f"i:/app/repo{repo_id}",
        "declared": [surface.split("/")[-1]],
        "declared_abs": [surface],
        "declared_at": "2026-11-20T13:58:00Z", "auto": [],
        "last_seen": "2026-11-20T13:58:00Z",
    }


NOW = "2026-11-20T14:00:00Z"


def _snapshot(*entries):
    return {"generated_at": NOW, "entries": list(entries)}


class CrossRepoTest(unittest.TestCase):
    """J7：不同仓库里的同名路径**不产生撞车**。

    变异体（去掉仓库过滤 → 红）：两个仓库各有 `src/lib`，target 在 repoB，
    旧口径会同时命中 repoA 和 repoB。
    """

    def setUp(self):
        self.snap = _snapshot(
            _entry("dsh:other-A", 1, "i:/app/repo1/src/lib", label="agent-A", citizen="cA"),
            _entry("dsh:other-B", 2, "i:/app/repo2/src/lib", label="agent-B", citizen="cB"),
        )

    def test_same_relative_path_in_other_repo_is_not_a_collision(self):
        hits = fish_activity.find_collisions(
            self.snap, target_abs="i:/app/repo2/src/lib/x.ts",
            own_session_key="dsh:me", target_repo_id=2, now=NOW)
        labels = [h["label"] for h in hits]
        self.assertEqual(labels, ["agent-B"],
                         f"跨仓库同名路径被判成撞车了：命中 {labels}")

    def test_unanchored_target_degrades_and_says_so(self):
        """repo_id 未知 → 退化为路径匹配，但**必须**标 `repo_unanchored`.

        变异体（不标注 → 红）：把 target_repo_id 传 None，命中仍返回，
        但 `repo_unanchored` 必须是 True——否则读者会把「同名」当「同一文件」。
        """
        hits = fish_activity.find_collisions(
            self.snap, target_abs="i:/app/repo1/src/lib/x.ts",
            own_session_key="dsh:me", target_repo_id=None, now=NOW)
        self.assertTrue(hits, "退化模式下应当仍然报（漏报 > 多一句话是既有取舍）")
        self.assertTrue(all(h.get("repo_unanchored") for h in hits),
                        f"没标「未锚定仓库」：{[h.get('repo_unanchored') for h in hits]}")

    def test_both_anchored_and_equal_is_not_flagged_unanchored(self):
        """两侧都锚定且相同 → 正常判，且**不**标 unanchored（别把真撞车说成误报）。"""
        hits = fish_activity.find_collisions(
            self.snap, target_abs="i:/app/repo1/src/lib/x.ts",
            own_session_key="dsh:me", target_repo_id=1, now=NOW)
        self.assertTrue(hits)
        self.assertFalse(any(h.get("repo_unanchored") for h in hits),
                         "两侧都有锚点却被标成「未锚定」")


class NestedSameRepoTest(unittest.TestCase):
    """J8：同一个仓库里的嵌套子目录**正确归并**（不该漏报）。

    变异体（改成精确匹配 → 红）：把 `startswith(base + "/")` 换成 `==`，
    target 在更深的子目录时就不命中，真撞车被漏掉。
    """

    def test_nested_subdir_still_hits(self):
        snap = _snapshot(
            _entry("dsh:other-A", 1, "i:/app/repo1/src/lib", label="agent-A"),
        )
        hits = fish_activity.find_collisions(
            snap, target_abs="i:/app/repo1/src/lib/deep/nested/x.ts",
            own_session_key="dsh:me", target_repo_id=1, now=NOW)
        self.assertEqual([h["label"] for h in hits], ["agent-A"],
                         "同仓库嵌套子目录被漏报了（分量语义不能退化成精确匹配）")

    def test_deeper_entry_matches_even_deeper_target(self):
        """别人声明更深的目录，我写它**下面**的文件 → 命中（分量语义是双向包含）。"""
        snap = _snapshot(
            _entry("dsh:other-A", 1, "i:/app/repo1/src/lib/deep", label="agent-A"),
        )
        hits = fish_activity.find_collisions(
            snap, target_abs="i:/app/repo1/src/lib/deep/x.ts",
            own_session_key="dsh:me", target_repo_id=1, now=NOW)
        self.assertEqual([h["label"] for h in hits], ["agent-A"],
                         "声明面之下的写入没命中")

    def test_sibling_subtrees_do_not_hit(self):
        """**反向边界**：同仓库里互不为前缀的两个子树不算重叠。

        别人声明 `src/lib/deep`，我写 `src/lib/x.ts` —— 它们在 `src/lib` 处
        就分岔了（一个是子目录、一个是文件），不是同一块。这条守着
        「分量语义」不被误扩成「公共祖先也算重叠」。
        """
        snap = _snapshot(
            _entry("dsh:other-A", 1, "i:/app/repo1/src/lib/deep", label="agent-A"),
        )
        hits = fish_activity.find_collisions(
            snap, target_abs="i:/app/repo1/src/lib/x.ts",
            own_session_key="dsh:me", target_repo_id=1, now=NOW)
        self.assertEqual(hits, [], "两个兄弟子树被误判成重叠了")

    def test_same_session_is_never_reported(self):
        """自己绝不报自己（既有边界，改动后再确认一次没被这次修改破坏）。"""
        snap = _snapshot(
            _entry("dsh:me", 1, "i:/app/repo1/src/lib", label="me"),
        )
        hits = fish_activity.find_collisions(
            snap, target_abs="i:/app/repo1/src/lib/x.ts",
            own_session_key="dsh:me", target_repo_id=1, now=NOW)
        self.assertEqual(hits, [], "给自己报了撞车")


if __name__ == "__main__":
    unittest.main()
