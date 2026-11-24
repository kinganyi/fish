#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""112 · W-A（纯冗余收口）的判据：B3 / B4 / B5 + N-6。

每条判据都配了一个**真源码变异**（`mutation_112_reduction.py`），先红后绿才算数：

| 判据 | 变异（打在语义开关上，不是注释） |
|---|---|
| A1 四份 `_host_from_argv` / 四份 `_dbg` 已收口 | 把 `def _host_from_argv` 复制回任一 hook → A1 红 |
| A2 四个 hook 的缺省 host 是**历史值**，且调用点显式传参 | posttool 的默认值改 `dsh` → A2 红 |
| A3 `_presence_has_session` 只有一份、activity 委托它 | 在 activity 里再写一份 → A3 红 |
| A4 小状态/快照写统一走 `fish_atomic` | 改回裸 `write_text` → A4 红 |

## A2 为什么钉「历史值」而不是「四个都一样」

112 §七自评点名的假绿就在这一条：把四个默认值统一成 `dsh`，再用"四个都等于 dsh"
当断言，它会**全绿**——而那恰恰是错误方向（`fish_posttool_hook` 的 `workbuddy`
是 WorkBuddy 宿主 PostToolUse 的历史口径）。所以断言写成**逐 hook 的显式清单**，
并额外钉住"默认值由调用点传入"，不许 hook_io 里再留一个隐式缺省。

## 扫描范围（这条要写清，否则判据会自欺）

