#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统级修复 · 第三批 D-1：git 采集必须在共享核心里（2026-11-21）。

## 这份测试守的是什么

第一版把 git 采集写在 `fish_push_hook.py` —— **DSH 专属脚本**。
症状（外部 AI 反馈报告的原话）：

> 同一台机器上 DSH 有 git 采集、WorkBuddy 没有；真提交完成后 fish 里依然
> 「没有这个仓库的记录」。

`is_git_commit()` 当时**已经**在共享核心 `fish_activity`，而**用它的代码不在**。

## 判据的形态：跑真进程，不调函数

这个 bug 的形态决定了：**直接调 `collect_git` 是测不出来的**——
我自己在调试时就吃了一次亏（函数调用成功、返回 `observed: clean`，
但数据一条都没落库）。所以这里的判据一律**跑真实 hook 脚本**。

## 变异体（去掉 → 必红）

· `collect_git` 里去掉 `conn.commit()` → 全部判据红
  （`fish_git_store` 一个 commit 都没有，它约定由调用方控制事务边界）
· 把调用从 `handle_post_tool_use` 挪回 `fish_push_hook` → WorkBuddy 判据红
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402


class _HookCase(unittest.TestCase):
    """准备一个隔离的库 + 一个真 git 仓库，然后跑真实 hook 进程。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-d1-"))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.close()

        self.repo = self.tmp / "r"
        self.repo.mkdir()

        def g(*a):
            subprocess.run(["git"] + list(a), cwd=str(self.repo), check=True,
                           capture_output=True)
        g("init", "-q")
        g("config", "user.email", "t@e.com")
        g("config", "user.name", "t")
        (self.repo / "a.txt").write_text("1", encoding="utf-8")
        g("add", "a.txt")
        g("commit", "-q", "-m", "x")
        g("remote", "add", "origin", "https://github.com/o/r.git")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _counts(self):
        conn = fish.connect(self.data)
        try:
            return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                    for t in ("git_repo", "git_checkout", "git_obs", "git_commit")}
        finally:
            conn.close()

    def _run_hook(self, script, payload, host="dsh"):
        env = {"FISH_DATA": str(self.data), "PYTHONIOENCODING": "utf-8",
               "PATH": __import__("os").environ.get("PATH", "")}
        r = subprocess.run(
            [sys.executable, str(P0 / script), "--host", host],
            input=json.dumps(payload), capture_output=True, text=True,
            env=env, timeout=120, encoding="utf-8", errors="replace")
        return r

    def _write_payload(self, session_id, path):
        return {"session_id": session_id, "cwd": str(self.repo),
                "tool_name": "write", "tool_input": {"file_path": str(path)},
                "tool_response": {"ok": True}}


class WorkBuddyHookTest(_HookCase):
    """**修复的重点**：WorkBuddy 走的 `fish_posttool_hook.py` 现在也会采集。

    变异体：把 git 采集挪回 `fish_push_hook.py` → 本类全红。
    """

    def test_workbuddy_write_triggers_observe(self):
        """写类动作 → git_obs 增加。**修复前这里恒为 0。**"""
        r = self._run_hook("fish_posttool_hook.py",
                           self._write_payload("wb1", self.repo / "b.txt"))
        self.assertEqual(r.returncode, 0, f"hook 退出码非 0：{r.stderr[:300]}")
        c = self._counts()
        self.assertEqual(c["git_repo"], 1, f"WorkBuddy 侧没有登记仓库：{c}")
        self.assertGreaterEqual(c["git_obs"], 1, f"WorkBuddy 侧没有 L1 观测：{c}")

    def test_workbuddy_commit_is_collected(self):
        """识别到 `git commit` → git_commit 增加。

        这是归因链唯一的确定性绑定，也是报告里"提交了却没记录"的直接症状。
        """
        (self.repo / "c.txt").write_text("2", encoding="utf-8")
        subprocess.run(["git", "add", "c.txt"], cwd=str(self.repo), check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "y"], cwd=str(self.repo),
                       check=True, capture_output=True)
        r = self._run_hook("fish_posttool_hook.py",
                           {"session_id": "wb1", "cwd": str(self.repo),
                            "tool_name": "bash",
                            "tool_input": {"command": "git commit -m y"},
                            "tool_response": {"ok": True}})
        self.assertEqual(r.returncode, 0, f"hook 退出码非 0：{r.stderr[:300]}")
        c = self._counts()
        self.assertEqual(c["git_commit"], 1, f"commit 没有被采集：{c}")


class DshHookStillWorksTest(_HookCase):
    """反向断言：下沉不能把 DSH 侧弄坏（原来那条路必须继续通）。"""

    def test_dsh_write_triggers_observe(self):
        r = self._run_hook("fish_push_hook.py",
                           self._write_payload("dsh1", self.repo / "b.txt"))
        self.assertEqual(r.returncode, 0, f"hook 退出码非 0：{r.stderr[:300]}")
        c = self._counts()
        self.assertGreaterEqual(c["git_obs"], 1, f"DSH 侧没有观测：{c}")


class SharedCoreTest(unittest.TestCase):
    """**架构判据**：git 采集的实现在共享核心，不在宿主专属脚本里。

    这一条是"防复发"的关键——光把代码搬过去，下次还会有人往宿主脚本里写业务逻辑。
    """

    HOST_SCRIPTS = ("fish_push_hook.py", "fish_posttool_hook.py")

    def test_host_scripts_do_not_import_git_layer(self):
        """两个宿主脚本都**不许**直接 import git 采集层。

        变异体：在 `fish_posttool_hook.py` 里加 `import fish_git_store` → 红。
        """
        for name in self.HOST_SCRIPTS:
            src = (P0 / name).read_text(encoding="utf-8")
            for banned in ("import fish_git_store", "import fish_git\n",
                           "from fish_git_store import"):
                self.assertNotIn(
                    banned, src,
                    f"{name} 直接引了 git 采集层（{banned!r}）——"
                    f"共享逻辑只能写在共享核心里，宿主脚本只做触发")

    def test_git_collect_lives_in_shared_core(self):
        """采集入口必须在 `fish_git_hook`，且被 `handle_post_tool_use` 调用。"""
        import inspect
        import fish_activity
        import fish_git_hook
        self.assertTrue(hasattr(fish_git_hook, "collect_git"))
        src = inspect.getsource(fish_activity.handle_post_tool_use)
        self.assertIn("fish_git_hook", src,
                      "handle_post_tool_use 没有调共享的 git 采集——"
                      "那意味着某个宿主会采不到")

    def test_collect_git_commits(self):
        """**`collect_git` 必须自己 commit。**

        这条判据来自一次真实事故：`fish_git_store` 里一个 commit 都没有
        （它约定由调用方控制事务边界），而我在下沉时把原实现里那行 `conn.commit()`
        漏了。后果极坏——**调用返回 `observed: 'clean'`（看起来成功），
        `conn.close()` 时全部回滚，库表里一条都没有。**

        变异体：删掉 `conn.commit()` → 红。
        """
        import inspect
        import fish_git_hook
        src = inspect.getsource(fish_git_hook.collect_git)
        self.assertIn("conn.commit()", src,
                      "collect_git 没有 commit —— fish_git_store 不提交，"
                      "数据会在 conn.close() 时静默回滚")


if __name__ == "__main__":
    unittest.main()
