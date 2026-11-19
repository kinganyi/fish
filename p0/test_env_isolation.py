#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P1-8 判据：测试读数必须与宿主环境无关。

## 审查方实测的问题（已复核成立）

`fish.py` 的宿主探测按优先级读 `DSH_SESSION_ID` / `CODEBUDDY_SESSION_ID` /
`CLAUDE_SESSION_ID`。这些在宿主自己的 shell 里**是有值的**，测试子进程会原样继承。

实测复现（本机）：

    $ python -m pytest test_session_bind.py           → 9 passed
    $ CLAUDE_SESSION_ID=fake python -m pytest …       → 2 failed

同一份代码、同一台机器，**只因为环境不同就给出两种读数**。这比"测试红了"更糟：
它让人无法判断红的是代码还是环境。审查方看到的"公开仓 5 failed 而非 2"正是这个。

## 修法

`conftest.py` 在**会话开始**清掉宿主会话类 env，结束后还原。
不在每个测试里清：同一件事写 128 份必有漏，而且"依赖宿主没设某个 env"是隐性前提。
也不在"每个测试前"清：有些判据**故意** `monkeypatch.setenv` 来验探测逻辑，
每测试前清会把它们的设置也擦掉——那是换个方式破坏判据。

## 这批判据为什么能失败

**第二条**跑真子进程：把污染 env 注进去，然后断言那组判据仍然全绿。
如果有人删掉 `conftest.py`，或把清理范围改窄到漏掉 `CLAUDE_SESSION_ID`，它就红。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class PollutingPredicateTest(unittest.TestCase):
    """`_is_polluting` 的口径：只清**会改变 fish 判定**的变量。"""

    def setUp(self):
        import importlib
        import conftest
        # **不许裸 reload**（第九轮审查 🔴，2026-11-20）：
        # `conftest` 的模块级 dict `_DATA_GUARD` 存着"生产数据护栏指向哪个目录"，
        # `importlib.reload` 会把它重置成 `{"dir": None, ...}` ——护栏从此**不再拦**
        # `p0/data` 下的删除，而 `pytest_unconfigure` 里那句"文件数变少"的检查
        # 也会因为 `dir is None` 提前 return。
        #
        # 实测后果：`pytest --collect-only` 的顺序里 `test_data_guard` 排在
        # `test_env_isolation` **之前**，所以全量跑中这个类之后的**上千条判据
        # 全部处于无护栏状态**——正是 INC-20261119-01 那个洞又开了。
        #
        # 这里要的只是 `_is_polluting`（纯函数，不依赖模块状态），
        # 所以 reload 之前把状态存下来、之后原样放回去。
        saved = {k: conftest._DATA_GUARD.get(k) for k in ("dir", "count")}
        saved_snap = dict(conftest._SNAPSHOT)
        self.c = importlib.reload(conftest)
        conftest._DATA_GUARD.update(saved)
        conftest._SNAPSHOT.update(saved_snap)

    def test_session_id_envs_are_polluting(self):
        for name in ("DSH_SESSION_ID", "CODEBUDDY_SESSION_ID",
                     "CLAUDE_SESSION_ID", "CODEX_SESSION_ID"):
            self.assertTrue(self.c._is_polluting(name), f"{name} 没被隔离")

    def test_alias_and_credential_envs_are_polluting(self):
        for name in ("FISH_ACTOR", "FISH_SESSION_ACTOR", "FISH_SESSION_TOKEN"):
            self.assertTrue(self.c._is_polluting(name), f"{name} 没被隔离")

    def test_install_paths_are_not_polluting(self):
        """**反向判据**：`CODEX_HOME` 是安装路径，不是会话标识。

        清掉它等于"换了个环境"，不是"隔离污染"——判据是"会不会改变 fish 的判定"，
        不是"看起来像不像宿主变量"。实测本机就有这两个。
        """
        for name in ("CODEX_HOME", "CODEX_INSTALL_DIR", "PATH", "TEMP"):
            self.assertFalse(self.c._is_polluting(name), f"{name} 不该被清")


