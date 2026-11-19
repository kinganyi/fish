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
        self.c = importlib.reload(conftest)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
