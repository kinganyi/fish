#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统级修复 · 第二批 A-1：断链清理的判据（2026-11-21）。

## 这份测试守的是什么

用户拍板「全部清理，避免以后推送和存储垃圾内容」。而「清理」在 fish 里有个
硬约束：**`events` 是 append-only**（`ev_no_delete` 触发器），所以物理删除
既做不到、也不该做。正确做法是**正式作废**（`memory.retired` + `withdrawn`）。

于是判据必须证明四件事：

1. 作废后 `recall` 不再返回它们（`status='withdrawn'`）；
2. **重建之后仍然不返回**——这条最关键，它证明「作废」进了事件流而不只是改了投影；
3. 事件是**追加**的，不是删的（`events` 行数增加，且历史事件仍在）；
4. 全文索引清了（`memory_fts` 是独立表，不删的话 `MATCH` 仍会命中）。

## 变异体（去掉 → 必红）

· `purge_broken` 只 UPDATE 不写 `memory.retired` → 判据 2 红（rebuild 复活）
· `purge_broken` 不删 `memory_fts` → 判据 4 红
· `purge_broken` 物理删 `code_index` 行 → 判据 3 红（或者事件没了）
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402
import fish_purge  # noqa: E402


class _PurgeCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-purge-t-"))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, status) VALUES ('T','测试','active')")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, title, *, body="正文", make_broken=False):
        """写一条记忆；`make_broken=True` 时把正文文件删掉（模拟迁移丢失）。"""
        code, rel = fish.record_memory(
            self.conn, self.data, project="T", title=title, body=body)
        self.conn.commit()
        if make_broken:
            p = self.data / rel
            if p.exists():
                p.unlink()
        return code

    def _broken_active(self):
        """当前仍然 active 的断链数（**这是判据的核心读数**）。"""
        rows = self.conn.execute(
            "SELECT code, file_path FROM code_index WHERE status='active'").fetchall()
        return sum(1 for _, fp in rows if fp and not (self.data / fp).exists())