class ReproducibleUnderPollutionTest(unittest.TestCase):
    """**核心判据**：注入污染 env 后，那组判据必须仍然全绿。"""

    POLLUTION = {
        "CLAUDE_SESSION_ID": "fake-claude-session",
        "CLAUDE_CODE": "1",
        "CODEBUDDY_SESSION_ID": "fake-codebuddy",
        "FISH_ACTOR": "污染者",
    }

    def test_session_bind_tests_pass_under_polluted_env(self):
        env = dict(os.environ)
        env.update(self.POLLUTION)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "test_session_bind.py",
             "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=str(HERE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=600)
        tail = (r.stdout or "")[-800:]
        self.assertEqual(r.returncode, 0,
                         f"带污染 env 时判据红了——读数与宿主绑定：\n{tail}")
        self.assertIn("conftest", r.stdout or "",
                      "conftest 没有报告隔离动作（可能根本没加载）")

    def test_clean_shell_also_passes(self):
        """对照组：干净 shell 也必须绿。

        只测"污染下变绿"而不测"干净时仍绿"，可能把判据改成永远绿。
        """
        env = {k: v for k, v in os.environ.items()}
        for k in ("CLAUDE_SESSION_ID", "CODEBUDDY_SESSION_ID", "FISH_ACTOR"):
            env.pop(k, None)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "test_session_bind.py",
             "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=str(HERE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=600)
        self.assertEqual(r.returncode, 0,
                         f"干净 shell 下判据红了：\n{(r.stdout or '')[-600:]}")


    def test_conftest_hooks_are_defined_once(self):
        """**pytest 只认最后定义的那个钩子**——重复定义等于前一个被静默顶掉。

        ## 这条判据的来历（2026-11-19）

        我加"生产数据护栏"时往 conftest 里新写了一个 `pytest_configure`，
        于是先前的**环境隔离钩子被顶掉了**：不报错、不警告，
        只是 `CLAUDE_SESSION_ID` 不再被清，S-1/S-5 在宿主 shell 里假红。

        抓到它的不是"新功能有没有加"，而是**"旧承诺还在不在"**那句断言。
        所以这里补一条直指病根的：钩子函数**不许重名**。
        """
        import re as _re
        src = (HERE / "conftest.py").read_text(encoding="utf-8")
        for hook in ("pytest_configure", "pytest_unconfigure"):
            n = len(_re.findall(rf"^def {hook}\(", src, _re.M))
            self.assertEqual(
                n, 1,
                f"conftest 里 `{hook}` 定义了 {n} 次——pytest 只认最后一个，"
                f"前面的会被**静默顶掉**（2026-11-19 环境隔离就是这么丢的）")

    def test_conftest_has_both_responsibilities(self):
        """合并之后的那个钩子必须**两件事都还在做**。

        只验"没有重复"不够：把环境隔离整段删掉也不重复。
        所以要同时验两件事各自的**实质**都还在。"""
        src = (HERE / "conftest.py").read_text(encoding="utf-8")
        self.assertIn("_is_polluting", src,
                      "环境隔离的逻辑不在了")
        self.assertIn("guarded_rmtree", src,
                      "生产数据护栏的逻辑不在了")
        # 两者必须在**同一个** configure 里
        import re as _re
        m = _re.search(r"^def pytest_configure\(config\):(.*?)(?=^def )", src,
                       _re.M | _re.S)
        self.assertIsNotNone(m, "找不到 pytest_configure")
        body = m.group(1)
        self.assertIn("_is_polluting", body, "configure 里没有环境隔离")
        self.assertIn("guarded_rmtree", body, "configure 里没有数据护栏")


    def test_reload_does_not_disable_the_guard(self):
        """**运行时判据**：reload 之后护栏必须还指向真目录（第九轮审查 🔴）。

        源码字符串判据（`test_conftest_has_both_responsibilities`）抓不到这个——
        它只看"源码里有没有 guarded_rmtree"，而 `_DATA_GUARD["dir"]` 被 reload
        重置成 `None` 是**运行期**的事。

        这条用的是 `setUp` 里 reload 过的那个模块，直接问它状态。
        """
        import conftest
        if not (HERE / "data").is_dir():
            # 公开包没有生产数据，`pytest_configure` 会提前返回，
            # 那时 `dir is None` 是**正确**的——判不到，不是通过。
            self.skipTest("这棵树没有 data/（公开包）——判不到，不是通过")
        self.assertIsNotNone(
            conftest._DATA_GUARD.get("dir"),
            "reload 之后 `_DATA_GUARD['dir']` 变成 None——"
            "**护栏从这一刻起不再拦 `p0/data` 下的删除**，"
            "而全量跑里这个类之后的判据全都在无护栏状态下跑")
        # 再确认护栏**真的**还在拦：拿假根当靶子，删它必须抛
        import shutil as _sh
        import tempfile as _tf
        from pathlib import Path as _P
        fake = _P(_tf.mkdtemp(prefix="fish-reload-guard-")) / "data"
        (fake / "projects").mkdir(parents=True)
        (fake / "projects" / "x.md").write_text("x", encoding="utf-8")
        real = conftest._DATA_GUARD["dir"]
        conftest._DATA_GUARD["dir"] = fake
        try:
            with self.assertRaises(AssertionError):
                _sh.rmtree(fake / "projects")
            self.assertTrue((fake / "projects" / "x.md").is_file(),
                            "抛了错但文件还是被删了")
        finally:
            conftest._DATA_GUARD["dir"] = real
            _sh.rmtree(fake.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
