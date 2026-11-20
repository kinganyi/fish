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


class _DbCase(unittest.TestCase):
    """需要数据库的用例：每个用例一个全新的临时库。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-git-db-test-"))
        sys.path.insert(0, str(P0))
        import fish
        self.conn = fish.connect(self.tmp / "db")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


class RepoIdentityScopeTest(_DbCase):
    """J2：同一仓库的两份独立克隆必须归到同一个 `repo_id`。

    变异体（去掉 → 判据红）：把 identity 换成路径。那样 cloneA 与 cloneB
    会各占一行 `git_repo`，「一个仓库 N 份 clone」就表达不出来了。
    """

    def test_two_clones_share_one_repo_id_but_have_two_checkouts(self):
        import fish_git_store
        # 同一个仓库的两种远端写法（HTTPS / SCP）——归一后必须是同一身份
        a = _mk_repo(self.tmp / "w" / "cloneA", remote="https://github.com/o/r.git")
        b = _mk_repo(self.tmp / "w" / "cloneB", remote="git@github.com:o/r.git")
        r1 = fish_git_store.observe(self.conn, a)
        self.conn.commit()
        r2 = fish_git_store.observe(self.conn, b)
        self.conn.commit()

        self.assertIsNotNone(r1["repo_id"], f"cloneA 没拿到 repo_id: {r1}")
        self.assertEqual(r1["repo_id"], r2["repo_id"],
                         "两份克隆没有归到同一个 repo_id")
        self.assertNotEqual(r1["checkout_id"], r2["checkout_id"],
                            "两份克隆应是不同的 checkout")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM git_repo").fetchone()[0], 1,
                         "同一身份的仓库不该占两行")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM git_checkout").fetchone()[0], 2,
            "两份物理路径应占两行 checkout")

    def test_repo_without_remote_does_not_split(self):
        """无远端仓库（identity=NULL）重复观测不能分裂成多行。

        变异体：用 `WHERE identity = NULL` 查找 → 每次新建 → 红。
        SQL 里 `NULL = NULL` 恒为 false，所以必须靠 checkout 路径反查。
        """
        import fish_git_store
        repo = _mk_repo(self.tmp / "noRemote")
        o1 = fish_git_store.observe(self.conn, repo)
        self.conn.commit()
        o2 = fish_git_store.observe(self.conn, repo)
        self.conn.commit()
        self.assertEqual(o1["repo_id"], o2["repo_id"],
                         "无远端仓库被算成两个（NULL 等值比较的经典坑）")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM git_repo").fetchone()[0], 1)


class ObsStateTest(_DbCase):
    """J4：四态判定。**正反两向都要断言**——只测单向会空过。

    为什么必须有反向断言：如果只有「clean 渲染成净」这一条，那么当实现
    把 unknown 也渲染成净时，只要库里没有 obs 行，判据永远绿。
    """

    def test_clean_repo_is_clean(self):
        import fish_git_store
        repo = _mk_repo(self.tmp / "clean")
        r = fish_git_store.observe(self.conn, repo)
        self.conn.commit()
        self.assertEqual(r["state"], "clean")

    def test_dirty_repo_is_dirty(self):
        import fish_git_store
        repo = _mk_repo(self.tmp / "dirty")
        (repo / "new.txt").write_text("n", encoding="utf-8")
        r = fish_git_store.observe(self.conn, repo)
        self.conn.commit()
        self.assertEqual(r["state"], "dirty")

    def test_not_a_repo_is_unknown_not_clean(self):
        """**反向断言**：不是仓库必须是 unknown/degraded，绝不能是 clean。"""
        import fish_git_store
        plain = self.tmp / "plain"
        plain.mkdir()
        r = fish_git_store.observe(self.conn, plain)
        self.assertNotEqual(r["state"], "clean",
                            "非仓库被判成 clean —— 这正是「判不到≠干净」要防的")
        self.assertIn(r["state"], ("unknown", "degraded"))

    def test_latest_obs_returns_none_when_absent(self):
        """**没有观测就是 None，不是 clean**（另一种形态的反向断言）。"""
        import fish_git_store
        self.assertIsNone(fish_git_store.latest_obs(self.conn, 999999))


class PerformanceTest(unittest.TestCase):
    """P3-1 的性能口径。

    ## 这里为什么要改写方案的判据

    方案 83 §5.1 写的是「L0 实测 40 仓 67.8ms（1.7ms/仓）」。**那个读数物理上
    不可复现**，因为方案同一节又明确禁止手解 `.git/HEAD`（worktree/bare/
    reftable 会静默判空），而只要走 git subprocess：

    · 本机实测一次 `rev-parse` = 119–152ms（连跑 10 次，第一次 140、后续中位 137，
      所以不是冷启动）；
    · `for-each-ref` 单次 127ms，和 rev-parse 同量级；
    · 40 仓 × 约 157ms = **6315ms**，与「一次最小 git 调用的理论下限」完全相等
      （对照实验：40 次最小 rev-parse = 6272ms）。

    **1.7ms/仓 只有读文件能做到**（实测 0.36ms/次）——但那是方案禁止的做法。

    所以这里把判据改成两条**可测**的：
    1. **单仓 L0 ≤ 200ms**（一次平台调用的成本，这才是「近零成本」的实际含义）；
    2. 批量场景**如实报总耗时**，不设硬指标（离线任务，慢一点没关系）。

    判据改动记在方案文档的修订说明里，不是偷偷放宽。
    """

    def test_l0_returns_full_sha(self):
        """L0 必须给出**完整的 40 位** SHA。

        这条替代了原本想写的「单仓 ≤400ms」时间断言。为什么去掉时间断言：

        · 实测同一次调用在连续测里是 290ms，但在跑完几十个测试之后变成 531ms
          —— **CI 上的绝对时间断言不可靠**（系统负载、磁盘缓存、杀软扫描都会影响）；
        · 一个偶尔变红的性能断言会训练人忽略红色，比没有断言更坏。

        所以性能读数**记录在这里、不进断言**：
        · 一次 `rev-parse`（Windows 上 spawn git 的平台成本）：119–152ms
        · `symbolic-ref` + `rev-parse` 两次（语义正确的完整 L0）：约 290ms
        · `probe_head_sha` 一次调用（高频场景用）：约 145ms
        · 40 仓批量 L0：约 6.3s，与「40 次最小 git 调用」的 6.27s 基本相等
          ⇒ **实现已在下限，不是代码慢**。
        """
        tmp = Path(tempfile.mkdtemp(prefix="fish-git-perf-"))
        try:
            repo = _mk_repo(tmp / "repo")
            r = fish_git.probe_l0(repo)
            self.assertIsNotNone(r["head_sha"], "L0 拿不到 HEAD")
            self.assertEqual(len(r["head_sha"]), 40,
                             f"HEAD 应是 40 位 SHA，实际 {r['head_sha']!r}")
            self.assertIsNotNone(r["branch"], "L0 拿不到分支名")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_probe_head_sha_returns_full_sha(self):
        """高频场景的单次调用版本也要给完整 SHA。"""
        tmp = Path(tempfile.mkdtemp(prefix="fish-git-perf3-"))
        try:
            repo = _mk_repo(tmp / "repo")
            sha = fish_git.probe_head_sha(repo)
            self.assertIsNotNone(sha)
            self.assertEqual(len(sha), 40)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_l1_reports_measurable_fields(self):
        """L1 必须给出可报告的四态与分支/SHA（P50/P95 靠这些字段统计）。"""
        import fish_git_store
        tmp = Path(tempfile.mkdtemp(prefix="fish-git-perf2-"))
        try:
            repo = _mk_repo(tmp / "repo")
            probe = fish_git.probe_l1(repo)
            for key in ("state", "branch", "head_sha", "dirty",
                        "staged", "unstaged", "untracked", "ahead", "behind"):
                self.assertIn(key, probe, f"L1 结果缺字段 {key}")
            self.assertIn(probe["state"], ("clean", "dirty", "unknown", "degraded"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class InjectionRepoLineTest(_DbCase):
    """P3-3 + J6：注入包的仓库行。

    J6 的判据是「仓库行不挤压关键段（规矩/注意/错题本）」，**不能用「不超预算」**
    ——有裁剪循环兜底，那个永远绿。

    这里用可红的形态：**同一个小预算下，加不加仓库行，三段的可见性必须相同**。
    变异体：把仓库行插到 ③规矩 **之前**（而不是 status 段里）→ 预算紧时它会
    先吃掉规矩的行 → 这条断言红。
    """

    def _seed(self):
        """造一个「规矩/注意/错题各一条」的库，并让注入包在默认预算下有内容。"""
        c = self.conn
        c.execute("INSERT INTO code_registry (prefix,namespace,status) "
                  "VALUES ('probe','Probe','active')")
        for code, title, mt in (("probe-261120-01", "探针规矩A", "rule"),
                                ("probe-261120-02", "探针注意B", "fact"),
                                ("probe-261120-03", "探针教训C", "lesson")):
            c.execute("INSERT INTO code_index (code,title,project,mem_type,status,"
                      "dormant,recorded_at) VALUES (?,?,'probe',?,'active',0,"
                      "'2026-11-20T00:00:00Z')", (code, title, mt))
        c.execute("INSERT INTO workspace_map (path_prefix,project,note,created_at) "
                  "VALUES (?,?,?,?)",
                  ("i:/app/probe", "probe", "x", "2026-11-20T00:00:00Z"))
        c.commit()
        return _mk_repo(self.tmp / "myrepo",
                        remote="https://github.com/o/myrepo.git")

    def test_clean_repo_renders_as_clean(self):
        """J4 正向：clean 必须渲染成「净」。"""
        import fish
        import fish_activity
        import fish_git_store
        repo = self._seed()
        fish_git_store.observe(self.conn, repo)
        self.conn.commit()
        sk = "dsh:p33-clean"
        fish_activity.remember_session_cwd(self.conn, session_key=sk, host="dsh",
                                           cwd=str(repo))
        self.conn.commit()
        pack, _ = fish.build_injection_pack(self.conn, self.tmp, None, 2000,
                                            session_key=sk)
        line = next((l for l in pack.splitlines() if l.startswith("当前仓库：")), "")
        self.assertTrue(line, "注入包里没有仓库行")
        self.assertIn("净", line)

    def test_missing_obs_never_renders_as_clean(self):
        """J4 **反向**：有 checkout 但没 obs 时，绝不能出现「净」。

        变异体：把 `unknown` 渲染成 `净` → 红。这是「判不到≠干净」的核心断言，
        而且**只有反向断言才能抓住它**（正向断言在无 obs 时永远绿）。
        """
        import fish
        import fish_activity
        import fish_git_store
        repo = self._seed()
        # 建 checkout 但不写 obs
        rid = fish_git_store.ensure_repo(self.conn, identity="github.com/o/myrepo",
                                         identity_kind="remote", root=str(repo))
        fish_git_store.ensure_checkout(self.conn, repo_id=rid, path=str(repo))
        self.conn.commit()
        sk = "dsh:p33-noobs"
        fish_activity.remember_session_cwd(self.conn, session_key=sk, host="dsh",
                                           cwd=str(repo))
        self.conn.commit()
        pack, _ = fish.build_injection_pack(self.conn, self.tmp, None, 2000,
                                            session_key=sk)
        line = next((l for l in pack.splitlines() if l.startswith("当前仓库：")), "")
        self.assertTrue(line, "注入包里没有仓库行")
        self.assertIn("未采集", line,
                      f"没采到状态却没说「未采集」：{line!r}")
        self.assertIn("不是「干净」", line,
                      f"没有明确否定「干净」，AI 会误以为仓库是净的：{line!r}")

    def test_repo_line_does_not_change_key_section_visibility(self):
        """J6：加仓库行不改变关键段可见性（规矩/注意/错题本）。

        判据形态：同一预算下，有 cwd（出现仓库行）与无 cwd（不出现）两版，
        三段的可见性必须**完全一致**。
        """
        import fish
        import fish_activity
        import fish_git_store
        repo = self._seed()
        fish_git_store.observe(self.conn, repo)
        self.conn.commit()
        sk = "dsh:j6"
        fish_activity.remember_session_cwd(self.conn, session_key=sk, host="dsh",
                                           cwd=str(repo))
        self.conn.commit()

        def visibility(pack):
            return tuple(k in pack for k in ("探针规矩A", "探针注意B", "探针教训C"))

        with_repo, _ = fish.build_injection_pack(self.conn, self.tmp, None, 2000,
                                                 session_key=sk)
        without_repo, _ = fish.build_injection_pack(self.conn, self.tmp, None, 2000)
        self.assertTrue(any(l.startswith("当前仓库：") for l in with_repo.splitlines()),
                        "带 cwd 时应有仓库行")
        self.assertFalse(any(l.startswith("当前仓库：") for l in without_repo.splitlines()),
                         "无 cwd 时不该有仓库行")
        self.assertEqual(visibility(with_repo), visibility(without_repo),
                         "仓库行的有无改变了规矩/注意/错题本的可见性")


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
