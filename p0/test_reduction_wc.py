#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""112 · W-C（项目解析单源）判据：A2。

## 判别力来自哪里（112 §七点名的「假绿」）

旧的 `test_surface_naming.py:198`（`test_fish_resolver_same_answer`）用
`/a/b/c/x.py` 这类**不存在的纯文本路径**——两边都进不了 `realpath` 分支，
所以它在"两个解析器已经分叉"的事实下**依然全绿**。本文件补的是那个缺口：

  · **真 junction**（`mklink /J` 现建现删，不依赖某台机器的 home 状态）；
  · **前提自证**：先断言这两种写法"只做文本归一"时**不相等**——否则测不到 realpath；
  · **对照组**：非 junction 的普通目录上两个解析器也必须一致（证明判据不是只会红）。
  · 变异：摘掉 `fish_surface` 的 canonical 兜底 → 本文件红。

## 行为说明

生产 `workspace_map` 5 行今天没有 junction，所以这轮对现网**行为中性**
（C-C3 的对拍读数在实施记录里，不放进测试：测试不该依赖生产库）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402
import fish_activity  # noqa: E402
import fish_surface  # noqa: E402


def _make_junction(link: Path, target: Path) -> bool:
    """"真的造一个 junction（Windows 上建 junction 不需要管理员）。成功才返回 True。"""
    try:
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and link.exists()


