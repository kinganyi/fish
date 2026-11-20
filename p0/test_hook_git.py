#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git 采集的**触发链路**（P3-1 / P4-2 · 2026-11-20）。

## 这份测试为什么必须跑真进程

上一轮我把「函数建好 + 单测通过」当成功能完成——但 `observe` / `record_commit`
在生产代码里**一个调用方都没有**，七张表永远不会自动长数据。

所以这份测试的性质不同：它**跑完整的 hook 进程**（stdin 喂 payload），
验证的是「工具调用完之后，库里会不会自己多一行」。这是「接上了」的唯一判据。

## 变异体（去掉 → 必红）

· 去掉 `collect_git` 的调用 → A/B 两段的 `git_obs` / `git_commit` 都是 0
· 去掉 `_obs_throttled` → 同一 checkout 在 60s 内会被采两次（D 段红）
· 把 `is_git_commit` 放宽成「包含 commit 子串」→ E 段（`echo git commit`）红
"""
import json
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


def _g(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _mk_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _g("init", "-q", cwd=path)
    _g("config", "user.email", "t@e.com", cwd=path)
    _g("config", "user.name", "t", cwd=path)
    (path / "a.txt").write_text("1", encoding="utf-8")
    _g("add", "a.txt", cwd=path)
    _g("commit", "-q", "-m", "first", cwd=path)
    _g("remote", "add", "origin", "https://github.com/o/myrepo.git", cwd=path)
    return path


class HookGitCollectTest(unittest.TestCase):
    """跑完整 hook 进程，验证 git 采集被真实触发。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-hookgit-"))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.close()
        self.repo = _mk_repo(self.tmp / "myrepo")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_hook(self, payload):
        env = dict(os.environ)
        env["FISH_DATA"] = str(self.data)
        env["PYTHONIOENCODING"] = "utf-8"
        env["FISH_HOOK_DEBUG"] = "1"
        return subprocess.run(
            [sys.executable, str(P0 / "fish_push_hook.py"), "--host", "dsh"],
            input=json.dumps(payload), capture_output=True, text=True,
            env=env, timeout=60)

    def _count(self, table):
        conn = fish.connect(self.data)
        try:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_write_action_triggers_observe(self):
        """写类动作走完 hook，`git_obs` 必须自己多一行（没人手工调 observe）。"""
        self.assertEqual(self._count("git_obs"), 0, "前置：库里应当还没有观测")
        r = self._run_hook({
            "session_id": "t-write", "cwd": str(self.repo), "tool_name": "write",
            "tool_input": {"file_path": str(self.repo / "b.txt")},
            "tool_response": {"ok": True}})
        self.assertEqual(r.returncode, 0, f"hook 非零退出：{r.stderr}")
        self.assertEqual(self._count("git_repo"), 1, "写动作没触发仓库发现")
        self.assertEqual(self._count("git_obs"), 1, "写动作没触发 L1 观测")

    def test_git_commit_triggers_commit_collect(self):
        """`git commit` 走完 hook，`git_commit` 表必须自己多一行 + 一条事件。

        这是归因链唯一的确定性绑定——不靠时间窗口猜。
        """
        (self.repo / "c.txt").write_text("2", encoding="utf-8")
        _g("add", "c.txt", cwd=self.repo)
        _g("commit", "-q", "-m", "second", cwd=self.repo)
        r = self._run_hook({
            "session_id": "t-commit", "cwd": str(self.repo), "tool_name": "bash",
            "tool_input": {"command": "git commit -m second"},
            "tool_response": {"ok": True}})
        self.assertEqual(r.returncode, 0, f"hook 非零退出：{r.stderr}")
        self.assertEqual(self._count("git_commit"), 1, "commit 没被自动采集")
        conn = fish.connect(self.data)
        try:
            n = conn.execute(
                "SELECT count(*) FROM events WHERE type='git.commit'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1, "commit 事件没写进 events（投影将无源可重放）")

    def test_throttle_prevents_duplicate_observe(self):
        """**J-节流**：同一 checkout 在 60s 内第二次写不该再采。

        变异体：去掉 `_obs_throttled` → 第二次也会采 → 本判据红。
        """
        for i in range(2):
            r = self._run_hook({
                "session_id": "t-throttle", "cwd": str(self.repo),
                "tool_name": "write",
                "tool_input": {"file_path": str(self.repo / f"f{i}.txt")},
                "tool_response": {"ok": True}})
            self.assertEqual(r.returncode, 0)
        self.assertEqual(self._count("git_repo"), 1)
        self.assertEqual(self._count("git_obs"), 1,
                         "60s 内被采了两次——节流没生效（每次工具调用都跑 status 会拖垮 hook）")

    def test_non_commit_bash_does_not_create_commit_row(self):
        """普通 bash 命令不该产生 commit 行（假归因比漏归因更坏）。"""
        self._run_hook({
            "session_id": "t-nc", "cwd": str(self.repo), "tool_name": "bash",
            "tool_input": {"command": "echo git commit"},
            "tool_response": {"ok": True}})
        self.assertEqual(self._count("git_commit"), 0,
                         "`echo git commit` 被误判成提交了")

    def test_hook_failure_does_not_block(self):
        """git 采集出问题也不能让 hook 非零退出（旁路纪律）。"""
        r = self._run_hook({
            "session_id": "t-bad", "cwd": "I:/definitely/not/here", "tool_name": "write",
            "tool_input": {"file_path": "I:/definitely/not/here/x.txt"},
            "tool_response": {"ok": True}})
        self.assertEqual(r.returncode, 0, f"采集失败把 hook 打挂了：{r.stderr}")


class IsGitCommitTest(unittest.TestCase):
    """`is_git_commit` 的精确性（归因链的入口，两个方向都要准）。"""

    def test_recognizes_real_commit_forms(self):
        import fish_activity
        for cmd in ("git commit -m x", "git commit --amend",
                    "git -C /x commit -m y", "git --git-dir=/x/.git commit -m z",
                    "git merge feature", "cd /x && git commit -m y"):
            self.assertTrue(fish_activity.is_git_commit(cmd), f"漏判：{cmd}")

    def test_rejects_lookalikes(self):
        import fish_activity
        for cmd in ("git status", "git checkout main", "git push",
                    "echo git commit", "git commit-tree abc", "git commitx",
                    "grep git commit log"):
            self.assertFalse(fish_activity.is_git_commit(cmd), f"误判：{cmd}")


if __name__ == "__main__":
    unittest.main()