class PurgeTest(_PurgeCase):

    def test_scan_finds_only_broken(self):
        self._make("好的", body="有正文")
        bad = self._make("坏的", make_broken=True)
        found = fish_purge.scan_broken(self.conn, self.data)
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["codes"], [bad])

    def test_dry_run_writes_nothing(self):
        """**默认必须 dry-run**：作废不可逆，不能一句话就清了。"""
        self._make("坏的", make_broken=True)
        before_events = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        res = fish_purge.purge_broken(self.conn, self.data, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["purged"], 0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM events").fetchone()[0], before_events)
        self.assertEqual(self._broken_active(), 1, "dry-run 居然改了状态")

    def test_purge_marks_withdrawn(self):
        """J-A1a：作废后不再是 active。"""
        self._make("坏的", make_broken=True)
        res = fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        self.assertEqual(res["purged"], 1)
        self.assertEqual(self._broken_active(), 0, "作废后仍然是 active")

    def test_purge_appends_events_does_not_delete(self):
        """J-A1c：**事件是追加的**，不是删的。

        变异体：物理删 `code_index` 行而不写事件 → 这里仍然通过（事件数不变），
        但下面 `test_survives_rebuild` 会红——两条一起才守得住。
        """
        self._make("坏的", make_broken=True)
        before = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        after = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertGreater(after, before, "没有追加任何事件——作废没进事件流")
        n_retired = self.conn.execute(
            "SELECT count(*) FROM events WHERE type='memory.retired'").fetchone()[0]
        self.assertEqual(n_retired, 1)
        # 原始 memory.recorded 必须还在（append-only 的意义）
        n_rec = self.conn.execute(
            "SELECT count(*) FROM events WHERE type='memory.recorded'").fetchone()[0]
        self.assertEqual(n_rec, 1, "memory.recorded 被删了——那是在篡改历史")

    def test_survives_rebuild(self):
        """**J-A1b：重建之后仍然干净。这条是整套清理成立与否的试金石。**

        `rebuild_projection` 从 `memory.recorded` 重建 `code_index`；
        如果作废只改了投影、没进事件流，重建会把它复活成 `active`。

        变异体：`purge_broken` 只 UPDATE 不写 `memory.retired` → 本判据红。
        """
        self._make("坏的", make_broken=True)
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        self.assertEqual(self._broken_active(), 0, "作废后不干净")

        fish.rebuild_projection(self.conn, self.data)
        self.conn.commit()
        self.assertEqual(self._broken_active(), 0,
                         "重建把它复活了——作废没进事件流（只是改了投影）")

    def test_fts_entries_removed(self):
        """J-A1d：全文索引必须清（它是独立虚表，不删的话 MATCH 仍会命中）。

        ⚠️ 查询要用**单个 bigram 或英文词**，不能用整词：`memory_fts` 把中文按
        bigram 切分（标题"独一无二"存成 `独一 一无 无二`），`MATCH "独一无二"`
        永远 0 命中——我第一版就是这么写的，前置断言当场就红了。
        用英文词 `ftsuuid` 最稳（不会被切）。
        """
        code = self._make("标题ftsuuid", body="正文内容", make_broken=True)
        hit_before = self.conn.execute(
            "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
            ('"ftsuuid"',)).fetchone()[0]
        self.assertEqual(hit_before, 1, "前置：FTS 里本该有它")
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        hit_after = self.conn.execute(
            "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
            ('"ftsuuid"',)).fetchone()[0]
        self.assertEqual(hit_after, 0, f"FTS 没清，仍能搜到 {code}")

    def test_good_memories_untouched(self):
        """**反向断言**：清理只动断链，不许误伤有正文的。

        变异体：去掉 exists 判断（全清）→ 红。
        """
        good = self._make("好的", body="有正文")
        self._make("坏的", make_broken=True)
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        row = self.conn.execute(
            "SELECT status FROM code_index WHERE code=?", (good,)).fetchone()
        self.assertEqual(row[0], "active", "有正文的记忆被误作废了")


class ReportTest(_PurgeCase):
    """A-3：一致性巡检必须给出**结构**，不只是一个数字。"""

    def test_report_has_date_structure(self):
        """断链的日期分布是定位根因的关键（85 号调查里正是靠它看出来的）。"""
        self._make("坏的", make_broken=True)
        rep = fish_purge.consistency_report(self.conn, self.data)
        self.assertEqual(rep["broken"], 1)
        self.assertIn("broken_by_date", rep)
        self.assertEqual(sum(rep["broken_by_date"].values()), 1,
                         "按日期分组的计数和总数对不上")
        self.assertIn("broken_by_project", rep)


class BoardPostFullTest(unittest.TestCase):
    """B-1：`board_posts.text` 必须存全文，**渲染时**才截断。

    ## 这条判据守的是什么

    旧实现在写入时就截断（`text[:199] + "…"`），而表定义与常量注释都写着
    「渲染时硬截断」——**两处注释在说谎**。后果是原文永久丢失：
    czhy--开发-3 读到一条只剩 200 字的 note，作者写的三项内容全没了，
    而且无处可查。

    ## 变异体（去掉 → 必红）

    · `board_post` 里恢复 `text = text[:199] + "…"` → 判据 1 红
    · `board_post_rows` 把 `display_text` 去掉 → 判据 2 红
    """

    def setUp(self):
        import fish_board
        self.fb = fish_board
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-b1-"))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.board = fish_board.board_ensure(self.conn, scope="project", scope_key="X")

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _long(self, n=600):
        return "基线372afade 分支清理结果：" + "很长内容" * (n // 4)

    def test_db_stores_full_text(self):
        """**库里必须是全文**（判据 1）。

        变异体：把 `text = text[:199] + "…"` 加回 `board_post` → 红。
        """
        text = self._long()
        pid = self.fb.board_post(self.conn, self.board, "C1", "note", text)
        self.conn.commit()
        stored = self.conn.execute(
            "SELECT text FROM board_posts WHERE id=?", (pid,)).fetchone()[0]
        self.assertEqual(len(stored), len(text),
                         f"库里只有 {len(stored)} 字符（原文 {len(text)}）——写入时被截了")
        self.assertFalse(stored.endswith("…"), "库里存的是截断版（以省略号结尾）")

    def test_render_is_truncated(self):
        """**渲染时截断**（判据 2）。

        变异体：`board_post_rows` 去掉 `display_text` → 红。
        """
        text = self._long()
        self.fb.board_post(self.conn, self.board, "C1", "note", text)
        self.conn.commit()
        rows = self.fb.board_post_rows(self.conn, self.board)
        shown = rows[0][2]
        self.assertLessEqual(len(shown), self.fb.BOARD_MAX_POST_CHARS,
                             f"渲染没截断：{len(shown)} 字符")
        self.assertTrue(shown.endswith("…"), "截断了但没标省略号")
        self.assertNotEqual(shown, text, "渲染返回了全文（显示层没截）")

    def test_full_reader_returns_original(self):
        """能取回全文——否则「库里有」和「没有」对用户没区别。"""
        text = self._long()
        pid = self.fb.board_post(self.conn, self.board, "C1", "note", text)
        self.conn.commit()
        rec = self.fb.board_post_full(self.conn, pid)
        self.assertEqual(rec["text"], text)
        self.assertTrue(rec["truncated_for_display"])
        self.assertEqual(rec["chars"], len(text))

    def test_short_text_unaffected(self):
        """**反向断言**：短文本不该被动手脚（避免"一律截断"的过度修正）。"""
        pid = self.fb.board_post(self.conn, self.board, "C1", "note", "短消息")
        self.conn.commit()
        stored = self.conn.execute(
            "SELECT text FROM board_posts WHERE id=?", (pid,)).fetchone()[0]
        self.assertEqual(stored, "短消息")
        self.assertEqual(self.fb.board_post_rows(self.conn, self.board)[0][2], "短消息")
        self.assertFalse(self.fb.board_post_full(self.conn, pid)["truncated_for_display"])


class KbVaultAvailabilityTest(unittest.TestCase):
    """E-1 / E-2：知识库的可用性必须**按磁盘现算**，不能只看 `status` 字段。

    ## 这一组守的是什么

    `kb_vault.status` 是**写一次就永远 active** 的字段。实测 `安逸知识库` 的 path
    指向 `p0/data/kb/安逸知识库`（目录不存在），而 status 还是 active ——于是：

      · `kb tree` / `kb search` 恒报离线（用户和 czhy--开发-3 都撞到过）；
      · **612 条 kb 推送候选全是从打不开的库产出的（accepted 0）**。

    ## 变异体（去掉 → 必红）

    · `kb_vault_availability` 改成 `return "ok"`（不看磁盘）→ 判据 1 红
    · `_kb_push_candidates_index` 去掉 usable 过滤 → 判据 3 红
    · `kb_vault_fix_path` 去掉 `dst.is_dir()` 检查 → 判据 4 红
    """

    def setUp(self):
        import fish_kb_index
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-e1-"))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        fish_kb_index.ensure_kb_index(self.conn)
        self.missing = self.tmp / "nope"
        self.conn.execute(
            "INSERT INTO kb_vault (vault_id, name, path, path_key, mode, status, "
            "is_default, created_at, updated_at) VALUES ('V1','测试库',?,'v1','ro','active',"
            "1,'2026-11-21T00:00:00Z','2026-11-21T00:00:00Z')", (str(self.missing),))
        # ⚠️ 造一条 active 的 kb_index 行：`kb_index_fresh` 要求 `active_rows > 0`，
        # 否则它会判 fresh=False 直接早退——那样 `_kb_push_candidates_index` 永远
        # 在 K7 那道闸就返回空，usable 过滤根本没被走到（第一版判据就是这么假绿的）。
        self.conn.execute(
            "INSERT INTO kb_index (vault_id, rel_path, mtime, size, content_hash, "
            "status, indexed_at) VALUES ('V1','a.md',0.0,10,'h1','active',"
            "'2099-01-01T00:00:00Z')")
        # ⚠️ 还必须造 `kb_fts` 行：`search_kb` 查的是这张虚表，没有它的话
        # 命中恒为空——那样"去掉可用性过滤"也不会多出候选，判据就是假绿的。
        self.conn.execute(
            "INSERT INTO kb_fts (vault_id, rel_path, title, body_excerpt) "
            "VALUES ('V1','a.md','测试标题','hello 的内容')")
        self.conn.commit()
        self._fts_ready = True

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _vault(self):
        import fish_kb
        return fish_kb.kb_vault_list(self.conn)[0]

    def test_availability_follows_disk_not_status(self):
        """判据 1：**status 是 active 但目录不存在 → 必须是 missing**。

        变异体：`return "ok"` → 红。
        """
        import fish_kb
        v = self._vault()
        self.assertEqual(v.get("status"), "active", "前置：status 字段是 active")
        self.assertFalse(Path(v["path"]).exists(), "前置：目录确实不存在")
        self.assertEqual(fish_kb.kb_vault_availability(v), "missing",
                         "只看 status 不看磁盘 —— 这就是 612 条零采纳推送的根因")

    def test_availability_ok_when_dir_exists(self):
        """反向断言：目录真在时必须是 ok（不能一律报 missing）。"""
        import fish_kb
        real = self.tmp / "real"
        real.mkdir()
        self.conn.execute("UPDATE kb_vault SET path=?, path_key=? WHERE vault_id='V1'",
                          (str(real), str(real).lower()))
        self.conn.commit()
        self.assertEqual(fish_kb.kb_vault_availability(self._vault()), "ok")

    def test_push_skips_entries_from_missing_vault(self):
        """判据 3：vault 打不开时**不产候选**。

        变异体：去掉 usable 过滤 → 红。
        """
        import fish_push
        out = fish_push._kb_push_candidates_index(
            self.conn, "hello", project=None, exclude=(), limit=2, now=None)
        self.assertEqual(out, [],
                         f"目录不存在却产出了 {len(out)} 条候选——推的是打不开的东西")

    def test_fix_path_refuses_nonexistent_target(self):
        """判据 4：改登记时**新路径必须真的存在**，否则只是从一个坏的换成另一个。

        变异体：去掉 `if not dst.is_dir(): raise` → 红。
        """
        import fish_kb
        with self.assertRaises(fish_kb.KBError):
            fish_kb.kb_vault_fix_path(
                self.conn, None, str(self.tmp / "also-nope"), dry_run=False)

    def test_fix_path_updates_and_is_auditable(self):
        """修正必须改登记 **且** 进事件流（半年后能查"为什么变了"）。"""
        import fish_kb
        real = self.tmp / "real"
        real.mkdir()
        before = self.conn.execute(
            "SELECT count(*) FROM events WHERE type='kb.vault_path_fixed'").fetchone()[0]
        fish_kb.kb_vault_fix_path(self.conn, None, str(real), dry_run=False,
                                  note="测试")
        after = self.conn.execute(
            "SELECT count(*) FROM events WHERE type='kb.vault_path_fixed'").fetchone()[0]
        self.assertEqual(after, before + 1, "改登记没有留事件")
        self.assertEqual(fish_kb.kb_vault_availability(self._vault()), "ok")

    def test_fix_path_dry_run_writes_nothing(self):
        """默认 dry-run：改登记会改变所有 kb 检索的落点，不能一句话就改。"""
        import fish_kb
        real = self.tmp / "real"
        real.mkdir()
        r = fish_kb.kb_vault_fix_path(self.conn, None, str(real), dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(self._vault()["path"], str(self.missing), "dry-run 改了库")


class BrokenVisibilityTest(_PurgeCase):
    """A-2 / A-3：断链必须**被看见**（消费侧标记 + 体检结构）。

    ## 为什么「清理了」还不够

    清理（`purge_broken`）处理的是**存量**（318 条）。而断链会**继续产生**：
    磁盘故障、手工删文件、又一次迁移。所以消费侧那道闸不能省——
    它是"防止 AI 引用空记忆"的最后一道。

    ## 变异体（去掉 → 必红）

    · `tool_recall` 里去掉 `⚠无正文` 标记 → 判据 1 红
    · 标记改成无条件加（不判 exists）→ 判据 2（反向断言）红
    · `cmd_healthcheck` 改回扫所有行（含已作废）→ 判据 3 红
    """

    def _recall(self):
        import mcp_server

        class _Id:
            status = "valid"
            citizen_id = "C1"
            evidence = "machine"
            session_key = "dsh:t"

        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, kind, created_at) "
            "VALUES ('C1','t','role','2026-11-21T00:00:00Z')")
        self.conn.commit()
        srv = object.__new__(mcp_server.FishServer)
        srv.conn = self.conn
        srv.root = self.data
        return srv.tool_recall({"query": "T-", "limit": 10}, _Id())

    def test_recall_marks_broken(self):
        """判据 1：断链条目必须带 `⚠无正文`。"""
        self._make("有正文的", body="x")
        self._make("会断链的", body="y", make_broken=True)
        import fish
        fish.ensure_fts(self.conn, self.data)
        out = self._recall()
        self.assertIn("⚠无正文", out, "断链没被标记——那正是「点进去是空的」的形态")

    def test_recall_does_not_mark_healthy(self):
        """判据 2（反向断言）：有正文的**不许**被标记。

        变异体：去掉 exists 判断 → 全标 → 红。
        """
        self._make("有正文的", body="x")
        import fish
        fish.ensure_fts(self.conn, self.data)
        out = self._recall()
        self.assertNotIn("⚠无正文", out, "有正文的条目被误标了")

    def test_healthcheck_scans_only_active(self):
        """判据 3：体检的断链读数**只算 active**。

        已作废的正文缺失不算断链——第一版扫所有行，于是"清理完还报 318 条"，
        那个读数会让"清理干净了吗"永远为假。
        """
        code = self._make("坏的", make_broken=True)
        import fish
        import fish_purge
        before = fish_purge.scan_broken(self.conn, self.data)["count"]
        self.assertEqual(before, 1, "前置：作废前应算 1 条断链")
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        after = fish_purge.scan_broken(self.conn, self.data)["count"]
        self.assertEqual(after, 0, f"作废后仍算 {after} 条断链")

    def test_healthcheck_reports_date_structure(self):
        """判据 4：体检必须给出**日期结构**（定位根因的关键）。
        """
        self._make("坏的", make_broken=True)
        import fish_purge
        rep = fish_purge.consistency_report(self.conn, self.data)
        self.assertIn("broken_by_date", rep)
        self.assertEqual(sum(rep["broken_by_date"].values()), 1)


if __name__ == "__main__":
    unittest.main()
