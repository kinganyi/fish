#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 4（替代方案）：归属缺口可见 + 显式补账（2026-11-22）。

## 为什么不是"自动建公民"

原设计要在 hook 侧给无身份的会话自动建公民。调研后**放弃**，因为它撞上本模块
自己写下的原则：

    ⚠️ 下面两条 SQL 是本模块**唯一**的 citizens 读点，由 `test_activity` 逐条冻结
       （设计 T02 §7）。查不到就是 None，**「不知道是谁」不等于「谁都可以」**。

从会话键推一段身份，正是这句话禁止的事；而且身份的唯一来源是凭证。
所以改成两件事：**把缺口说出来**，以及**给一个显式补账入口**（人指定才算）。

## 判据

    U1  `unattributed_sessions` 只返回「有写入且无公民」的会话
    U2  `attribute_session` 缺省是干跑——**一行都不写**
    U3  `--apply` 只改无归属行；**已有归属的一行不碰**
    U4  空 session / 空 citizen 要响亮报错（不静默当作"没事可做"）
    U5  没有缺口时 `render_unattributed_line` 返回空串（不制造噪音）
    U6  那行必须真的进 healthcheck 输出（装配，不只是函数对）
"""

import importlib.util
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import fish_identity_diag  # noqa: E402

SK = "codebuddy:orphan-session-0001"
SK2 = "codebuddy:orphan-session-0002"
CID = "01W4OWNER00000000000001"
CID2 = "01W4OTHER00000000000002"


class AttributionGapTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for cid, name in ((CID, "补账目标"), (CID2, "另一个公民")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id, display_name, kind, status,"
                " created_at, is_owner) VALUES (?,?,'role','active',?,'0')",
                (cid, name, fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _write(self, sk, cid=""):
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, citizen_id, project,"
            " tool_name, action, path, path_dir, path_abs, ok, seen_at)"
            " VALUES ('codebuddy',?,?,'W4','write','write','a.py','p0',"
            "'i:/p0/a.py',1,?)", (sk, cid, fish.now_utc()))

    # ------------------------------------------------------------------ U1
    def test_U1_lists_only_unattributed_write_sessions(self):
        """只列出「有写入且无公民」的会话；有归属的不算、非 write 的不算。

        变异：把 SQL 里的 `action='write'` 去掉 → 本条变红。
        """
        self._write(SK)                    # 无归属，应出现
        self._write(SK2, cid=CID)          # 有归属，不该出现
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, citizen_id, project,"
            " tool_name, action, path_dir, path_abs, ok, seen_at)"
            " VALUES ('codebuddy','readonly-sess','','W4','read','exec','p0',"
            "'i:/p0/a.py',1,?)", (fish.now_utc(),))
        self.conn.commit()
        got = {sk for sk, _n, _l in fish_identity_diag.unattributed_sessions(self.conn)}
        self.assertEqual(got, {SK})

    # ------------------------------------------------------------------ U2
    def test_U2_dry_run_writes_nothing(self):
        """缺省是**干跑**：匹配数照报，但一行都不写。

        变异：把 `if not matched or not apply:` 里的 `not apply` 去掉 → 本条变红。
        """
        self._write(SK)
        self._write(SK)
        self.conn.commit()
        matched, changed = fish_identity_diag.attribute_session(self.conn, SK, CID,
                                                           apply=False)
        self.assertEqual(matched, 2)
        self.assertEqual(changed, 0)
        left = self.conn.execute(
            "SELECT COUNT(*) FROM tool_activity WHERE session_key=? AND"
            " (citizen_id IS NULL OR citizen_id='')", (SK,)).fetchone()[0]
        self.assertEqual(left, 2, "干跑却写库了")

    # ------------------------------------------------------------------ U3
    def test_U3_apply_touches_only_unattributed_rows(self):
        """`--apply` 只改无归属行；**已有归属的一行不碰**（那可能是别人显式写的）。

        变异：把 UPDATE 的 `(citizen_id IS NULL OR citizen_id='')` 条件去掉
        → 本条变红。
        """
        self._write(SK)                    # 无归属 → 该被改
        self._write(SK, cid=CID2)          # 已有归属 → 不许动
        self.conn.commit()
        _m, changed = fish_identity_diag.attribute_session(self.conn, SK, CID, apply=True)
        self.assertEqual(changed, 1)
        rows = self.conn.execute(
            "SELECT citizen_id FROM tool_activity WHERE session_key=? ORDER BY id",
            (SK,)).fetchall()
        self.assertEqual([r[0] for r in rows], [CID, CID2])

    # ------------------------------------------------------------------ U4
    def test_U4_empty_arguments_raise_loudly(self):
        """空 session / 空 citizen 必须报错，不能静默当成"没事可做"。

        变异：把那个 `if not sk or not cid: raise` 换成 `return (0, 0)` → 本条变红。
        """
        with self.assertRaises(ValueError):
            fish_identity_diag.attribute_session(self.conn, "", CID, apply=False)
        with self.assertRaises(ValueError):
            fish_identity_diag.attribute_session(self.conn, SK, "", apply=False)

    # ------------------------------------------------------------------ U5
    def test_U5_no_gap_means_no_line(self):
        """没有缺口 → 返回空串（不制造噪音）。

        变异：把 `if not rows: return ""` 删掉 → 本条变红。
        """
        self.assertEqual(fish_identity_diag.render_unattributed_line(self.conn), "")
        self._write(SK)
        self.conn.commit()
        self.assertNotEqual(fish_identity_diag.render_unattributed_line(self.conn), "")

    # ------------------------------------------------------------------ U6
    def test_U6_line_reaches_the_healthcheck_output(self):
        """那行必须真的打印在 healthcheck 里（装配，不只是函数对）。

        这是"零件对了、装配没接"那一类坑的探针——本仓里 `render_questions_block`
        就这么死过一周。

        变异：把 `fish.py` healthcheck 里那两行接线删掉 → 本条变红。
        """
        self._write(SK)
        self.conn.commit()
        buf = StringIO()
        # `cmd_healthcheck` 以 `SystemExit(0/1)` 收尾（退出码是脚本可读的唯一结论），
        # 所以这里要接住它——否则测试会因为"程序正常退出"而失败。
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                fish.cmd_healthcheck(type("A", (), {"root": str(self.root)})())
        out = buf.getvalue()
        self.assertIn("归属缺口", out, "healthcheck 里没有归属缺口这一行")
        self.assertIn("activity attribute", out, "提示里没给补账命令")


if __name__ == "__main__":
    unittest.main(verbosity=2)