A1/A3 扫的是 `p0/` 顶层的**生产模块**（排除 `test_*.py` 与 `mutation_*.py`：
变异脚本的靶点片段里必然出现 `def _host_from_argv` 这类字符串，把它们算进来
等于让判据永远红）。`p0/tools/` 是验证脚本，不是随产品分发的模块，也不在扫描面内。
"""
import importlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish_atomic  # noqa: E402
import fish_common  # noqa: E402

HOOKS = ("fish_posttool_hook", "fish_pretool_hook",
         "fish_prompt_hook", "fish_push_hook")

#: 历史缺省值（112 §三 B3 实测：只有 posttool 是 workbuddy，其余三个是 dsh）。
#: 四个 hook 的宿主命令行**都显式传 `--host`**，所以这是兜底口径——但兜底也必须
#: 保历史值：同一事件（PostToolUse）两个 hook 在无 `--host` 时记成两个宿主身份，
#: 会话键与在场记录会分叉。
HISTORICAL_DEFAULTS = {
    "fish_posttool_hook": "workbuddy",
    "fish_pretool_hook": "dsh",
    "fish_prompt_hook": "dsh",
    "fish_push_hook": "dsh",
}


def _production_modules():
    """`p0/` 顶层的生产模块（测试与变异脚本除外）。"""
    for path in sorted(P0.glob("*.py")):
        if path.name.startswith("test_") or path.name.startswith("mutation_"):
            continue
        yield path


def _count_defs(name: str) -> list:
    """`def <name>(` 出现在哪些生产模块里（返回 `文件名:行号` 清单）。"""
    pattern = re.compile(r"^\s*def\s+%s\s*\(" % re.escape(name), re.M)
    hits = []
    for path in _production_modules():
        src = path.read_text(encoding="utf-8")
        for match in pattern.finditer(src):
            hits.append("%s:%d" % (path.name, src.count("\n", 0, match.start()) + 1))
    return hits


def _os_replace_calls(path: Path) -> list:
    """AST 找**真正的** `os.replace(...)` 调用行号（docstring 里提到不算）。"""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "replace":
            continue
        owner = func.value
        if (isinstance(owner, ast.Attribute) and owner.attr == "path"
                and isinstance(owner.value, ast.Name) and owner.value.id == "os"):
            hits.append(node.lineno)
    return hits


class HostAndDbgConsolidatedTest(unittest.TestCase):
    """A1 + B4：四份 `_host_from_argv` / 四份 `_dbg` 的副本必须一份都不剩。"""

    def test_no_local_host_from_argv_anywhere(self):
        hits = _count_defs("_host_from_argv")
        self.assertEqual(hits, [], "`_host_from_argv` 还有本地实现（应在 hook_io 里唯一）：%s" % hits)

    def test_no_local_dbg_in_the_four_hooks(self):
        hits = _count_defs("_dbg")
        hook_hits = [h for h in hits if h.split(":")[0] in
                     {name + ".py" for name in HOOKS}]
        self.assertEqual(hook_hits, [], "四个 hook 里还有 `def _dbg`（应在 hook_io.dbg）：%s"
                         % hook_hits)

    def test_dbg_is_a_thin_alias_pinned_to_each_hook_tag(self):
        """四个 hook 只剩"tag 不同"这一件事；必须委托到同一个实现。

        行为断言：`FISH_HOOK_DEBUG=1` 时打 `[<hook 名>] <msg>`，未开时静默。
        tag 是四个 hook 之间**唯一**的差异，也是合并时唯一需要保留的东西。
        """
        for name in HOOKS:
            mod = importlib.import_module(name)
            self.assertFalse(hasattr(mod, "_host_from_argv"),
                             "%s 仍带本地 _host_from_argv" % name)
            os.environ["FISH_HOOK_DEBUG"] = "1"
            try:
                out = self._capture_stderr(lambda: mod._dbg("probe"))
            finally:
                os.environ.pop("FISH_HOOK_DEBUG", None)
            self.assertIn("[%s] probe" % name, out,
                          "%s 的诊断前缀变了（等价性破坏）：%r" % (name, out))
            quiet = self._capture_stderr(lambda: mod._dbg("probe"))
            self.assertEqual(quiet, "", "%s 默认必须静默" % name)

    @staticmethod
    def _capture_stderr(fn) -> str:
        buf = io.StringIO()
        old = sys.stderr
        sys.stderr = buf
        try:
            fn()
        finally:
            sys.stderr = old
        return buf.getvalue()


class HostFromArgvBehaviourTest(unittest.TestCase):
    """A2：合并后的解析语义必须与四份旧实现逐条等价。"""

    def test_parsing_semantics(self):
        from hook_io import host_from_argv
        self.assertEqual(host_from_argv([], "dsh"), "dsh", "无 --host 时用调用点给的默认值")
        self.assertEqual(host_from_argv(["--host", "workbuddy"], "dsh"), "workbuddy")
        self.assertEqual(host_from_argv(["--host=DSH"], "workbuddy"), "dsh",
                         "`--host=` 形态要小写；这条四种旧实现都这么写")
        self.assertEqual(host_from_argv(["--host", ""], "workbuddy"), "workbuddy",
                         "空值必须退回默认值（旧实现用的是 `or 默认值`）")
        self.assertEqual(host_from_argv(["--host"], "dsh"), "dsh",
                         "`--host` 后面没值 → 默认值（旧实现 fallthrough 到末尾）")
        self.assertEqual(host_from_argv(["--other", "x"], "dsh"), "dsh")
        self.assertEqual(host_from_argv(["--host=dsh", "--host", "workbuddy"], "x"),
                         "dsh", "先出现的 `--host` 胜（旧实现的遍历顺序）")

    def test_each_hook_passes_its_historical_default_explicitly(self):
        """逐 hook 钉历史默认值 + 钉"默认值在调用点显式传"（112 §七的防假绿）。"""
        for name, want in HISTORICAL_DEFAULTS.items():
            mod = importlib.import_module(name)
            self.assertEqual(getattr(mod, "DEFAULT_HOST", None), want,
                             "%s 的缺省宿主不是历史值 %r（112 §三 B3）" % (name, want))
            src = (P0 / (name + ".py")).read_text(encoding="utf-8")
            self.assertIn("hook_io.host_from_argv(argv, DEFAULT_HOST)", src,
                          "%s 没在调用点显式传默认值——隐式缺省会让行为分叉看不见" % name)


class PresenceProbeConsolidatedTest(unittest.TestCase):
    """A3（B5）：列探测只有一份，`fish_activity` 委托 `fish_common`。"""

    def test_only_one_definition(self):
        hits = _count_defs("_presence_has_session")
        where = [hit.split(":")[0] for hit in hits]
        self.assertEqual(where, ["fish_common.py"],
                         "`_presence_has_session` 应只在 fish_common 里有一份：%s" % hits)

    def test_activity_delegates_to_the_single_copy(self):
        import fish_activity
        self.assertIs(fish_activity._presence_has_session,
                      fish_common._presence_has_session,
                      "fish_activity 自己又留了一份探测（应委托 fish_common）")

    def test_delegation_still_answers_both_schema_shapes(self):
        """行为等价：有 `session_key` 列 → True；两列老形态 → False；无表 → False。"""
        import fish_activity
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE presence (board_id INTEGER, citizen_id TEXT)")
            self.assertFalse(fish_activity._presence_has_session(conn),
                             "老库（无 session_key 列）必须判 False")
            conn.execute("ALTER TABLE presence ADD COLUMN session_key TEXT")
            self.assertTrue(fish_activity._presence_has_session(conn),
                            "新库必须判 True")
            conn.execute("DROP TABLE presence")
            self.assertFalse(fish_activity._presence_has_session(conn),
                             "无表 → False（不许抛）")
        finally:
            conn.close()


class AtomicStateWriteTest(unittest.TestCase):
    """A4（N-6）：小状态/快照写统一走 `fish_atomic.write_text_atomic`。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fish-wa-atomic-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_activity_state_write_uses_fish_atomic(self):
        import fish_activity
        with mock.patch.object(fish_atomic, "write_text_atomic",
                               wraps=fish_atomic.write_text_atomic) as spy:
            ok = fish_activity.set_warn_pending(
                self.root, session_key="dsh:wa", reason="探针",
                now="2026-11-24T00:00:00Z")
        self.assertTrue(ok)
        self.assertEqual(spy.call_count, 1,
                         "activity 的状态文件没走 fish_atomic（手搓 tmp+replace 又有了一份）")

    def test_fastpath_state_write_uses_fish_atomic(self):
        import hook_fastpath
        with mock.patch.object(fish_atomic, "write_text_atomic",
                               wraps=fish_atomic.write_text_atomic) as spy:
            hook_fastpath.record_attempt(self.root, "prompt", "timeout")
        self.assertEqual(spy.call_count, 1,
                         "hook_fastpath 的冷却状态仍是裸写（写到一半被杀会让计数归零）")

    def test_prompt_gate_state_write_uses_fish_atomic(self):
        import fish_prompt_hook
        with mock.patch.object(fish_atomic, "write_text_atomic",
                               wraps=fish_atomic.write_text_atomic) as spy:
            res = fish_prompt_hook.prompt_gate(
                "sess-wa", "一条足够长的提示词", data_dir=self.root)
        self.assertTrue(res.get("pass"), res)
        self.assertEqual(spy.call_count, 1,
                         "prompt 闸的状态文件还是手搓 tmp+replace")

    def test_snapshot_write_uses_fish_atomic(self):
        import fish
        import fish_activity
        conn = fish.connect(self.root / "data")
        self.addCleanup(conn.close)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        out = self.root / "data" / "_audit" / "hooks" / "active_surfaces.json"
        with mock.patch.object(fish_atomic, "write_text_atomic",
                               wraps=fish_atomic.write_text_atomic) as spy:
            fish_activity.build_snapshot(conn, out_path=out)
        self.assertEqual(spy.call_count, 1, "快照写没走 fish_atomic")
        self.assertTrue(out.is_file(), "快照没有落盘")

    def test_no_hand_rolled_atomic_write_left_in_these_modules(self):
        """结构面（AST，不看注释）：这三个模块里**不得再调用** `os.replace`。"""
        for name in ("fish_activity.py", "hook_fastpath.py", "fish_prompt_hook.py"):
            lines = _os_replace_calls(P0 / name)
            self.assertEqual(lines, [], "%s 里还有手搓 tmp+replace：第 %s 行"
                             % (name, lines))

    def test_activity_write_failure_still_returns_false(self):
        """失败语义不变：activity 侧返回 False（调用方据此不递提醒）。"""
        import fish_activity
        with mock.patch.object(fish_atomic, "write_text_atomic",
                               side_effect=OSError("boom")):
            self.assertFalse(fish_activity.set_warn_pending(
                self.root, session_key="dsh:wa", reason="探针",
                now="2026-11-24T00:00:00Z"),
                "写盘失败必须返回 False（否则同一句提醒会被反复递）")

    def test_fastpath_write_failure_still_silent(self):
        """失败语义不变：fastpath 侧吞掉 + `warn_ignored`，不改变返回值。"""
        import hook_fastpath
        with mock.patch.object(fish_atomic, "write_text_atomic",
                               side_effect=OSError("boom")):
            left = hook_fastpath.record_attempt(self.root, "prompt", "timeout")
        self.assertEqual(left, 0, "写盘失败不该抛，也不该假装进入冷却")


class HookDryRunTest(unittest.TestCase):
    """合并后的干跑行为不变：空 stdin → exit 0、stdout 空、只写 FISH_DATA 隔离目录。

    这条守的是"合并把 hook 入口弄坏"的那类回归——单元判据只调到内部函数，
    真入口（`__main__`）是否还能跑起来是另一件事。
    """

    def test_dry_run_with_empty_stdin(self):
        import subprocess
        for name in HOOKS:
            with tempfile.TemporaryDirectory() as tmp:
                env = dict(os.environ, FISH_DATA=tmp, PYTHONIOENCODING="utf-8")
                env.pop("FISH_HOOK_DEBUG", None)
                proc = subprocess.run(
                    [sys.executable, name + ".py"], input="", capture_output=True,
                    text=True, encoding="utf-8", errors="replace", env=env,
                    timeout=180, cwd=str(P0))
                self.assertEqual(proc.returncode, 0,
                                 "%s 干跑失败：%s" % (name, (proc.stderr or "")[-800:]))
                self.assertEqual(proc.stdout.strip(), "",
                                 "%s 干跑不该有 stdout 输出：%r" % (name, proc.stdout[:200]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
