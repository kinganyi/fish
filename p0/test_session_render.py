#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""撞车自撞修复 · T04 判据（渲染三处一致 + 注入包折叠 + 无 ULID 契约）。

## 来源

设计：`45-撞车自撞修复-设计.md` §4（渲染与可读名）+ §7.1。
一句话：会话短标识**只在一处算**（`fish_naming.session_labels`），看板文件 / 注入包 /
`board show` 三个产物**吃同一份 label** —— 所以这里对三个产物做**同一组断言**，
而不是各写一遍（各写一遍的话，改了一边另一边永远绿）。

## 判据清单

  S18 渲染无 ULID / 无完整会话键（人类面）；机器面（`board show --json`）**保留**会话键
  S19 会话短标识成对且不同；非 ASCII 会话键按**字符**切、不出乱码
  S20 注入包按公民折叠（同公民两会话只占 1 格，附「+N 会话」）
  另：看板文件仍守 32KB 预算；注入包仍守 3 格预算
"""
import datetime
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_naming  # noqa: E402

CID_A = "01SESSIONRENDER00000A1"
CID_B = "01SESSIONRENDER00000B2"
CID_C = "01SESSIONRENDER00000C3"

# 与 `test_board_readable` 同一份口径：26 位 Crockford base32（ULID 字符集）
ULID_RE = re.compile(r"(?<![0-9A-HJKMNP-TV-Z])[0-9A-HJKMNP-TV-Z]{26}(?![0-9A-HJKMNP-TV-Z])")

SESSION_A1 = "dsh:session-88f9b6a5-65c5-49f5-8b65"
SESSION_A2 = "dsh:session-d8bcad9b-5794-436d-a8f9"
SESSION_B1 = "self:企业微信-主AI"
SESSION_B2 = "self:企业微信-备用"


class RenderBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-session-render-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('PJ','大肥鱼-测试',NULL,0,'active')")
        for cid, name in ((CID_A, "AI-甲"), (CID_B, "AI-乙"), (CID_C, "AI-丙")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host)"
                " VALUES (?,?,'resident','active',?,'dsh')",
                (cid, name, fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 夹具
    def _checkin(self, cid, session_key, doing, surface=""):
        return fish.board_checkin(self.conn, cid, doing, surface=surface,
                                  scope="project", scope_key="PJ",
                                  session_key=session_key)

    def _board(self):
        return fish.board_ensure(self.conn, "project", "PJ")

    def _render_text(self):
        board = self._board()
        fish.board_render(self.conn, self.root, board)
        path = fish.board_out_path(self.root, board)
        self.assertTrue(path.exists(), f"看板文件没生成：{path}")
        return path.read_text(encoding="utf-8")

    def _inject_text(self):
        block = fish.board_block(self.conn, "PJ")
        self.assertIsNotNone(block, "注入包看板段不该是 None")
        return block

    def _show_json(self):
        info = fish.board_show(self.conn, "project", "PJ")
        return json.dumps({"board": info["board"], "presence": info["presence"],
                           "posts": [list(p) for p in info["posts"]]},
                          ensure_ascii=False, default=str)


class NoIdsInHumanFacesTest(RenderBase):
    """S18：三处渲染面的共同契约（人类面永不含机器码）。"""

    def test_S18_no_ulid_and_no_full_session_key(self):
        self._checkin(CID_A, SESSION_A1, "改 fish 判定", "p0/fish.py")
        self._checkin(CID_A, SESSION_A2, "改看板", "p0/board.py")
        self._checkin(CID_C, "dsh:session-cccc0000-1111", "写文档", "docs/a.md")
        text = self._render_text()
        inject = self._inject_text()
        shown = self._show_json()
        for name, blob in (("看板文件", text), ("注入包", inject), ("board show(JSON)", shown)):
            self.assertIsNone(ULID_RE.search(blob),
                              f"{name} 里出现了 26 位公民 ID：{ULID_RE.search(blob)}")
        # 人类面：完整会话键一次都不许出现（只允许 `host#short` 形态）
        for name, blob in (("看板文件", text), ("注入包", inject)):
            self.assertNotIn("dsh:session-", blob, f"{name} 里出现了完整会话键")
            self.assertNotIn("self:企业微信", blob, f"{name} 里出现了完整会话键")
        # 机器面反过来：会话键就是要给脚本用的，必须保留
        self.assertIn(SESSION_A1, shown, "机器视图必须保留完整会话键")
        self.assertIn("session_short", shown, "机器视图要带会话短标识字段")

    def test_S18_board_file_stays_under_budget(self):
        for index in range(40):
            self._checkin(CID_A, f"dsh:session-load{index:04d}", f"任务 {index}",
                          f"p{index}/x.py")
        text = self._render_text()
        self.assertLess(len(text.encode("utf-8")), 32 * 1024,
                        "看板文件必须仍在 32KB 预算内（会话维度不许把文件撑爆）")


class SessionLabelsTest(RenderBase):
    """S19：会话短标识成对出现、互不相同；非 ASCII 按字符切。"""

    def test_S19_two_sessions_get_distinct_short_labels(self):
        self._checkin(CID_A, SESSION_A1, "改 fish 判定", "p0/fish.py")
        self._checkin(CID_A, SESSION_A2, "改看板", "p0/board.py")
        text = self._render_text()
        rows = fish.board_presence_rows(self.conn, self._board())
        self.assertEqual(len(rows), 2)
        labels = [row["label"] for row in rows]
        self.assertEqual(len(set(labels)), 2, f"两行标签必须不同：{labels}")
        shorts = [row["session_short"] for row in rows]
        self.assertEqual(sorted(shorts), ["dsh#88f9", "dsh#d8bc"],
                         f"会话短标识取 `host#前4位`：{shorts}")
        for label, short in zip(labels, shorts):
            self.assertIn("·", label, f"同公民多会话的标签要带分隔符：{label}")
            self.assertIn(short, label)
            self.assertIn(label, text, f"看板文件里要能看到会话短标识：{label}")

    def test_S19_non_ascii_key_sliced_by_character(self):
        self._checkin(CID_B, SESSION_B1, "改企业微信侧", "p0/im.py")
        self._checkin(CID_B, SESSION_B2, "改备用通道", "p0/im2.py")
        rows = fish.board_presence_rows(self.conn, self._board())
        labels = [row["label"] for row in rows if row["citizen_id"] == CID_B]
        self.assertEqual(len(labels), 2)
        self.assertEqual(len(set(labels)), 2, f"非 ASCII 会话也要能消歧：{labels}")
        for label in labels:
            self.assertNotIn("\ufffd", label, "按字节切会出半截乱码（U+FFFD）")
            self.assertIn("self#", label)
            self.assertIn("企业微信", label, "按**字符**取前 4 个：企业微信")
        self.assertIn("企业微信-主", labels[0] + labels[1],
                      "4 位撞车时逐级加长到 6 位（永不回落到机器码）")

    def test_S19_single_session_has_no_suffix(self):
        """既有文本保持不变：一个公民只有一个会话时不加后缀。"""
        self._checkin(CID_C, "dsh:session-cccc0000-1111", "写文档", "docs/a.md")
        row = fish.board_presence_rows(self.conn, self._board())[0]
        self.assertEqual(row["label"], "AI-丙", f"单会话不许加后缀：{row['label']}")
        self.assertNotIn("·dsh#", self._render_text())


class InjectFoldTest(RenderBase):
    """S20：注入包按公民折叠（守住 3 格预算）。"""

    def test_S20_same_citizen_folds_into_one_cell(self):
        self._checkin(CID_A, SESSION_A1, "改 fish 判定", "p0/fish.py")
        self._checkin(CID_A, SESSION_A2, "改看板渲染", "p0/board.py")
        self._checkin(CID_C, "dsh:session-cccc0000-1111", "写文档", "docs/a.md")
        block = self._inject_text()
        self.assertIn("+1 会话", block, f"同公民两会话必须折叠并写明还有几个：{block}")
        self.assertEqual(block.count("AI-甲"), 1,
                         f"该公民只许占 1 格（注入包有 3 格硬上限）：{block}")
        self.assertIn("2 人在场", block, "人数按公民去重")
        self.assertIn("3 会话", block, "另给会话数（否则读不出规模）")

    def test_S20_cell_budget_three(self):
        """3 个公民各 2 个会话 = 6 行 presence，注入包仍只占 3 格（不许被会话挤爆）。"""
        for citizen, prefix in ((CID_A, "dsh:a"), (CID_B, "dsh:b"), (CID_C, "dsh:c")):
            for index in (1, 2):
                self._checkin(citizen, f"{prefix}{index}", f"任务 {citizen[-2:]}-{index}")
        block = self._inject_text()
        cells = sum(block.count(f"AI-{suffix}") for suffix in ("甲", "乙", "丙"))
        self.assertEqual(cells, 3, f"3 个公民应占 3 格（每格一次名字）：{block}")
        self.assertIn("3 人在场", block)
        self.assertIn("6 会话", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
