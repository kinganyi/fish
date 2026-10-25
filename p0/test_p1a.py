#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish P1a 验收测试
=================
覆盖 v0.3 新增：config / neighbors 四策略 / 访问记录 / typed decay / citizens
纪律：每条判据都配"破坏后应变红"的自证。
"""
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fish  # noqa: E402
import daily_report  # noqa: E402


def iso_days_ago(days: int) -> str:
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class FishP1aTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-p1a-")
        self.root = os.path.join(self.tmp, "data")
        os.makedirs(self.root, exist_ok=True)
        conn = fish.connect(fish.Path(self.root))
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) VALUES (?,?,?,?,?)",
            ("TEST", "t", "", 0, "active"))
        conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) VALUES (?,?,?,?,?)",
            ("OTHR", "o", "", 0, "active"))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self):
        return fish.connect(fish.Path(self.root))

    def _add(self, conn, code, title, project, mtype="fact", tags=None,
             occurred=None, recorded=None, dormant=0):
        conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,tags,"
            "file_path,occurred_at,recorded_at,access_count,dormant) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (code, title, project, "u", mtype, "shared", "active",
             json.dumps(tags or [], ensure_ascii=False), "", occurred or fish.now_utc(),
             recorded or fish.now_utc(), dormant))

    # ------------------------------------------------------------ 配置

    def test_config_defaults_and_set(self):
        conn = self._conn()
        self.assertEqual(fish.get_config(conn, "neighbors.strategy"), "mixed")
        self.assertEqual(fish.get_config(conn, "neighbors.n"), 3)
        self.assertFalse(fish.get_config(conn, "neighbors.cross_project"))
        # 设置生效
        fish.set_config(conn, "neighbors.n", 5)
        self.assertEqual(fish.get_config(conn, "neighbors.n"), 5)
        # 写入了 config.changed 事件
        n = conn.execute("SELECT COUNT(*) FROM events WHERE type='config.changed'").fetchone()[0]
        self.assertGreaterEqual(n, 1)
        # 反例：未知键必须报错
        with self.assertRaises(KeyError):
            fish.get_config(conn, "no.such.key")
        # 反例：枚举值非法必须报错
        with self.assertRaises(ValueError):
            fish.set_config(conn, "neighbors.strategy", "bogus")
        conn.close()

    # ------------------------------------------------------------ 邻近

    def test_neighbors_strategies(self):
        conn = self._conn()
        d = fish.local_date()
        self._add(conn, f"TEST-{d}-01", "a", "TEST", "fact", ["x"])
        self._add(conn, f"TEST-{d}-02", "b", "TEST", "lesson", ["x"])
        self._add(conn, f"TEST-{d}-03", "c", "TEST", "fact", ["x", "y"])
        self._add(conn, f"OTHR-{d}-01", "z", "OTHR", "fact", ["x"])
        conn.commit()
        base = f"TEST-{d}-02"

        r_code = fish.neighbors(conn, base, n=2, strategy="code")
        self.assertEqual(r_code["strategy"], "code")
        self.assertTrue(all(c["project"] == "TEST" for c in r_code["prev"] + r_code["next"]))

        r_type = fish.neighbors(conn, base, n=2, strategy="type")
        self.assertTrue(all(c["mem_type"] == "lesson" for c in r_type["hits"]))
        # 反例自证：基线是 lesson，fact 不该出现在 type 策略里
        self.assertFalse(any(c["mem_type"] != "lesson" for c in r_type["hits"]))

        r_tag = fish.neighbors(conn, base, n=2, strategy="tag")
        self.assertTrue(all("x" in json.loads(c["tags"]) for c in r_tag["hits"]))

        r_mixed = fish.neighbors(conn, base, n=3, strategy="mixed")
        self.assertEqual(r_mixed["strategy"], "mixed")
        self.assertTrue(all("score" in h for h in r_mixed["hits"]))
        # 同项目的分应高于跨项目
        scores = {h["code"]: h["score"] for h in r_mixed["hits"]}
        if f"OTHR-{d}-01" in scores:
            self.assertGreater(scores[f"TEST-{d}-01"], scores[f"OTHR-{d}-01"])
        conn.close()

    def test_neighbors_excludes_self_and_respects_dormant(self):
        conn = self._conn()
        d = fish.local_date()
        self._add(conn, f"TEST-{d}-01", "a", "TEST")
        self._add(conn, f"TEST-{d}-02", "b", "TEST", dormant=1)
        conn.commit()
        res = fish.neighbors(conn, f"TEST-{d}-01", n=5, strategy="code", include_dormant=False)
        codes = [c["code"] for c in res["prev"] + res["next"]]
        self.assertNotIn(f"TEST-{d}-01", codes, "不能包含自己")
        self.assertNotIn(f"TEST-{d}-02", codes, "休眠默认不出现")
        res2 = fish.neighbors(conn, f"TEST-{d}-01", n=5, strategy="code", include_dormant=True)
        codes2 = [c["code"] for c in res2["prev"] + res2["next"]]
        self.assertIn(f"TEST-{d}-02", codes2, "显式要求时应出现")
        conn.close()

    # ------------------------------------------------------------ 访问记录

    def test_recall_updates_access(self):
        conn = self._conn()
        d = fish.local_date()
        self._add(conn, f"TEST-{d}-01", "可检索标题", "TEST")
        conn.commit()
        conn.close()
        # 走 CLI 层：直接调用 cmd_recall
        ns = type("A", (), {"root": self.root, "query": f"TEST-{d}-01", "limit": 10})()
        fish.cmd_recall(ns)
        conn = self._conn()
        la, ac, dm = conn.execute(
            "SELECT last_accessed_at, access_count, dormant FROM code_index WHERE code=?",
            (f"TEST-{d}-01",)).fetchone()
        self.assertIsNotNone(la, "召回后应记录访问时间")
        self.assertEqual(ac, 1, "召回后计数应 +1")
        self.assertEqual(dm, 0, "被想起应唤醒")
        conn.close()

    # ------------------------------------------------------------ 衰减

    def test_typed_decay(self):
        # rule 永不衰减
        d1, s1 = fish.compute_decay("rule", None, iso_days_ago(3650))
        self.assertEqual(d1, 1.0)
        self.assertEqual(s1, "active")
        # episode 30 天半衰期：30 天前 decay≈0.5 → 边界；45 天≈0.35 → dormant
        d2, s2 = fish.compute_decay("episode", None, iso_days_ago(45))
        self.assertLess(d2, 0.5)
        self.assertEqual(s2, "dormant")
        # episode 120 天 → archived
        d3, s3 = fish.compute_decay("episode", None, iso_days_ago(120))
        self.assertLessEqual(d3, 0.1)
        self.assertEqual(s3, "archived")
        # 反例自证：把 recorded_at 换成现在，必须回到 active
        d4, s4 = fish.compute_decay("episode", None, fish.now_utc())
        self.assertEqual(s4, "active")
        self.assertAlmostEqual(d4, 1.0, places=2)

    def test_forgetting_apply(self):
        conn = self._conn()
        d = fish.local_date()
        self._add(conn, f"TEST-{d}-01", "老记忆", "TEST", "episode",
                  occurred=iso_days_ago(60), recorded=iso_days_ago(60))
        conn.commit()
        conn.close()
        ns = type("A", (), {"root": self.root, "apply": True})()
        fish.cmd_forgetting(ns)
        conn = self._conn()
        dm = conn.execute("SELECT dormant FROM code_index WHERE code=?",
                          (f"TEST-{d}-01",)).fetchone()[0]
        self.assertEqual(dm, 1, "应被标记休眠")
        ev = conn.execute("SELECT COUNT(*) FROM events WHERE type='memory.dormant'").fetchone()[0]
        self.assertGreaterEqual(ev, 1, "应写入 memory.dormant 事件")
        conn.close()

    # ------------------------------------------------------------ 公民

    def test_citizen_kinds(self):
        conn = self._conn()
        for kind in fish.KINDS:
            cid = fish.ulid()
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
                "VALUES (?,?,?,?,?,?,?)", (cid, f"x-{kind}", kind, None, None, "active", fish.now_utc()))
        conn.commit()
        rows = conn.execute("SELECT kind, COUNT(*) FROM citizens GROUP BY kind").fetchall()
        self.assertEqual(len(rows), 4, "四种存在形态都应可注册")
        # 反例：非法 kind 不应进入（CLI 层拦截）
        conn.close()
        ns = type("A", (), {"root": self.root, "name": "bad", "kind": "god",
                            "role": None, "host": None})()
        fish.cmd_citizen_add(ns)
        conn = self._conn()
        n = conn.execute("SELECT COUNT(*) FROM citizens WHERE kind='god'").fetchone()[0]
        self.assertEqual(n, 0, "非法 kind 必须被拒")
        conn.close()

    def test_owner_must_be_citizen(self):
        """owner 必须是有效公民 ID：缺省归系统主人；旧默认值归一；乱填必须报错（07 D-13）。"""
        conn = self._conn()
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,is_owner) "
            "VALUES ('OWNER1','主人','resident','active',?,1)", (fish.now_utc(),))
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at) "
            "VALUES ('DEV001','开发AI','visitor','active',?)", (fish.now_utc(),))
        conn.commit()

        # 1) 缺省（None / 空）→ 系统主人
        self.assertEqual(fish.resolve_owner_arg(conn, None), "OWNER1")
        self.assertEqual(fish.resolve_owner_arg(conn, ""), "OWNER1")
        # 2) 旧默认值 human-anyi 从来不是公民 ID → 归到系统主人，不写幽灵 ID
        self.assertEqual(fish.resolve_owner_arg(conn, "human-anyi"), "OWNER1")
        # 3) 有效公民 ID → 原样
        self.assertEqual(fish.resolve_owner_arg(conn, "DEV001"), "DEV001")
        # 4) 乱填 → 报错，且必须给下一步（03 §G「失败要给下一步」）
        with self.assertRaises(ValueError) as ctx:
            fish.resolve_owner_arg(conn, "随便写的名字")
        msg = str(ctx.exception)
        self.assertIn("必须是公民 ID", msg)
        self.assertIn("OWNER1", msg, "报错必须列出可用公民")
        self.assertIn("fish citizen-list", msg, "报错必须给下一步")

        # 5) 端到端：CLI remember 不传 --owner 时，落库的必须是真公民 ID
        conn.close()
        ns = type("A", (), {"root": self.root, "project": "TEST", "title": "无 owner",
                            "body": "", "type": "fact", "tags": [], "visibility": "shared",
                            "owner": None, "session_actor": None, "attach": [],
                            "cause": None})()
        fish.cmd_remember(ns)
        conn = self._conn()
        row = conn.execute("SELECT owner FROM code_index WHERE title='无 owner'").fetchone()
        self.assertEqual(row[0], "OWNER1", "CLI 缺省必须落到系统主人，而不是 human-anyi")
        conn.close()

    def test_occurred_at_backdating(self):
        """occurred_at 可回填过去的事，且必须同时进投影与事件；非法格式要在落库前被拒。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        past = "2026-09-01T03:00:00Z"
        code, _ = fish.record_memory(conn, fish.Path(self.root), project="TEST",
                                     title="补记过去", occurred_at=past)
        row = conn.execute("SELECT occurred_at FROM code_index WHERE code=?", (code,)).fetchone()
        self.assertEqual(row[0], past, "投影要记 occurred_at")
        p = json.loads(conn.execute(
            "SELECT payload FROM events WHERE type='memory.recorded'").fetchone()[0])
        self.assertEqual(p["occurred_at"], past, "事件也要记 occurred_at（否则重建不忠实）")

        # 反例：非法格式必须被拒，且**不得留下空号或孤儿文件**
        reg_before = conn.execute(
            "SELECT last_seq FROM code_registry WHERE prefix='TEST'").fetchone()[0]
        proj_before = len(list((fish.Path(self.root) / "projects" / "TEST").glob("*.md")))
        with self.assertRaises(ValueError):
            fish.record_memory(conn, fish.Path(self.root), project="TEST",
                               title="坏时间", occurred_at="2026年9月1日")
        reg_after = conn.execute(
            "SELECT last_seq FROM code_registry WHERE prefix='TEST'").fetchone()[0]
        proj_after = len(list((fish.Path(self.root) / "projects" / "TEST").glob("*.md")))
        self.assertEqual(reg_before, reg_after, "非法参数不得消耗编码序号")
        self.assertEqual(proj_before, proj_after, "非法参数不得留下孤儿文件")
        n = conn.execute("SELECT COUNT(*) FROM code_index WHERE title='坏时间'").fetchone()[0]
        self.assertEqual(n, 0, "非法 occurred_at 不得落库")
        conn.close()

    def test_access_has_event_source(self):
        """访问状态要有事件来源：每次访问留事件，投影被清空也能重建回来（07 D-12）。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        code, _ = fish.record_memory(conn, fish.Path(self.root), project="TEST",
                                     title="会被访问的记忆")
        fish.record_access(conn, [code], "recall")
        fish.record_access(conn, [code], "recall")
        ev = conn.execute("SELECT COUNT(*) FROM events WHERE type='memory.accessed'").fetchone()[0]
        self.assertEqual(ev, 2, "每次访问必须留一笔事件")
        self.assertEqual(
            conn.execute("SELECT access_count FROM code_index WHERE code=?",
                         (code,)).fetchone()[0], 2, "counted 访问要累加")

        # 模拟灾难：投影被清空，且没有快照可用
        conn.execute("DELETE FROM code_index")
        conn.commit()
        rep = fish.rebuild_projection(conn, fish.Path(self.root))
        row = conn.execute("SELECT access_count,last_accessed_at FROM code_index WHERE code=?",
                           (code,)).fetchone()
        self.assertEqual(row[0], 2, "没有快照时，访问次数必须能从事件重放出来")
        self.assertIsNotNone(row[1], "last_accessed_at 必须能从事件恢复")
        self.assertEqual(rep["access_from_events"], 2)

        # 反例自证：counted=False（被看见/读正文）不累加，但仍留事件
        fish.record_access(conn, [code], "inject", counted=False)
        self.assertEqual(
            conn.execute("SELECT access_count FROM code_index WHERE code=?",
                         (code,)).fetchone()[0], 2, "被看见不算主动查询")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM events WHERE type='memory.accessed'"
                         ).fetchone()[0], 3, "counted=False 也要留事件")
        conn.close()

    def test_time_strategy_stable_when_same_timestamp(self):
        """回归：同一秒内的多条记录，time 策略必须按编码定序，前后不重叠。"""
        conn = self._conn()
        d = fish.local_date()
        same = fish.now_utc()
        for i, t in ((1, "a"), (2, "b"), (3, "c")):
            self._add(conn, f"TEST-{d}-{i:02d}", t, "TEST", occurred=same)
        conn.commit()
        res = fish.neighbors(conn, f"TEST-{d}-02", n=5, strategy="time")
        prev = [c["code"] for c in res["prev"]]
        nxt = [c["code"] for c in res["next"]]
        self.assertIn(f"TEST-{d}-01", prev, "同秒内更小编码应在 prev")
        self.assertIn(f"TEST-{d}-03", nxt, "同秒内更大编码应在 next")
        self.assertFalse(set(prev) & set(nxt), "前后不能重叠")
        conn.close()


    # ------------------------------------------------------------ 注入包

    def test_injection_pack_basic(self):
        conn = self._conn()
        cid = "01CITIZEN0000000000000001"
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, "测试居民", "resident", "owner", None, "active", fish.now_utc()))
        d = fish.local_date()
        for i in range(1, 6):
            self._add(conn, f"TEST-{d}-{i:02d}", f"条目{i}", "TEST", "fact", ["tagA", "tagB"])
        conn.commit()

        pack, meta = fish.build_injection_pack(conn, fish.Path(self.root), cid)
        self.assertIn("测试居民", pack)
        self.assertIn(cid, pack)
        self.assertIn("TEST", pack)
        self.assertLessEqual(meta["tokens"], meta["budget"] + 5)
        self.assertFalse(meta["over_budget"])
        self.assertGreater(meta["layers"]["L0"], 0)
        conn.close()

    def test_injection_over_budget_is_loud(self):
        """最小注入包仍超预算时，必须报告 over_budget，且身份行永不舍弃。"""
        conn = self._conn()
        cid = "01CITIZEN0000000000000002"
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, "小预算居民", "visitor", None, None, "active", fish.now_utc()))
        conn.commit()
        pack, meta = fish.build_injection_pack(conn, fish.Path(self.root), cid, budget=10)
        self.assertTrue(meta["over_budget"], "超预算必须响亮报告，不能假装成功")
        self.assertIn(cid, pack, "身份行永不被裁剪")
        self.assertTrue(any("仍超预算" in t for t in meta["trimmed"]))
        conn.close()

    def test_injection_trim_order(self):
        """裁剪顺序：先 L2，再 L1 条目。"""
        conn = self._conn()
        cid = "01CITIZEN0000000000000003"
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, "裁剪测试", "resident", None, None, "active", fish.now_utc()))
        d = fish.local_date()
        for i in range(1, 4):
            self._add(conn, f"TEST-{d}-{i:02d}", f"条目{i}", "TEST", "fact", ["t"])
        conn.commit()
        _, meta = fish.build_injection_pack(conn, fish.Path(self.root), cid, budget=120)
        self.assertIn("L2", meta["trimmed"], "应先裁 L2")
        conn.close()


    # ------------------------------------------------------------ 旧库迁移

    def test_legacy_db_migration_owner(self):
        """回归：旧库 citizens 无 is_owner 列时，migrate 必须能补列、建索引、回填主人。

        这个 bug 真实发生过：SCHEMA 里的建索引语句先于 migrate 的加列执行，
        旧库直接报 no such column: is_owner。新库测不出来，必须专门造旧库。
        """
        import sqlite3
        from pathlib import Path as _P
        root = _P(self.tmp) / "legacyroot"
        root.mkdir()
        db = root / "index.sqlite"
        c = sqlite3.connect(str(db))
        c.executescript("""