class JunctionParityTest(unittest.TestCase):
    """C-C1：junction 场景下 `fish` 与 `fish_surface`/`fish_activity` 给同一项目。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-wc-junction-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.real = self.tmp / "real-proj"
        (self.real / "sessions").mkdir(parents=True)
        self.link = self.tmp / "link-proj"
        if not _make_junction(self.link, self.real):
            raise unittest.SkipTest("本机建不了 junction（mklink /J 失败）——测不到这一层")
        self.addCleanup(self._drop_link)
        self.conn = fish.connect(self.tmp / "data")
        self.addCleanup(self.conn.close)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute("DELETE FROM workspace_map")
        self.conn.commit()

    def _drop_link(self):
        try:
            os.rmdir(str(self.link))
        except OSError:
            pass

    def _map(self, path, project):
        fish.set_workspace_mapping(self.conn, str(path), project)
        self.conn.commit()

    def _pairs(self, pairs):
        return [(str(path), project) for path, project in pairs]

    def test_fixture_is_a_real_junction(self):
        """前提自证：文本归一后链接路径与真实路径**不相等**，且两种写法都不等于 None。

        若哪天 mklink 造出来的东西被文本归一折平了，下面的绿就不是 realpath 的功劳。
        """
        norm_link = fish_surface.normalize_path(str(self.link))
        norm_real = fish_surface.normalize_path(str(self.real))
        self.assertNotEqual(norm_link, norm_real,
                            "夹具无效：链接路径与真实路径文本归一后就相等了")
        self.assertNotIn(fish_surface.normalize_path(str(self.link / "sessions")),
                         (norm_real, norm_real + "/sessions"),
                         "夹具无效：链接写法恰好就是真实写法")

    def test_mapping_real_query_link(self):
        """映射写真实路径、用链接路径查询：两个解析器都要给出 REALPROJ。"""
        self._map(self.real, "REALPROJ")
        target = str(self.link / "sessions")
        fish_answer = fish.resolve_project_by_cwd(self.conn, target)
        self.assertEqual(fish_answer, "REALPROJ", "fish 侧自己就断了（前提失效）")
        surface_answer = fish_surface.longest_prefix(target, self._pairs([(self.real, "REALPROJ")]))
        self.assertIsNotNone(surface_answer,
                             "fish_surface 在 junction 上给 None——与 fish 解析器分叉（112 N-2）")
        self.assertEqual(surface_answer[1], fish_answer,
                         "两个解析器给了不同项目")
        activity_answer = fish_activity.resolve_project(self.conn, target)
        self.assertEqual(activity_answer[0], fish_answer,
                         "fish_activity.resolve_project（true 生产 hook 路径）仍与 fish 分叉")

    def test_mapping_link_query_real(self):
        """反方向：映射写链接路径、用真实路径查询。"""
        mapped = self.link / "sessions" / "--proj--"
        self._map(mapped, "LINKPROJ")
        target = str(self.real / "sessions" / "--proj--" / "deeper")
        fish_answer = fish.resolve_project_by_cwd(self.conn, target)
        self.assertEqual(fish_answer, "LINKPROJ", "fish 侧反方向自己就断了")
        surface_answer = fish_surface.longest_prefix(
            target, self._pairs([(mapped, "LINKPROJ")]))
        self.assertIsNotNone(surface_answer, "fish_surface 反方向给 None（分叉）")
        self.assertEqual(surface_answer[1], fish_answer)

    def test_longest_prefix_and_component_boundary_survive_realpath(self):
        """realpath 只补一次比对，**不许**把最长前缀/分量边界弄丢。"""
        self._map(self.real, "OUTER")
        self._map(self.real / "sessions", "INNER")
        pairs = self._pairs([(self.real, "OUTER"), (self.real / "sessions", "INNER")])
        self.assertEqual(fish.resolve_project_by_cwd(
            self.conn, str(self.link / "sessions" / "abc")), "INNER")
        self.assertEqual(fish_surface.longest_prefix(
            str(self.link / "sessions" / "abc"), pairs)[1], "INNER")
        self.assertEqual(fish.resolve_project_by_cwd(self.conn, str(self.link / "other")), "OUTER")
        self.assertEqual(fish_surface.longest_prefix(
            str(self.link / "other"), pairs)[1], "OUTER")
        self.assertEqual(fish.resolve_project_by_cwd(self.conn, str(self.link / "sessionsX")),
                         "OUTER", "分量边界被破坏：`sessionsX` 被 `sessions` 吞了")
        self.assertEqual(fish_surface.longest_prefix(
            str(self.link / "sessionsX"), pairs)[1], "OUTER")

    def test_unrelated_path_still_none(self):
        self._map(self.real, "REALPROJ")
        self.assertIsNone(fish_surface.longest_prefix(
            "C:/Windows/System32", self._pairs([(self.real, "REALPROJ")])),
            "realpath 把不同目录折到一起了")


class NonJunctionControlTest(unittest.TestCase):
    """对照组（绿）：普通目录上两个解析器本来就一致——证明判据不是"任何输入都红"。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-wc-plain-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mapped = self.tmp / "proj"
        (self.mapped / "sub").mkdir(parents=True)
        self.conn = fish.connect(self.tmp / "data")
        self.addCleanup(self.conn.close)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute("DELETE FROM workspace_map")
        fish.set_workspace_mapping(self.conn, str(self.mapped), "PLAINPROJ")
        self.conn.commit()

    def test_both_resolvers_agree_on_plain_paths(self):
        pairs = [(str(self.mapped), "PLAINPROJ")]
        for target in (str(self.mapped), str(self.mapped / "sub"),
                       str(self.mapped / "sub" / "x.py")):
            fish_answer = fish.resolve_project_by_cwd(self.conn, target)
            self.assertEqual(fish_answer, "PLAINPROJ")
            surface_answer = fish_surface.longest_prefix(target, pairs)
            self.assertIsNotNone(surface_answer)
            self.assertEqual(surface_answer[1], fish_answer)
            self.assertEqual(fish_activity.resolve_project(self.conn, target)[0], fish_answer)

    def test_unmapped_path_is_none_on_both(self):
        pairs = [(str(self.mapped), "PLAINPROJ")]
        target = str(self.tmp / "elsewhere")
        self.assertIsNone(fish.resolve_project_by_cwd(self.conn, target))
        self.assertIsNone(fish_surface.longest_prefix(target, pairs))


class SingleSourceTest(unittest.TestCase):
    """结构面：`fish.resolve_project_by_cwd` 只剩"查表 + 交给 fish_surface"。"""

    def test_fish_resolver_delegates(self):
        src = (P0 / "fish.py").read_text(encoding="utf-8")
        # 取 resolve_project_by_cwd 的函数体（到下一个顶层 def 为止）
        start = src.index("def resolve_project_by_cwd(")
        end = src.index("\ndef ", start + 10)
        body = src[start:end]
        self.assertIn("fish_surface.longest_prefix", body,
                      "fish.resolve_project_by_cwd 没有委托 fish_surface（单源没落地）")
        self.assertNotIn("normalize_workspace_path(prefix)", body,
                         "fish.resolve_project_by_cwd 还在自己扫表比对（两份实现并存）")
        self.assertNotIn("_canonical_path(prefix)", body,
                         "canonical 兜底还留在 fish.py（应只在 fish_surface 里）")

    def test_canonical_path_lives_in_surface(self):
        self.assertTrue(hasattr(fish_surface, "canonical_path"),
                        "physical 归一没下沉到 fish_surface（fish_activity 用不到它）")
        self.assertIs(fish._canonical_path, fish_surface.canonical_path,
                      "fish 里应留**转发别名**（测试与既有调用依赖 `fish._canonical_path`）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
