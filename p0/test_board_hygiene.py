#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板卫生：读路径不建板 + 空板不渲染 + 空板提前归档 + sweep 进台账（2026-11-22）。

## 为什么需要这一组（全部有实测证据，不是设想）

实测：库里 **146 块板里 78 块是空板**（没有任何 `board_posts`）。而 `board_ensure`
有 10 个调用点，其中 **3 条是纯读路径**：

    board_block（注入渲染） / board_show（查板） / board_render_root（聚合根板）

**读一次就建一块板** —— 那 78 块里的大部分是这么来的，不是"没人用"。
不堵住读路径，任何清理策略都会退化成"清了又长"。

## 判据

    H1  `board_block`（注入渲染）走一次，`boards` 行数**不变**
    H2  `board_show`（查板）走一次，`boards` 行数**不变**，且返回可渲染的虚拟板
    H3  空板 `board_render` **不产文件**（返回 size=0）
    H4  空板在 `board.empty_sweep_days` 后**提前归档**，有内容的板**不提前**
    H5  `board_sweep` 已注册进维护台账（能在 `maintenance_task` 里查到）

## 变异自证（每条都写过怎么让它红，见各判据 docstring）

这一组全是"**某件事没有发生**"型判据——它们最容易写成永远绿的假判据。
所以每条都配一个"反向动作"：把守卫拆掉，它必须变红。
"""

import datetime
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import fish_board        # noqa: E402
import fish_maintenance  # noqa: E402

CID = "01HYGIENE0000000000001"


def _period(days_ago=0):
    """`YYMMDD`（与 `fish_common.board_period` 同口径，走 +08 本地日）。"""
    d = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()
    return (d - datetime.timedelta(days=days_ago)).strftime("%y%m%d")


class BoardHygieneTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, kind, status, created_at,"
            " is_owner) VALUES (?,?,?,?,?,'0')",
            (CID, "卫生测试AI", "role", "active", fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _board_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]

    # ------------------------------------------------------------------ H1
    def test_H1_board_block_does_not_create_a_board(self):
        """注入渲染走一次，`boards` 行数不变。

        变异：把 `board_ensure(conn, scope, key, create=False)` 改回
        `board_ensure(conn, scope, key)` → 行数 +1，本条变红。
        """
        before = self._board_count()
        blk = fish.board_block(self.conn, project="HYGIENE_NOPROJ")
        after = self._board_count()
        self.assertEqual(after, before,
                         f"`board_block` 建了板：{before} → {after}")
        self.assertIn("还没有人签到", blk,
                      "空板文案必须仍然出现——不建板不等于不说'今天没人'")

    # ------------------------------------------------------------------ H2
    def test_H2_board_show_does_not_create_a_board(self):
        """查板走一次，`boards` 行数不变，且返回的虚拟板能渲染。

        变异：同上，把 `create=False` 去掉 → 行数 +1，本条变红。
        """
        before = self._board_count()
        info = fish.board_show(self.conn, "project", "HYGIENE_NOPROJ")
        after = self._board_count()
        self.assertEqual(after, before,
                         f"`board_show` 建了板：{before} → {after}")
        self.assertIsNone(info["board"]["id"], "没有板时应返回 id=None 的虚拟板")
        self.assertEqual(info["presence"], [])
        self.assertEqual(info["posts"], [])
        # 接口形状不变：`_render_one` 只读 scope/scope_key/period_key/status/closed_at
        txt = fish._render_one(self.conn, info["board"], info["posts"])
        self.assertIn("看板", txt, "虚拟板必须能渲染，否则调用方会在渲染处炸")

    def test_H2b_virtual_board_is_never_persisted(self):
        """虚拟板不能悄悄落库——它是"没有板"的读数，不是一块板。

        变异：在 `_virtual_board` 里加一句 `board_ensure(...)` → 行数 +1，变红。
        """
        b = fish_board._virtual_board("project", "HYGIENE_NOPROJ")
        self.assertIsNone(b["id"])
        self.assertEqual(self._board_count(), 0)

    # ------------------------------------------------------------------ H3
    def test_H3_empty_board_still_renders_and_says_so(self):
        """空板**仍然产文件**，且文件里明写「未登记 = 未发生」。

        ## 这条守的是我自己的一次错判

        第一版设计里我要做「C 空板不产文件」，理由是"空文件没有信息量、占门面"。
        实测推翻：两条更早的契约都写着「空看板要**明写**而不是留白」——
        `test_board.py::B4`（渲染必须有两区、空板必须明写）
        与 `board_block` 的设计约束 3（留白会让 AI 以为看板功能坏了）。

        那两条讲的不只是注入包：**文件本身也是给人读的门面**。
        文件不存在与"今天没人开工"是两件事，删掉文件就把后者变成前者。

        C 已撤回。这条判据改成**反向守**：证明我没把它砍掉。

        变异：删掉 `_render_one` 里那句「还没有人签到——未登记 = 未发生」→ 本条变红。
        """
        board = fish.board_ensure(self.conn, "project", "HYGIENE_EMPTY")
        path, size, split = fish.board_render(self.conn, self.root, board,
                                              home_root=self.root)
        self.assertGreater(size, 0, "空板必须照常产文件——留白会被读成'功能坏了'")
        target = fish.board_out_path(self.root, board, home_root=self.root)
        self.assertTrue(target.exists(), f"空板文件缺失：{target}")
        text = target.read_text(encoding="utf-8")
        self.assertIn("未登记 = 未发生", text,
                      "空板文件必须明写这句，不能只有标题和空表")

    def test_H3b_board_with_posts_renders_content(self):
        """有内容的板照常出内容——空判/明写都不能把正常板也砍了。

        变异：把 `_render_one` 里的动态区整段删掉 → 本条变红。
        """
        board = fish.board_ensure(self.conn, "project", "HYGIENE_USED")
        fish.board_post(self.conn, board, CID, "note", "有内容的一条动态")
        fish.board_render(self.conn, self.root, board, home_root=self.root)
        text = fish.board_out_path(self.root, board,
                                   home_root=self.root).read_text(encoding="utf-8")
        self.assertIn("有内容的一条动态", text, "有内容的板没渲染出动态")

    # ------------------------------------------------------------------ H4
    def test_H4_empty_board_is_swept_early(self):
        """昨天的空板提前归档；昨天**有内容**的板不提前归档。

        变异：把 `board_sweep` 查询里的空判那段 `OR (...)` 删掉 → 空板仍 open，变红。
        """
        pk = _period(1)                       # 昨天
        empty = fish.board_ensure(self.conn, "project", "HYGIENE_E1", pk)
        used = fish.board_ensure(self.conn, "project", "HYGIENE_U1", pk)
        fish.board_post(self.conn, used, CID, "note", "昨天的动态")

        # **不传 days**：走默认策略路径，空板规则才生效（显式 --days 只按日期）
        res = fish.board_sweep(self.conn, self.root)
        status = {r[0]: r[1] for r in self.conn.execute(
            "SELECT id, status FROM boards").fetchall()}
        self.assertEqual(status[empty["id"]], "closed",
                         "昨天的空板没有被提前归档")
        self.assertEqual(status[used["id"]], "open",
                         "有内容的板被提前归档了——空判必须只打空板")
        self.assertGreaterEqual(res.get("empty_swept") or 0, 1,
                                "返回值里要能看出有几块是'按空归档'的")

    def test_H4b_empty_rule_can_be_disabled(self):
        """`board.empty_sweep_days < 1` 关掉空板规则，只剩日期规则。

        变异：把 `empty_cutoff` 的 `if empty_days >= 1 else None` 改成恒算 → 本条变红。
        """
        fish.set_config(self.conn, "board.empty_sweep_days", 0)
        pk = _period(1)
        empty = fish.board_ensure(self.conn, "project", "HYGIENE_E2", pk)
        fish.board_sweep(self.conn, self.root)          # 默认策略路径
        st = self.conn.execute("SELECT status FROM boards WHERE id=?",
                               (empty["id"],)).fetchone()[0]
        self.assertEqual(st, "open", "关掉空板规则后，昨天的空板不该被归档")

    # ------------------------------------------------------------------ H5
    def test_H5_board_sweep_is_in_the_maintenance_ledger(self):
        """`board_sweep` 必须在维护台账里——否则跑没跑成永远查不到。

        变异：把 `TASK_ORDER` / `_TASKS` 里的 `board_sweep` 删掉 → 本条变红。
        """
        self.assertIn("board_sweep", fish_maintenance.TASK_ORDER)
        self.assertIn("board_sweep", fish_maintenance._TASKS)
        fish_maintenance.ensure_maintenance_tables(self.conn)
        names = {r[0] for r in self.conn.execute(
            "SELECT task_name FROM maintenance_task").fetchall()}
        self.assertIn("board_sweep", names, "台账里没有 board_sweep 这一行")
        row = self.conn.execute(
            "SELECT enabled, interval_hours FROM maintenance_task "
            "WHERE task_name='board_sweep'").fetchone()
        self.assertEqual(row[0], 1, "board_sweep 应默认启用")
        self.assertEqual(row[1], 24)

    def test_H5b_board_sweep_task_runs_and_reports(self):
        """台账任务真跑一次：能拿到 (affected, detail)，且 detail 两个 cutoff 都在。

        变异：把 `_task_board_sweep` 的 `return` 改成 `return 0, {}` → detail 断言变红。
        """
        pk = _period(1)
        fish.board_ensure(self.conn, "project", "HYGIENE_E3", pk)
        affected, detail = fish_maintenance._TASKS["board_sweep"](
            self.conn, self.root, {}, datetime.datetime.now(datetime.timezone.utc))
        self.assertGreaterEqual(affected, 1)
        self.assertIn("cutoff", detail)
        self.assertIn("empty_cutoff", detail)
        self.assertIn("empty_swept", detail)

    def test_H6_board_sweep_is_run_by_daily_pipeline(self):
        """日报的维护管道必须真的跑到 `board_sweep`，并把 `last_run_at` 写进台账。

        2026-11-23（复查 BUG-4）：`board_sweep` 已进台账，但日报仍直接调
        `fish.board_sweep()`，而维护管道的 `only=` 不含它 —— 台账 `last_run_at`
        永远是 None（跑没跑查不到）。修法：唯一 owner = 维护管道。

        变异：把 `daily_report.MAINTENANCE_DAILY_ONLY` 里的 `board_sweep` 删掉
        → `last_run_at` 仍为 None，本条变红。
        """
        import daily_report
        self.assertIn("board_sweep", daily_report.MAINTENANCE_DAILY_ONLY,
                      "日报维护管道的 only 名单里没有 board_sweep")
        fish_maintenance.run_maintenance(
            self.conn, self.root,
            only=list(daily_report.MAINTENANCE_DAILY_ONLY), force=True)
        row = self.conn.execute(
            "SELECT last_run_at, last_status FROM maintenance_task "
            "WHERE task_name='board_sweep'").fetchone()
        self.assertIsNotNone(row[0], "日报管道没跑 board_sweep：last_run_at 仍是 None")
        self.assertEqual(row[1], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
