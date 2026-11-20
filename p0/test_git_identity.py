#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git 仓库身份层的判据（2026-11-20 · fish 引入 git 的 Phase 2）。

## 这份测试守的是什么

两条**被实测抓出来的**真缺陷，以及一条覆盖率底线：

1. 代理形态剥不干净 —— 第一版 `normalize_remote_url` 只判断「rest 是否以
   `scheme://` 开头」，但代理 URL 的 rest 是 `ghfast.top/https://github.com/...`，
   它以主机名开头，那个判断永远为假，前缀就剥不掉。实测输出
   `ghfast.top/https://github.com/tt-a1i/archify` —— 一个仓库两个身份。
2. `read_remotes` 把 name 和 url 写反 —— 实测输出 `[('git@github.com:...', 'origin')]`。
3. 发现器必须 100% 命中：v1.0 的「从容器根向下扫」实测从 4 条根只发现 4 个仓库，
   而「从路径向上找」在真实机器上是 45/45。

## 为什么这些用例用临时仓库

身份归一是纯函数，但发现器要真 git 仓库才有意义。用 `tempfile` 造，**不碰
任何现有仓库**——测试不该依赖开发机的具体目录布局，也不该去改真实仓库的 index。
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

import fish_git  # noqa: E402


def _git(args, cwd):
    """跑一条 git，失败就抛——测试里出错要响，不要静默。"""
    subprocess.run(
        ["git"] + list(args), cwd=str(cwd), check=True,
        capture_output=True,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )


def _mk_repo(path, *, remote=None):
    """造一个带一次提交的最小仓库。"""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.com"], path)
    _git(["config", "user.name", "tester"], path)
    (path / "a.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "a.txt"], path)
    _git(["commit", "-q", "-m", "init"], path)
    if remote:
        _git(["remote", "add", "origin", remote], path)
    return path


class RemoteIdentityTest(unittest.TestCase):
    """远端 URL → identity 的归一。"""

    def test_scp_and_https_are_the_same_identity(self):
        """同一仓库的 SCP 与 HTTPS 写法必须归一（否则算两个仓库）。"""
        a = fish_git.normalize_remote_url("git@github.com:kinganyi/fish.git")
        b = fish_git.normalize_remote_url("https://github.com/kinganyi/fish.git")
        self.assertEqual(a, b,
                         "SCP 与 HTTPS 写法归一后必须相同，否则同一仓库会占两行")
        self.assertEqual(a, ("github.com/kinganyi/fish", "direct"))

    def test_proxy_prefix_is_stripped(self):
        """代理前缀必须剥干净 —— 第一版在这里错过（rest 以主机名开头）。"""
        ident, kind = fish_git.normalize_remote_url(
            "https://ghfast.top/https://github.com/tt-a1i/archify.git")
        self.assertEqual(ident, "github.com/tt-a1i/archify",
                         "代理前缀没剥掉：identity 里不该出现代理主机")
        self.assertEqual(kind, "proxy")

    def test_mirror_keeps_its_own_identity(self):
        """镜像站的身份**不是**上游身份（布局不同），但要标 mirror。"""
        ident, kind = fish_git.normalize_remote_url(
            "https://gitcode.com/gh_mirrors/co/ComfyUI.git")
        self.assertEqual(kind, "mirror")
        self.assertTrue(ident.startswith("gitcode.com/"),
                        "镜像站的 identity 不该被改写成上游 github 身份")

    def test_local_path_remote(self):
        """远端本身是本地路径时，identity 用 local: 前缀，不硬算成主机名。"""
        ident, kind = fish_git.normalize_remote_url(r"I:\app\fish-devA")
        self.assertEqual(kind, "local_path")
        self.assertTrue(ident.startswith("local:"), f"实际 identity={ident!r}")

    def test_unknown_returns_none_not_guess(self):
        """判不到就是 None —— 按 fish 的纪律，不猜。"""
        self.assertEqual(fish_git.normalize_remote_url(""), (None, "unknown"))
        self.assertEqual(fish_git.normalize_remote_url("   "), (None, "unknown"))

    def test_dot_git_suffix_does_not_split_identity(self):
        """带不带 .git 后缀是同一个仓库。"""
        a = fish_git.normalize_remote_url("https://github.com/o/r.git")
        b = fish_git.normalize_remote_url("https://github.com/o/r")
        self.assertEqual(a, b)


class DiscoverRootTest(unittest.TestCase):
    """从任意路径向上找仓库根。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-git-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_root_from_nested_dir(self):
        """从深层子目录也要能找到根。"""
        repo = _mk_repo(self.tmp / "repo")
        deep = repo / "a" / "b" / "c"
        deep.mkdir(parents=True)
        got = fish_git.discover_root(deep)
        self.assertIsNotNone(got, "从子目录找不到仓库根")
        self.assertTrue(got.endswith("repo"),
                        f"期望落在 repo 根，实际 {got!r}")

    def test_finds_root_from_file_path(self):
        """落笔路径可能是文件（Write 的 file_path）——要从它的目录找。"""
        repo = _mk_repo(self.tmp / "repo")
        got = fish_git.discover_root(repo / "a.txt")
        self.assertIsNotNone(got)
        self.assertTrue(got.endswith("repo"))

    def test_nested_repo_returns_its_own_root(self):
        """仓库里套仓库时，返回**最近**那个根，不吞并到父项目。

        这条是 v1.0 的实测错误点：用「最长前缀匹配」时，ComfyUI 会被判成
        anyi-studio 的一部分。`rev-parse --show-toplevel` 天然正确。
        """
        outer = _mk_repo(self.tmp / "outer")
        inner = _mk_repo(outer / "inner")
        got_outer = fish_git.discover_root(outer)
        got_inner = fish_git.discover_root(inner)
        self.assertTrue(got_outer.endswith("outer"), f"外层根错: {got_outer!r}")
        self.assertTrue(got_inner.endswith("inner"),
                        f"嵌套仓库应返回自己: {got_inner!r}")

    def test_non_repo_returns_none(self):
        """不是仓库就返回 None，不往上层瞎猜。"""
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(fish_git.discover_root(plain))

    def test_missing_path_returns_none(self):
        self.assertIsNone(fish_git.discover_root(self.tmp / "nope-not-here"))

    def test_read_remotes_returns_name_then_url(self):
        """`read_remotes` 的顺序必须是 (name, url) —— 第一版写反了。"""
        repo = _mk_repo(self.tmp / "repo", remote="git@github.com:o/r.git")
        remotes = fish_git.read_remotes(repo)
        self.assertEqual(remotes, [("origin", "git@github.com:o/r.git")],
                         f"name/url 顺序错了: {remotes!r}")


class ScanInventoryTest(unittest.TestCase):
    """离线扫描与清单生成（P2-2）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-git-scan-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_finds_nested_repos_independently(self):
        """嵌套仓库必须**各自独立**被发现，不被父仓库吞并。

        实测对应物：`anyi-studio/anyi-studio-comfy/ComfyUI` 是独立仓库，
        v1.0 的「最长前缀匹配」会把它判成 anyi-studio 的一部分。
        """
        _mk_repo(self.tmp / "outer")
        _mk_repo(self.tmp / "outer" / "sub" / "inner")
        repos = fish_git.scan_repos([self.tmp], max_depth=5)
        paths = [r["path"] for r in repos]
        self.assertEqual(len(repos), 2, f"嵌套仓库应发现 2 个，实际 {paths}")
        self.assertTrue(any(p.endswith("outer") for p in paths))
        self.assertTrue(any(p.endswith("inner") for p in paths))

    def test_scan_skips_noise_dirs(self):
        """`node_modules` 里的假 `.git` 不该被当成仓库。"""
        repo = _mk_repo(self.tmp / "repo")
        fake = repo / "node_modules" / "pkg"
        fake.mkdir(parents=True)
        (fake / ".git").mkdir()
        repos = fish_git.scan_repos([self.tmp], max_depth=6)
        self.assertEqual(len(repos), 1,
                         f"噪声目录里的 .git 被误当成仓库: {[r['path'] for r in repos]}")

    def test_scan_respects_max_depth(self):
        """超过 max_depth 的仓库不该被发现（深度是显式参数，不是猜的）。"""
        _mk_repo(self.tmp / "a" / "b" / "c" / "deep")
        self.assertEqual(len(fish_git.scan_repos([self.tmp], max_depth=2)), 0)
        self.assertEqual(len(fish_git.scan_repos([self.tmp], max_depth=6)), 1)

    def test_inventory_shape_is_self_consistent(self):
        """清单的 count 必须等于 repos 长度，且字段齐全。"""
        _mk_repo(self.tmp / "repo", remote="https://github.com/o/r.git")
        out = self.tmp / "inv.json"
        payload = fish_git.write_inventory(out, [self.tmp])
        self.assertTrue(out.exists(), "清单文件没写出来")
        self.assertEqual(payload["count"], len(payload["repos"]))
        for key in ("version", "generated_at", "roots", "max_depth", "count",
                    "with_identity", "repos"):
            self.assertIn(key, payload, f"清单缺字段 {key}")
        for r in payload["repos"]:
            for key in ("path", "identity", "identity_kind", "remotes", "shallow"):
                self.assertIn(key, r, f"条目缺字段 {key}")
        self.assertEqual(payload["with_identity"], 1, "有远端的仓库应被计入 with_identity")


class ProbeTest(unittest.TestCase):
    """L0/L1 观测探针。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-git-probe-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_l0_reports_branch_and_head(self):
        repo = _mk_repo(self.tmp / "repo")
        l0 = fish_git.probe_l0(repo)
        self.assertIsNotNone(l0["head_sha"], "拿不到 HEAD")
        self.assertEqual(len(l0["head_sha"]), 40, "HEAD 应是 40 位 SHA")
        self.assertIsNotNone(l0["branch"], "拿不到分支名")

    def test_l1_clean_repo(self):
        repo = _mk_repo(self.tmp / "repo")
        l1 = fish_git.probe_l1(repo)
        self.assertEqual(l1["state"], "clean", f"刚提交的仓库应为 clean: {l1}")
        self.assertEqual(l1["dirty"], 0)

    def test_l1_dirty_repo(self):
        """脏仓库必须报 dirty —— 且**不能**报成 clean（判不到≠干净的反面）。"""
        repo = _mk_repo(self.tmp / "repo")
        (repo / "b.txt").write_text("new\n", encoding="utf-8")
        l1 = fish_git.probe_l1(repo)
        self.assertEqual(l1["state"], "dirty")
        self.assertEqual(l1["dirty"], 1)
        self.assertGreaterEqual(l1["untracked"], 1, "新增未跟踪文件应被计入")

    def test_l1_non_repo_is_degraded_not_clean(self):
        """**判不到 ≠ 干净**：非仓库必须 degraded/unknown，绝不能报 clean。"""
        plain = self.tmp / "plain"
        plain.mkdir()
        l1 = fish_git.probe_l1(plain)
        self.assertNotEqual(l1["state"], "clean",
                            "非仓库被判成 clean —— 这正是「判不到≠干净」要防的")
        self.assertIn(l1["state"], ("degraded", "unknown"))

    def test_shallow_detection(self):
        repo = _mk_repo(self.tmp / "repo")
        self.assertFalse(fish_git.probe_shallow(repo), "普通仓库不该被判成 shallow")


if __name__ == "__main__":
    unittest.main()
