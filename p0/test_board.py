#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板（Board）· P1-b 判据（2026-10-25）。

看板的定义（`38` §1.1）：**回答「此刻谁在动哪块」**。
判据围绕这条定义与三条硬纪律（`09` §4.6 的三个已知坑）展开：

  坑 1 无限追加必死 → B5 容量切分
  坑 2 只写自己行是并发协议 → B2 签到幂等、不覆盖别人
  坑 3 看板 ≠ 日志 → B4 结构（在场表 + 今日动态两区）

判据清单
--------
  B1  表结构（boards / presence / board_posts + 唯一约束）
  B2  签到幂等（同一公民重复签到只有一行；不动别人的行）
  B3  未签到时给提示、签到后提示消失
  B4  渲染结构（在场表 + 今日动态两区；明写"未登记=未发生"）
  B5  容量纪律（造 500 条 → 不超 32KB 且按小时切分）
  B6  单条动态 ≤200 字（硬截断）
  B7  **删渲染文件可自愈**（表是真相，文件是投影）
  B8  归档后不再接收签到，但不删
  B9  变异：关掉切分 → B5 必须红（证明判据有判别力）
"""
import importlib.util
import datetime
import os
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

CID_A = "01BOARDTESTA000000000001"
CID_B = "01BOARDTESTB000000000002"


class BoardTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for cid, name in ((CID_A, "AI-A"), (CID_B, "AI-B")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (cid, name, "resident", "member", None, "active", fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    # ------------------------------------------------------------------ B1
    def test_B1_tables_exist(self):
        """三张表 + UNIQUE(scope, scope_key, period_key)。"""
        for t in ("boards", "presence", "board_posts"):
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({t})")]
            self.assertTrue(cols, f"表 {t} 没建出来")
        b1 = fish.board_ensure(self.conn, "project", "P1")
        b2 = fish.board_ensure(self.conn, "project", "P1")
        self.assertEqual(b1["id"], b2["id"], "同一 (scope,key,period) 必须只有一块板")
        # 不同 scope 要分开
        b3 = fish.board_ensure(self.conn, "root", "")
        self.assertNotEqual(b1["id"], b3["id"], "root 与 project 必须是两块板")

    # ------------------------------------------------------------------ B2
    def test_B2_checkin_idempotent(self):
        """同一公民重复签到只有一行；**不动别人的行**（只写自己行是并发协议）。"""
        fish.board_checkin(self.conn, CID_A, "做 A 的事", scope="project", scope_key="P1")
        n1 = self.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0]
        fish.board_checkin(self.conn, CID_A, "改主意了，做 B 的事",
                           scope="project", scope_key="P1")
        n2 = self.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0]
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1, "重复签到不该变成两行")
        row = self.conn.execute("SELECT doing FROM presence WHERE citizen_id=?",
                                (CID_A,)).fetchone()
        self.assertEqual(row[0], "改主意了，做 B 的事", "重复签到应更新 doing")

        # 另一个人签到，不能覆盖 A 的行
        fish.board_checkin(self.conn, CID_B, "B 在干别的", scope="project", scope_key="P1")
        rows = {r[0]: r[1] for r in self.conn.execute(
            "SELECT citizen_id, doing FROM presence")}
        self.assertEqual(rows[CID_A], "改主意了，做 B 的事", "A 的行被 B 覆盖了")
        self.assertEqual(rows[CID_B], "B 在干别的")

    def test_B2_doing_truncated(self):
        """doing 超 80 字要截断——看板一行只放得下一句话。

        （`doing` **必填**那条由 CLI 的 `required=True` + `cmd_board` 的显式检查保证，
        测在 `test_board_cli` 里更合适；这里只测函数层的截断。）
        """
        fish.board_checkin(self.conn, CID_A, "x" * 200, scope="project", scope_key="P1")
        row = self.conn.execute("SELECT doing FROM presence WHERE citizen_id=?",
                                (CID_A,)).fetchone()
        self.assertLessEqual(len(row[0]), fish.BOARD_DOING_MAX,
                             f"doing 应被截断到 {fish.BOARD_DOING_MAX} 字，实际 {len(row[0])}")

    # ------------------------------------------------------------------ B3
    def test_B3_checkin_reminder(self):
        """未签到 → 有提示；签到后 → 没提示。"""
        ok, tip = fish.board_checkin_or_remind(self.conn, CID_A, "P1")
        self.assertFalse(ok)
        self.assertIn("签到", tip or "")
        fish.board_checkin(self.conn, CID_A, "干活", scope="project", scope_key="P1")
        ok2, tip2 = fish.board_checkin_or_remind(self.conn, CID_A, "P1")
        self.assertTrue(ok2)
        self.assertIsNone(tip2)

    # ------------------------------------------------------------------ B4
    def test_B4_render_structure(self):
        """渲染必须有两区：在场表 + 今日动态；空看板要**明写**而不是留白。"""
        board = fish.board_ensure(self.conn, "project", "P1")
        path, size, _ = fish.board_render(self.conn, self.root, board)
        text = fish.board_out_path(self.root, board).read_text(encoding="utf-8")
        self.assertIn("## 在场", text, "缺在场区")
        self.assertIn("## 今日动态", text, "缺动态区")
        self.assertIn("未登记 = 未发生", text, "空看板必须明写这句话，不能留白")
        self.assertIn("请勿手工编辑", text, "必须声明本文件是渲染产物")

        fish.board_checkin(self.conn, CID_A, "做看板", surface="p0/fish.py",
                           scope="project", scope_key="P1")
        fish.board_post(self.conn, board, CID_A, "progress", "跑通了")
        fish.board_render(self.conn, self.root, board)
        text2 = fish.board_out_path(self.root, board).read_text(encoding="utf-8")
        # 合并裁决：落点用 devA 的 `board_out_path`（唯一入口，boards.path 相对
        # fish-home）；内容契约用主树 T03「可读名 + 不许出现 26 位公民 ID」。
        self.assertIn("AI-A", text2, "在场行必须显示可读名")
        self.assertNotIn(CID_A, text2, "人类可读面不许出现 26 位公民 ID")
        self.assertIn("做看板", text2)
        self.assertIn("跑通了", text2)

    # ------------------------------------------------------------------ B6
    def test_B6_post_truncated(self):
        """单条动态 ≤200 字（超长硬截断并标省略号）。"""
        board = fish.board_ensure(self.conn, "project", "P1")
        long_text = "长" * 500
        fish.board_post(self.conn, board, CID_A, "note", long_text)
        row = self.conn.execute("SELECT text FROM board_posts").fetchone()
        self.assertLessEqual(len(row[0]), fish.BOARD_MAX_POST_CHARS,
                             f"动态未被截断：{len(row[0])} 字")
        self.assertTrue(row[0].endswith("…"), "截断应标省略号")

    # ------------------------------------------------------------------ B5/B9
    def test_B5_capacity_split(self):
        """造 500 条动态：渲染文件不超 32KB，且发生按小时切分。"""
        board = fish.board_ensure(self.conn, "project", "P1")
        # 造跨 5 个小时的动态，每条接近上限 → 总量必然超 32KB
        base = datetime.datetime.now(datetime.timezone.utc)
        for i in range(500):
            ts = (base - datetime.timedelta(hours=i // 100)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.conn.execute(
                "INSERT INTO board_posts (board_id,citizen_id,kind,text,created_at) "
                "VALUES (?,?,?,?,?)",
                (board["id"], CID_A, "note", "第%d条" % i + "内容" * 60, ts))
        self.conn.commit()
        path, size, split = fish.board_render(self.conn, self.root, board)
        self.assertLessEqual(size, fish.BOARD_MAX_BYTES,
                             f"渲染文件 {size} B 超过上限 {fish.BOARD_MAX_BYTES} B")
        self.assertTrue(split, "超限时必须切分（v1 就是死在只追加不切分上）")
        # 分片文件要真的存在
        shards = list(fish.board_out_path(self.root, board).parent.glob("看板-*-*.md"))
        self.assertTrue(shards, "应产出 -HH 分片文件")

    def test_B5_mutation_no_split_goes_red(self):
        """变异：把上限调大 → 不切分 → 文件必然超 32KB（证明 B5 有判别力）。"""
        board = fish.board_ensure(self.conn, "project", "P1")
        base = datetime.datetime.now(datetime.timezone.utc)
        for i in range(300):
            ts = (base - datetime.timedelta(hours=i // 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.conn.execute(
                "INSERT INTO board_posts (board_id,citizen_id,kind,text,created_at) "
                "VALUES (?,?,?,?,?)",
                (board["id"], CID_A, "note", "第%d条" % i + "内容" * 60, ts))
        self.conn.commit()
        orig = fish.BOARD_MAX_BYTES
        try:
            fish.BOARD_MAX_BYTES = 10 ** 9          # 假装没有容量约束
            _p, size_no_limit, split_no = fish.board_render(self.conn, self.root, board)
        finally:
            fish.BOARD_MAX_BYTES = orig
        self.assertFalse(split_no, "上限调大后不该切分")
        self.assertGreater(size_no_limit, orig,
                           f"不切分时必然超 32KB（实测 {size_no_limit} B）——"
                           "这正是 v1 的死法")

    # ------------------------------------------------------------------ B7
    def test_B7_render_self_heals(self):
        """**删掉渲染文件，重跑即重建**（表是真相，文件是投影）。"""
        board = fish.board_ensure(self.conn, "project", "P1")
        fish.board_checkin(self.conn, CID_A, "干活", scope="project", scope_key="P1")
        path, _, _ = fish.board_render(self.conn, self.root, board)
        p = fish.board_out_path(self.root, board)
        before = p.read_text(encoding="utf-8")
        os.remove(p)
        self.assertFalse(p.exists())
        path2, _, _ = fish.board_render(self.conn, self.root, board)
        self.assertEqual(path, path2)
        self.assertTrue(p.exists(), "删掉的渲染文件应被重建")
        self.assertEqual(p.read_text(encoding="utf-8"), before,
                         "重建内容应与删除前完全一致")

    # ------------------------------------------------------------------ B8
    def test_B8_close_archives(self):
        """归档后不再接收签到，但**不删**任何东西。"""
        board = fish.board_ensure(self.conn, "project", "P1")
        fish.board_checkin(self.conn, CID_A, "干活", scope="project", scope_key="P1")
        fish.board_close(self.conn, self.root, "project", "P1")
        st = self.conn.execute("SELECT status FROM boards WHERE id=?",
                               (board["id"],)).fetchone()[0]
        self.assertEqual(st, "closed")
        with self.assertRaises(ValueError):
            fish.board_checkin(self.conn, CID_B, "迟到", scope="project", scope_key="P1")
        n = self.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0]
        self.assertEqual(n, 1, "归档不该删在场行")
        path = fish.board_out_path(self.root, board).read_text(encoding="utf-8")
        self.assertIn("closed", path, "渲染头应标出已归档")


    # ------------------------------------------------------------------ B9
    def test_B9_project_open_creates_board(self):
        """新项目一开**自动出看板**（`09` §8.3 C1）。

        走真实的 `cmd_project_add`（不是手工调 board_ensure）——
        判据要钉的是"开项目这条路径会顺手出板"，而不是"board_ensure 能建板"。
        """
        import argparse
        args = argparse.Namespace(root=str(self.root), prefix="NEWP", name="新项目")
        # ⚠️ 返回值是 `None`——这是**既有 CLI 契约**（`main()` 里 `args.func(args)`
        # 忽略返回值），不是缺陷。判据只看副作用，不锚定返回值形状。
        fish.cmd_project_add(args)
        # 立刻能在项目目录里看到看板（不需要额外一步）。
        # 定位走 `board_out_path`（唯一落点入口），并顺带钉住新口径里的
        # `10-项目库/<项目>/` 段——只断"文件存在"会让落点漂移假绿。
        b = fish.board_ensure(self.conn, "project", "NEWP")
        self.assertTrue(b["path"].startswith("10-项目库/NEWP/"),
                        f"看板 path 应是相对 fish-home 的 10-项目库/<项目>/：{b['path']}")
        files = list(fish.board_out_path(self.root, b).parent.glob("看板-*.md"))
        self.assertTrue(files, "开项目后应自动生成看板文件")
        self.assertIn("## 在场", files[0].read_text(encoding="utf-8"))
        # 表里也要有这块板
        n = self.conn.execute("SELECT COUNT(*) FROM boards WHERE scope='project' "
                              "AND scope_key='NEWP'").fetchone()[0]
        self.assertEqual(n, 1, "boards 表里应有这块板")

    # ------------------------------------------------------------------ B10
    def test_B10_injection_has_board_block(self):
        """注入包必须带看板段；**空看板要明写"还没有人签到"**，不能留白。"""
        # 空看板
        block = fish.board_block(self.conn, "P1")
        self.assertIsNotNone(block, "注入包应包含看板段")
        self.assertIn("还没有人签到", block, "空看板要明写，不能留白")
        self.assertIn("未登记 = 未发生", block)
        # 有人签到后
        fish.board_checkin(self.conn, CID_A, "做看板", scope="project", scope_key="P1")
        block2 = fish.board_block(self.conn, "P1")
        self.assertIn("1 人在场", block2)
        self.assertIn("做看板", block2)

    def test_B10_in_pack_with_layer_budget(self):
        """看板段真的进了注入包，且 meta.layers 里有独立的 board 计数。

        为什么要独立计数：混在别的层里就说不清"看板占了多少"，
        而预算裁剪时要靠这个数字决定先裁谁。
        """
        fish.board_checkin(self.conn, CID_A, "做看板", scope="project", scope_key="P1")
        pack, meta = fish.build_injection_pack(
            self.conn, self.root, CID_A, budget=6000, project="P1")
        self.assertIn("看板", pack, "注入包里应出现看板段")
        self.assertIn("board", meta["layers"], "meta.layers 应有独立的 board 计数")
        self.assertGreater(meta["layers"]["board"], 0)

    def test_B10_board_trimmed_first(self):
        """预算紧张时**先裁看板**（增强项让位于注意事项）。

        这条守的是"重要性排序"：没有看板 AI 照样能工作，
        但没有"别处的注意事项"它可能重复犯错。
        """
        fish.board_checkin(self.conn, CID_A, "做看板", scope="project", scope_key="P1")
        _pack, meta = fish.build_injection_pack(
            self.conn, self.root, CID_A, budget=120, project="P1")
        self.assertIn("看板", meta["trimmed"],
                      f"超预算时应先裁看板，实际裁剪链={meta['trimmed']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