CREATE TABLE citizens (citizen_id TEXT PRIMARY KEY, display_name TEXT, kind TEXT,
  role TEXT, host TEXT, status TEXT, created_at TEXT);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, type TEXT, updated_at TEXT);
INSERT INTO citizens (citizen_id,display_name,kind,status,created_at)
  VALUES ('C1','旧主人','resident','active','2026-01-01');
INSERT INTO config (key,value,type,updated_at)
  VALUES ('default_citizen','C1','string','2026-01-01');
""")
        c.commit()
        c.close()

        conn = fish.connect(root)
        conn.executescript(fish.SCHEMA)   # 旧表不会被 IF NOT EXISTS 改动
        fish.migrate(conn)                # 必须在这里补列 + 建索引 + 回填

        cols = {r[1] for r in conn.execute("PRAGMA table_info(citizens)")}
        self.assertIn("is_owner", cols, "迁移必须补上 is_owner 列")
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("ux_citizens_owner", idx, "迁移必须创建唯一索引")
        row = conn.execute("SELECT citizen_id FROM citizens WHERE is_owner=1").fetchone()
        self.assertIsNotNone(row, "owner 必须从旧 config 回填")
        self.assertEqual(row[0], "C1")

        # 唯一性：第二个 is_owner=1 必须被数据库拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,is_owner) "
                "VALUES ('C2','二号','resident','active','2026-01-02',1)")
        conn.rollback()
        conn.close()


    # ------------------------------------------------------------ 附件 + 会话身份

    def test_attachments_content_addressed(self):
        """附件按 SHA256 内容寻址：去重、可校验、正文有引用、索引有元数据。"""
        import hashlib
        from pathlib import Path
        img = Path(self.tmp) / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-image-data" * 20)

        conn = fish.connect(fish.Path(self.root))
        conn.executescript(fish.SCHEMA)
        code, rel = fish.record_memory(
            conn, Path(self.root), project="TEST", title="带附件的记忆", body="正文",
            mem_type="artifact", attachments=[{"path": str(img), "caption": "测试图"}])
        conn.commit()

        # 1. blob 落盘，文件名 = 内容 sha256
        files = [b for b in (Path(self.root) / "blobs").rglob("*") if b.is_file()]
        self.assertEqual(len(files), 1, "应只有一个 blob 文件")
        h = hashlib.sha256(img.read_bytes()).hexdigest()
        self.assertEqual(files[0].stem, h, "文件名必须是内容的 sha256")
        self.assertEqual(files[0].read_bytes(), img.read_bytes(), "内容逐字节一致")

        # 2. 正文有引用
        md = (Path(self.root) / rel).read_text(encoding="utf-8")
        self.assertIn("## 附件", md)
        self.assertIn("pic.png", md)
        self.assertIn(h[:12], md)
        self.assertIn("测试图", md)

        # 3. 同一文件再记一次 → 不产生第二个 blob
        fish.record_memory(conn, Path(self.root), project="TEST", title="重复附件",
                           mem_type="artifact", attachments=[str(img)])
        files2 = [b for b in (Path(self.root) / "blobs").rglob("*") if b.is_file()]
        self.assertEqual(len(files2), 1, "相同内容不得重复存储（去重）")

        # 4. 索引里有可查询的附件元数据
        row = conn.execute("SELECT attachments FROM code_index WHERE code=?", (code,)).fetchone()
        meta = json.loads(row[0])
        self.assertEqual(meta[0]["sha256"], h)
        self.assertEqual(meta[0]["caption"], "测试图")
        conn.close()

    def test_session_actor_recorded(self):
        """会话身份与宿主身份分开存：owner=宿主，session_actor=会话。"""
        from pathlib import Path
        conn = fish.connect(Path(self.root))
        conn.executescript(fish.SCHEMA)
        code, _ = fish.record_memory(
            conn, Path(self.root), project="TEST", title="会话记录",
            owner="workbuddy-main", session_actor="anyi-dev")
        conn.commit()
        row = conn.execute("SELECT owner,session_actor FROM code_index WHERE code=?",
                           (code,)).fetchone()
        self.assertEqual(row[0], "workbuddy-main", "owner 仍是宿主身份")
        self.assertEqual(row[1], "anyi-dev", "session_actor 记录会话身份")
        # 反例：不传 session_actor 时为空，不能凭空编一个
        code2, _ = fish.record_memory(conn, Path(self.root), project="TEST",
                                      title="无会话", owner="workbuddy-main")
        conn.commit()
        row2 = conn.execute("SELECT session_actor FROM code_index WHERE code=?",
                            (code2,)).fetchone()
        self.assertIsNone(row2[0])
        conn.close()

    # ------------------------------------------------- supersede 回显（BUG-1）

    def test_supersede_echo_reports_actual_superseder(self):
        """对已 superseded 的条目再 supersede：回显必须报库内实际替代者，且声明本次未生效。

        背景（2026-10-22 反馈 ANYI-261022-34）：旧实现回显入参 `new_code`，
        后到的会话会得到「被 <我的入参> 替代过」的假成功。
        """
        conn = self._conn()
        for code, title in [("TEST-261022-01", "旧结论"),
                            ("TEST-261022-99", "第一替代者"),
                            ("TEST-261022-50", "后到会话的新结论")]:
            self._add(conn, code, title, "TEST")
        conn.commit()

        first = fish.supersede_memory(conn, "TEST-261022-01", "TEST-261022-99")
        self.assertIn("TEST-261022-99", first, "首次替代应成功并回显替代者")

        # 后到会话想把替代者改成自己的条目
        msg = fish.supersede_memory(conn, "TEST-261022-01", "TEST-261022-50")

        # 1) 回显的是库内实际替代者，不是入参
        self.assertIn("实际替代者是 TEST-261022-99", msg,
                      "回显必须是库内实际替代者（TEST-261022-99），不能回显入参")
        self.assertNotIn("被 TEST-261022-50 替代过", msg,
                         "不得出现旧实现的入参回显句式")
        # 2) 必须明确「本次未生效」，区分于「生效了」
        self.assertIn("未生效", msg, "必须声明本次调用未生效")
        # 3) 库内状态不得被第二次调用改写
        actual = conn.execute("SELECT superseded_by FROM code_index WHERE code=?",
                              ("TEST-261022-01",)).fetchone()[0]
        self.assertEqual(actual, "TEST-261022-99", "第二次调用不得改写替代者")
        # 4) 空操作不留痕：只应有 1 条 memory.superseded 事件
        n = conn.execute("SELECT COUNT(*) FROM events WHERE type='memory.superseded'"
                         ).fetchone()[0]
        self.assertEqual(n, 1, "未生效的调用不应追加事件")
        conn.close()

    def test_supersede_echo_judgement_can_go_red(self):
        """变异自证：把旧实现的回显字符串喂给上一条的判据，判据必须红。

        这条不测业务逻辑，测的是「判据有判别力」——否则上一条测试可能永远绿。
        """
        buggy = "TEST-261022-01 已经是 superseded（被 TEST-261022-50 替代过）"
        self.assertNotIn("实际替代者是 TEST-261022-99", buggy,
                         "旧回显里没有实际替代者 → 上一条判据应判红")
        self.assertIn("被 TEST-261022-50 替代过", buggy,
                      "旧回显把入参当事实 → 上一条判据应能抓到")
        self.assertNotIn("未生效", buggy,
                         "旧回显没有声明未生效 → 上一条判据应判红")

    # ------------------------------------------------------------ 备份自动化

    def test_backup_creates_consistent_snapshot(self):
        """备份：VACUUM INTO 出一致快照 + _meta.json + **真的做了 WAL checkpoint**。

        判据做强的前提：故意**不关闭**写入连接，让数据留在 WAL 里（实测 115 KB）。
        备份后 WAL 必须归零——若不调 checkpoint，它不会自己变空。
        """
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.execute("INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                     "VALUES ('T','t','',0,'active')")
        conn.commit()
        for i in range(2):
            fish.record_memory(conn, Path(self.root), project="T", title=f"备份{i}")
        db = Path(self.root) / "index.sqlite"
        wal = Path(str(db) + "-wal")
        try:
            self.assertGreater(wal.stat().st_size if wal.exists() else 0, 0,
                               "前置条件：数据应还在 WAL 里（连接未关）")

            bk = Path(self.tmp) / "backups"
            meta = daily_report.backup_database(db_path=db, backups_dir=bk, day="2026-10-22")

            dest = Path(meta["path"])
            self.assertTrue(dest.exists(), "备份文件必须存在")
            self.assertEqual(dest.name, "index.sqlite")
            self.assertEqual(dest.parent.name, "2026-10-22", "必须按天分目录")

            # 能独立打开 + 条数一致 + 完整性 ok
            c2 = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
            try:
                self.assertEqual(c2.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(c2.execute("SELECT COUNT(*) FROM code_index").fetchone()[0],
                                 meta["counts"]["memories"])
                self.assertEqual(c2.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                                 meta["counts"]["events"])
            finally:
                c2.close()

            # _meta.json 的四要素
            m = json.loads((dest.parent / "_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(m["sha256"], daily_report._sha256(dest),
                             "sha256 必须是备份文件本身的，不能是源库的")
            self.assertEqual(m["size_bytes"], dest.stat().st_size)
            self.assertEqual(m["counts"]["memories"], 2)
            self.assertIn("created_at", m)
            self.assertIn("busy", m["wal_checkpoint"], "checkpoint 结果必须如实记录")

            # checkpoint 真发生了：WAL 必须被截断（不调它不会自己变空）
            size = wal.stat().st_size if wal.exists() else 0
            self.assertEqual(size, 0, "wal_checkpoint(TRUNCATE) 之后 WAL 必须归零")
            self.assertEqual(m["wal_checkpoint"]["busy"], 0)
        finally:
            conn.close()

    def test_backup_is_idempotent_same_day(self):
        """同一天连跑两次：不报错、只有一个日期目录、无 .tmp 残留、meta 与文件一致。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        fish.record_memory(conn, Path(self.root), project="TEST", title="幂等")
        conn.close()

        bk = Path(self.tmp) / "backups"
        db = Path(self.root) / "index.sqlite"
        m1 = daily_report.backup_database(db_path=db, backups_dir=bk, day="2026-10-22")
        m2 = daily_report.backup_database(db_path=db, backups_dir=bk, day="2026-10-22")

        dirs = [p for p in bk.iterdir() if p.is_dir()]
        self.assertEqual(len(dirs), 1, "同一天只应有一个日期目录")
        self.assertEqual(sorted(p.name for p in dirs[0].iterdir()),
                         ["_meta.json", "index.sqlite"], "不得残留 .tmp 文件")
        m = json.loads((dirs[0] / "_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(m["sha256"], daily_report._sha256(Path(m2["path"])),
                         "meta 必须描述当前这份文件")
        self.assertEqual(m2["counts"]["memories"], m1["counts"]["memories"])

    def test_backup_prune_keeps_14_days(self):
        """保留最近 14 天：超期日期目录删掉；不是日期的目录一律不动。"""
        bk = Path(self.tmp) / "backups"
        for d in ("2026-09-01", "2026-10-08", "2026-10-09", "2026-10-22"):
            (bk / d).mkdir(parents=True)
        (bk / "手工放的").mkdir()
        removed = daily_report.prune_backups(backups_dir=bk, keep_days=14, day="2026-10-22")
        self.assertIn("2026-09-01", removed)
        self.assertIn("2026-10-08", removed)
        self.assertFalse((bk / "2026-09-01").exists(), "超期目录必须真的被删掉")
        self.assertNotIn("2026-10-09", removed, "第 14 天（含）必须保留")
        self.assertTrue((bk / "2026-10-09").exists())
        self.assertTrue((bk / "2026-10-22").exists())
        self.assertTrue((bk / "手工放的").exists(), "不是日期的目录不许动")


    # ------------------------------------------------------------ 全文检索（ADR-005）

    def _mk(self, conn, title, body, project="TEST", tags=None):
        code, _ = fish.record_memory(conn, Path(self.root), project=project,
                                     title=title, body=body, mem_type="fact",
                                     tags=tags or [])
        return code

    def test_recall_finds_chinese_substring(self):
        """中文子串可查：正文含「协作约定」的条目，recall("协作") 必须命中（ADR-005）。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        a = self._mk(conn, "规则 A", "协作约定 CR-1~15，含 CR-9/CR-12/CR-14")
        b = self._mk(conn, "无关条目", "支付回调重试逻辑")
        rows, _ = fish.search_memories(conn, query="协作")
        codes = [r[0] for r in rows]
        self.assertIn(a, codes, "正文里的中文子串必须能查到")
        self.assertNotIn(b, codes)
        # scope=title 时不应命中（「协作」只出现在正文里）
        rows_t, _ = fish.search_memories(conn, query="协作", scope="title")
        self.assertNotIn(a, [r[0] for r in rows_t], "scope=title 不得命中正文")
        # 匹配来源标注（回包要告诉 AI "为什么命中"）
        self.assertEqual({r[0]: r[7] for r in rows}.get(a), "正文")
        conn.close()

    def test_recall_finds_hyphenated_id(self):
        """单号可查且**不报错**：`CR-12` 裸写会让 FTS5 抛 no such column（ADR-005 背景 1）。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        a = self._mk(conn, "错题本", "PIT-062 sqlite 全绿掩盖 PG 崩溃")
        try:
            rows, _ = fish.search_memories(conn, query="PIT-062")
        except Exception as e:          # 转义失效时这里会炸
            self.fail(f"单号检索抛异常（查询转义失效）：{type(e).__name__}: {e}")
        self.assertIn(a, [r[0] for r in rows])
        conn.close()

    def test_recall_negative_control(self):
        """负对照：库里不存在的单号必须 0 条，不能全量兜底（ADR-005 判据）。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        self._mk(conn, "错题本", "PIT-062 sqlite 全绿掩盖 PG 崩溃")
        self.assertEqual(fish.search_memories(conn, query="PIT-099")[0], [],
                         "不存在的单号必须 0 条")
        self.assertEqual(fish.search_memories(conn, query="量子纠缠")[0], [])
        conn.close()

    def test_recall_project_and_tags_filter(self):
        """project / tags 过滤生效（关闭 07 D-5，回应 ANYI-261022-80 的规则分叉）。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        # setUp 已注册 TEST 与 OTHR 两个前缀，直接复用
        a = self._mk(conn, "协作约定", "CR-12 引用纪律", tags=["规范"])
        b = self._mk(conn, "协作约定", "CR-12 引用纪律", project="OTHR", tags=["规范"])
        codes = [r[0] for r in fish.search_memories(conn, query="CR-12", project="OTHR")[0]]
        self.assertIn(b, codes)
        self.assertNotIn(a, codes, "project 过滤必须生效")
        both = {r[0] for r in fish.search_memories(conn, query="CR-12", tags=["规范"])[0]}
        self.assertTrue({a, b} <= both, "tags 过滤必须生效")
        conn.close()

    def test_reindex_and_rebuild_keep_fts(self):
        """索引可重建：清空 memory_fts 后 ensure_fts 自愈；rebuild 后命中集不变（ADR-005）。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        a = self._mk(conn, "协作约定", "CR-12 引用纪律")
        before = [r[0] for r in fish.search_memories(conn, query="协作")[0]]
        self.assertIn(a, before)

        conn.execute("DELETE FROM memory_fts")
        conn.commit()
        self.assertEqual(fish.search_memories(conn, query="协作")[0], [],
                         "索引清空后应查不到（证明判据读的是索引，不是全表兜底）")
        self.assertGreater(fish.ensure_fts(conn, Path(self.root)), 0, "ensure_fts 必须自愈")
        self.assertIn(a, [r[0] for r in fish.search_memories(conn, query="协作")[0]])

        rep = fish.rebuild_projection(conn, Path(self.root))
        after = [r[0] for r in fish.search_memories(conn, query="协作")[0]]
        self.assertEqual(sorted(before), sorted(after), "rebuild 后命中集必须一致")
        self.assertGreaterEqual(rep["fts_indexed"], 1)
        conn.close()

    def test_recall_fallback_respects_project(self):
        """未命中时的回退列表必须遵守 project 过滤（2026-10-22 缺陷 ANYI-261022-114）。

        旧实现在 CLI 与 MCP 各写了一份 `ORDER BY code DESC LIMIT 5`，
        于是 `project=OTHR` 查一个不存在的词，会捞出 TEST 项目的条目。
        """
        import contextlib
        import io
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        a = self._mk(conn, "TEST 的条目", "内容甲")
        b = self._mk(conn, "OTHR 的条目", "内容乙", project="OTHR")
        conn.close()

        ns = type("A", (), {"root": self.root, "query": "绝对不存在的词ZZZ", "limit": 10,
                            "scope": "all", "code": None, "project": "OTHR",
                            "tags": []})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_recall(ns)
        out = buf.getvalue()
        self.assertIn(b, out, "回退列表应含本项目的条目")
        self.assertNotIn(a, out, "回退列表不得跨项目串台")

        # 不给 project 时才允许看全库
        ns.project = None
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            fish.cmd_recall(ns)
        self.assertIn(a, buf2.getvalue(), "不限定 project 时应能看到全库条目")


    def test_withdraw_memory(self):
        """作废：status → withdrawn + 写 memory.retired 事件 + 默认检索不再返回（07 D-6）。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        code = self._mk(conn, "误写条目", "内容")
        self.assertEqual(fish.search_memories(conn, query="误写")[0][0][0], code)

        msg = fish.withdraw_memory(conn, code, reason="写错项目")
        self.assertIn("withdrawn", msg)
        self.assertEqual(
            conn.execute("SELECT status FROM code_index WHERE code=?", (code,)).fetchone()[0],
            "withdrawn")
        ev = conn.execute("SELECT payload FROM events WHERE type='memory.retired'").fetchone()
        self.assertIsNotNone(ev, "必须写 memory.retired 事件")
        self.assertEqual(json.loads(ev[0])["status"], "withdrawn")

        # 默认检索看不到；显式带上 include_superseded 才看得到
        self.assertEqual(fish.search_memories(conn, query="误写")[0], [])
        self.assertIn(code, [r[0] for r in fish.search_memories(
            conn, query="误写", include_superseded=True)[0]])
        # 正文文件必须留着——删文件会造断链，healthcheck 会报
        # 路径从 `code_index.file_path` 读：**2026-10-25 起文件名含标题缩写**
        # （`<编码>__<标题24字>.md`），不再等于 `<编码>.md`。判据意图不变（作废不删文件）。
        _fp = conn.execute("SELECT file_path FROM code_index WHERE code=?",
                           (code,)).fetchone()[0]
        self.assertTrue((Path(self.root) / _fp).exists(),
                        f"作废不删文件（file_path={_fp}）")
        # 重复作废是幂等的，不是错
        self.assertIn("已经是 withdrawn", fish.withdraw_memory(conn, code))
        conn.close()

    def test_withdraw_rejects_superseded(self):
        """作废与取代是两种语义：对已 superseded 的条目作废必须报错，不许混用。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        a = self._mk(conn, "旧条目", "内容一")
        b = self._mk(conn, "新条目", "内容二")
        fish.supersede_memory(conn, a, b)
        with self.assertRaises(ValueError) as ctx:
            fish.withdraw_memory(conn, a)
        self.assertIn("supersede", str(ctx.exception))
        conn.close()

    def test_rebuild_replays_withdrawn(self):
        """重建能还原 withdrawn：清空投影后从事件重放，状态必须还是 withdrawn。"""
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        code = self._mk(conn, "要作废的", "内容")
        fish.withdraw_memory(conn, code, reason="test")
        conn.execute("DELETE FROM code_index")
        conn.commit()
        rep = fish.rebuild_projection(conn, Path(self.root))
        self.assertEqual(
            conn.execute("SELECT status FROM code_index WHERE code=?", (code,)).fetchone()[0],
            "withdrawn", "重建必须还原作废状态（memory.retired 可重放）")
        self.assertGreaterEqual(rep["retired_events"], 1)
        conn.close()


    def test_search_does_not_truncate_before_status_filter(self):
        """回归 ANYI-261024-05：FTS 前 N 条命中恰好都 superseded 时，active 命中不能被饿死。

        真实症状：query="反馈" 全库命中 23 条（其中 active 8 条），却返回 0 条——
        因为旧实现先在 FTS 里 LIMIT 5，最早那 5 条恰好全是 superseded，再按 active 过滤就空了。
        """
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        codes = [self._mk(conn, f"反馈问题{i}", "同一主题的内容") for i in range(6)]
        keep = codes[-1]
        for c in codes[:5]:
            fish.supersede_memory(conn, c, keep, reason="测试：制造前 5 条都已被取代的局面")

        rows, _ = fish.search_memories(conn, query="反馈", limit=5)
        got = [r[0] for r in rows]
        self.assertIn(keep, got, "active 命中必须能查到（先截断再过滤会返回空）")
        conn.close()

    def test_healthcheck_reports_fts_gap(self):
        """healthcheck 必须报出全文索引落后（2026-10-24 实测：缺口靠人工比对才发现）。"""
        import contextlib
        import io
        conn = self._conn()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        a = self._mk(conn, "索引测试一", "内容一")
        self._mk(conn, "索引测试二", "内容二")
        conn.execute("DELETE FROM memory_fts WHERE code=?", (a,))
        conn.commit()
        conn.close()

        ns = type("A", (), {"root": self.root})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_healthcheck(ns)
        out = buf.getvalue()
        self.assertIn("落后 1 条", out, "索引缺口必须在体检里报出来")
        self.assertIn("reindex", out, "报出来还要给下一步")

        conn = self._conn()
        fish.reindex_fts(conn, Path(self.root))
        conn.close()
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            fish.cmd_healthcheck(ns)
        self.assertIn("与记忆同步", buf2.getvalue(), "补齐后不应再报缺口")


if __name__ == "__main__":
    unittest.main(verbosity=2)
