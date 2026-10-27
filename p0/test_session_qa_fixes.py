#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""撞车自撞修复 · QA 补判据（S28 / S29 / S31 + S18 夹具增强）。

## 为什么单独一个文件

QA Round 1 指出：设计 §7.1 的 31 条判据里 **S28 / S29 / S31 没有机器判据**，
于是 F1（假 `#N`）、F3（跨板未合并）"判据全绿却漏掉了"。本文件补上这三条，
并给 S18 补一个**含 `tool_activity` 的夹具**（否则「在动但未在本板登记」一节
根本不出现在被测文本里，S18 只测了一半——F2 的泄漏点从未被覆盖）。

## 判据与失败条件

  S28 同一公民在 3 块板各 1 行（同 session_key）→ 三处 label 都**不含** `#N`
  S29 同公民 A 板 2 会话 + B 板 1 会话 → A 板两行带会话后缀，**B 板那行不带**
  S31 同 (citizen, session) 跨 3 块板 → 快照**恰好 1 条** entry；declared 为并集；
      last_seen 取 max；find_collisions 对同一目标只产出 **1 条** hit
  S18+ 看板渲染（含「在动但未在本板登记」一节）——**无完整会话键**、无 26 位 ULID
"""
import datetime
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_activity  # noqa: E402
import fish_naming  # noqa: E402

CID_A = "01QAFIXSESSION00000A1"
CID_B = "01QAFIXSESSION00000B2"

SESSION_A1 = "dsh:session-88f9b6a5-65c5-49f5-8b65-0001"
SESSION_A2 = "dsh:session-d8bcad9b-5794-436d-a8f9-0002"

ULID_RE = re.compile(r"(?<![0-9A-HJKMNP-TV-Z])[0-9A-HJKMNP-TV-Z]{26}(?![0-9A-HJKMNP-TV-Z])")
# QA R2-T1：原正则只匹配标准 UUID（`dsh:xxxxxxxx-xxxx`），而夹具键是
# `dsh:session-unchecked-...`——断言因此**恒真**（假绿），F2 回归抓不住。
# 改成"dsh: 后 8 个以上字母数字/连字符"，覆盖 `dsh:session-*` 与标准 UUID 两种形态。
FULL_SESSION_RE = re.compile(r"dsh:[A-Za-z0-9\-]{8,}")


def _ts(minutes_ago=0):
    base = datetime.datetime.now(datetime.timezone.utc)
    return (base - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class QaFixBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-qa-fix-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('PJ','大肥鱼-测试',NULL,0,'active')")
        for cid, name in ((CID_A, "AI-甲"), (CID_B, "AI-乙")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host) "
                "VALUES (?,?,'visitor','active',?,'dsh')",
                (cid, name, fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _checkin(self, project, cid=CID_A, session_key=SESSION_A1, doing="干活",
                 surface="p0/fish.py"):
        return fish.board_checkin(self.conn, cid, doing=doing, surface=surface,
                                  scope="project", scope_key=project,
                                  session_key=session_key)

    def _presence(self, board):
        return fish.board_presence_rows(self.conn, board)


class S28NoFakeHashTest(QaFixBase):
    """S28：跨板 3 行不产生假 `#N`（P6 现场：今天会出 `fish-主AI-3·dsh#3`）。"""

    def test_S28_three_boards_same_identity_no_fake_hash(self):
        boards = [self._checkin(f"PJ{i}", surface=f"p0/f{i}.py") for i in range(3)]
        rows = []
        for b in boards:
            rows.extend(self._presence(b))
        labels = fish_naming.session_labels(rows)
        mine = [v for (cid, _sk), v in labels.items() if cid == CID_A]
        self.assertEqual(len(mine), 1, "同一 (citizen, session) 只该有一个标签")
        self.assertNotIn("#", mine[0], f"跨板 3 行不许产生假 #N：{mine[0]}")
        # 契约：该公民在这批行里只有 1 个会话 → 沿用公民名（不加会话后缀）
        self.assertEqual(mine[0], "AI-甲")


class S29BoardScopedSuffixTest(QaFixBase):
    """S29：多会话判定按**板内行数**——A 板两行带后缀，B 板一行不带。"""

    def test_S29_suffix_follows_board_scoped_rows(self):
        board_a = self._checkin("PJA", session_key=SESSION_A1, surface="p0/a.py")
        fish.board_checkin(self.conn, CID_A, doing="第二会话", surface="p0/b.py",
                           scope="project", scope_key="PJA", session_key=SESSION_A2)
        board_b = self._checkin("PJB", session_key=SESSION_A2, surface="p0/c.py")

        rows_a = self._presence(board_a)
        rows_b = self._presence(board_b)
        labels_a = {r["session_key"]: fish_naming.session_labels(rows_a)[
            (r["citizen_id"], r["session_key"])] for r in rows_a}
        labels_b = {r["session_key"]: fish_naming.session_labels(rows_b)[
            (r["citizen_id"], r["session_key"])] for r in rows_b}
        self.assertEqual(len(rows_a), 2)
        self.assertEqual(len(rows_b), 1, "B 板只该有自己那一行")
        for label in labels_a.values():
            self.assertIn(fish_naming.SESSION_LABEL_SEP, label,
                          "A 板两会话 → 两行都带会话后缀")
        self.assertNotIn(fish_naming.SESSION_LABEL_SEP, labels_b[SESSION_A2],
                         "B 板只有 1 会话 → **不带**后缀（判定按板内行数）")


class S31CrossBoardMergeTest(QaFixBase):
    """S31：快照按身份键跨板合并 + find_collisions 同一目标只 1 条 hit。"""

    def test_S31_snapshot_merges_same_identity_across_boards(self):
        for i in range(3):
            self._checkin(f"PJ{i}", session_key=SESSION_A1,
                          surface=f"p0/f{i}.py", doing=f"板{i}")
        snap = fish_activity.build_snapshot(
            self.conn, out_path=self.tmp / "snap.json", window_min=90)
        mine = [e for e in snap["entries"] if e["citizen_id"] == CID_A]
        self.assertEqual(len(mine), 1, f"同身份跨 3 板只该 1 条 entry，实得 {len(mine)}")
        entry = mine[0]
        self.assertEqual(sorted(entry.get("projects") or []),
                         ["PJ0", "PJ1", "PJ2"], "projects 应合并三块板")
        self.assertEqual(sorted(entry["declared"]),
                         ["p0/f0.py", "p0/f1.py", "p0/f2.py"], "声明面应取并集")

        # find_collisions：另一个人要写其中一块面 → 对同一目标最多 1 条
        # （CID_B 已在 setUp 插入，这里不重复插）
        target = str((Path.cwd() / "p0" / "f1.py").resolve())
        hits = fish_activity.find_collisions(snap, target_abs=target,
                                             own_session_key="dsh:session-other")
        self.assertEqual(len(hits), 1, f"同一目标只该 1 条 hit，实得 {len(hits)}")

    def test_S31b_snapshot_merge_keeps_last_seen_max(self):
        self._checkin("PJX", session_key=SESSION_A1, surface="p0/x.py")
        # 手动把某一块板的 last_seen 改早，确保合并取 max
        self.conn.execute("UPDATE presence SET last_seen_at=? WHERE citizen_id=?",
                          (_ts(120), CID_A))
        self.conn.execute("UPDATE presence SET last_seen_at=? WHERE citizen_id=? AND "
                          "surface=?", (_ts(1), CID_A, "p0/x.py"))
        self.conn.commit()
        snap = fish_activity.build_snapshot(
            self.conn, out_path=self.tmp / "snap2.json", window_min=90)
        mine = [e for e in snap["entries"] if e["citizen_id"] == CID_A]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["last_seen"], _ts(1), "last_seen 取 max")


class S18UncheckedSectionTest(QaFixBase):
    """S18 增强：看板渲染含「在动但未在本板登记」一节时，人类面**无完整会话键**。"""

    def test_S18_unchecked_section_hides_full_session_key(self):
        # 一个"在动但没签到"的会话：只写 tool_activity
        self.conn.execute(
            "INSERT INTO tool_activity (host,session_key,citizen_id,project,tool_name,"
            "action,path,path_dir,path_abs,args_digest,ok,seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dsh", "dsh:session-unchecked-0001-aaaa", CID_B, "PJZ", "edit", "write",
             "p0/lonely.py", "p0", str((Path.cwd() / "p0" / "lonely.py").resolve()),
             "0123456789abcdef", 1, fish.now_utc()))
        self.conn.commit()
        board = self._checkin("PJZ", doing="我在场")
        lines = fish.render_unchecked_section(
            self.conn, board, self._presence(board))
        text = "\n".join(lines)
        self.assertIn("在动但未在本板登记", text)
        self.assertNotRegex(text, FULL_SESSION_RE.pattern, "人类面不许出现完整会话键")
        self.assertNotRegex(text, ULID_RE.pattern, "人类面不许出现 26 位 ULID")
        # 机器面保留完整键（`board_unchecked_sessions` 的返回）
        data = fish.board_unchecked_sessions(self.conn, board, self._presence(board))
        self.assertTrue(any(s.get("session_key", "").startswith("dsh:session-unchecked")
                            for s in data["sessions"]),
                        "机器面应保留完整会话键")


if __name__ == "__main__":
    unittest.main()
