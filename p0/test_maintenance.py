#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`fish_maintenance` 的判据（T04b · 34 号 §2.2/§2.3）。

判据清单（与 55 号 §2.3 的 M1–M5 对齐）：

  · **M1** 连跑两次 → 第二次全部 `skipped(interval)`；`force=True` 可以再跑
  · **M2** 某任务抛异常 → 其余任务仍执行、台账记 `error`（不阻塞后续）
  · **M3** 台账七行齐全、seed 幂等；跑后 `last_run_at/status/affected/seconds` 可读
  · **M4** `corpus_ingest` / `corpus_retag` 默认不跑；配置开启后才调依赖
  · **M5** 变异：去掉间隔判断 → M1 必红（变异体在 `mutation_maintenance.py`，
    本文件只保证"正确实现必绿"；M5 的落点就是 M1，因为判定同一段代码）

另加任务级边界：links 只标 suppressed 不物理删 / 恢复两端 active / 最近 7 天补建边；
push_log 三段规则；fts 行数不等才重建；citizens 0 跳过；禁用优先于 force；
`only` 过滤按注册表顺序；依赖缺失只跳过**依赖它的**任务。

所有用例都在 tempfile 主库里跑（`fish.connect` + 真实 `fish.SCHEMA` + `migrate`），
不碰生产 `p0/data`。
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish                      # noqa: E402 —— 用真实 schema 建临时主库
import fish_corpus               # noqa: E402 —— 只为核对采集窗口口径
import fish_maintenance as fm    # noqa: E402

NOW = "2026-10-27T12:00:00Z"

CFG_DEFAULTS = {
    "session.archive_after_days": 30,
    "activity.retention_days": 14,
    "corpus.daily_enabled": False,
    "corpus.daily_retag": False,
}


def _make_db(tmp):
    """临时主库（真实 schema）：`fish.connect` + `fish.SCHEMA` + `fish.migrate`。"""
    root = tmp / "data"
    root.mkdir(parents=True, exist_ok=True)
    conn = fish.connect(root)
    conn.executescript(fish.SCHEMA)
    fish.migrate(conn)
    return conn


class _Recorder:
    """六个依赖的桩：记录调用、可改返回值、可注入异常。"""

    def __init__(self, config=None):
        self.config = dict(CFG_DEFAULTS)
        self.config.update(config or {})
        self.calls = {name: [] for name in
                      ("archive_citizens", "reindex_fts", "build_links_on_write",
                       "corpus_ingest", "corpus_retag")}
        self.returns = {
            "archive": {"archived_count": 2, "checked": 3, "planned": 2,
                        "skipped": [], "cutoff": "2026-09-27T12:00:00Z"},
            "reindex": 3,
            "links": {"derived": 1, "mentioned": 1, "similar": 0},
            "ingest": {"segments_new": 4, "sessions_seen": 5, "degraded": [],
                       "orphan": [], "lag_sec": 1, "busy": False},
            "retag": {"sessions": 2, "changed": 1, "kept_mapped": 0,
                      "kept_identity": 0, "warnings": []},
        }
        self.failures = {}

    # ---- 六个依赖 ----
    def get_config(self, key):
        return self.config.get(key)

    def archive_citizens(self, conn, *, days, **kwargs):
        self.calls["archive_citizens"].append({"days": days})
        self._maybe_raise("archive_citizens")
        return dict(self.returns["archive"])

    def reindex_fts(self, conn, root):
        self.calls["reindex_fts"].append(str(root))
        self._maybe_raise("reindex_fts")
        return int(self.returns["reindex"])

    def build_links_on_write(self, conn, code, *, title="", body="", **kwargs):
        self.calls["build_links_on_write"].append(
            {"code": code, "title": title, "body": body})
        self._maybe_raise("build_links_on_write")
        return dict(self.returns["links"])

    def corpus_ingest(self, *, root=None, days=None, **kwargs):
        self.calls["corpus_ingest"].append({"root": root, "days": days})
        self._maybe_raise("corpus_ingest")
        return dict(self.returns["ingest"])

    def corpus_retag(self, *, root=None):
        self.calls["corpus_retag"].append({"root": root})
        self._maybe_raise("corpus_retag")
        return dict(self.returns["retag"])

    def _maybe_raise(self, name):
        exc = self.failures.get(name)
        if exc is not None:
            raise exc

    def deps(self, *, without=()):
        mapping = {
            "get_config": self.get_config,
            "archive_citizens": self.archive_citizens,
            "reindex_fts": self.reindex_fts,
            "build_links_on_write": self.build_links_on_write,
            "corpus_ingest": self.corpus_ingest,
            "corpus_retag": self.corpus_retag,
        }
        for name in without:
            mapping.pop(name, None)
        return mapping


class _Base(unittest.TestCase):
    """每个用例一个 tempfile 根 + 临时主库 + 一组可编程依赖。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-maint-"))
        self.root = self.tmp / "data"
        self.conn = _make_db(self.tmp)
        self.rec = _Recorder()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:                 # noqa: BLE001 —— 清理不该掩盖断言
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 常用夹具 ----
    def run_tasks(self, **kwargs):
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("deps", self.rec.deps())
        return fm.run_maintenance(self.conn, self.root, **kwargs)

    @staticmethod
    def by_name(result):
        return {task["name"]: task for task in result["tasks"]}

    def ledger(self):
        return {task["name"]: task
                for task in fm.maintenance_status(self.conn)["tasks"]}

    def rows(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def code(self, code, *, status="active", recorded_at=None, title=None,
             file_path=None):
        self.conn.execute(
            "INSERT INTO code_index (code,title,status,recorded_at,file_path) "
            "VALUES (?,?,?,?,?)", (code, title, status, recorded_at, file_path))
        self.conn.commit()

    def link(self, src, dst, *, suppressed=0):
        self.conn.execute(
            "INSERT INTO links (src,dst,rel,source,weight,created_at,suppressed) "
            "VALUES (?,?,?,?,?,?,?)",
            (src, dst, "related", "explicit", 1.0, "2026-10-20T00:00:00Z", suppressed))
        self.conn.commit()

    def push(self, code, pushed_at, reaction=None):
        self.conn.execute(
            "INSERT INTO push_log (code, channel, pushed_at, reaction) "
            "VALUES (?,?,?,?)", (code, "prompt", pushed_at, reaction))
        self.conn.commit()


# ============================================================ M1 间隔幂等
class M1IntervalTest(_Base):
    def test_M1_second_run_is_skipped_forced_reruns(self):
        # 七个任务都要在第一轮真的跑起来：两个 corpus 任务是**双层闸门**
        # （台账 enabled=0 是种子默认；配置键又是第二道），两层都要显式打开。
        self.rec.config.update({"corpus.daily_enabled": True,
                                "corpus.daily_retag": True})
        fm.set_task_enabled(self.conn, "corpus_ingest", True)
        fm.set_task_enabled(self.conn, "corpus_retag", True)
        first = self.run_tasks()
        self.assertEqual(first["errors"], 0, first)
        self.assertEqual((first["ran"], first["skipped"]), (7, 0), first)
        for task in first["tasks"]:
            self.assertEqual(task["status"], "ok", task)

        before = {name: row["last_run_at"] for name, row in self.ledger().items()}
        self.assertTrue(all(before.values()), before)

        # 1 小时后：最长的间隔是 6h，所以七个任务全部在间隔内
        second = self.run_tasks(now="2026-10-27T13:00:00Z")
        self.assertEqual((second["ran"], second["skipped"], second["errors"]),
                         (0, 7, 0), second)
        for task in second["tasks"]:
            self.assertEqual(task["status"], "skipped", task)
            self.assertEqual(task["reason"], fm.REASON_INTERVAL, task)

        after = {name: row["last_run_at"] for name, row in self.ledger().items()}
        self.assertEqual(after, before,
                         "跳过不许推进 last_run_at——否则任务会被自己的 skipped 饿死")

        # force 越过间隔闸门（关闭的任务仍然不跑，那条在 LedgerControlTest）
        forced = self.run_tasks(now="2026-10-27T13:00:00Z", force=True)
        self.assertEqual((forced["ran"], forced["skipped"]), (7, 0), forced)
        self.assertEqual(forced["errors"], 0, forced)


# ============================================================ M2 失败不阻塞
class M2IsolationTest(_Base):
    def test_M2_error_recorded_and_others_still_run(self):
        self.rec.config.update({"corpus.daily_enabled": True,
                                "corpus.daily_retag": True})
        fm.set_task_enabled(self.conn, "corpus_ingest", True)
        fm.set_task_enabled(self.conn, "corpus_retag", True)
        self.rec.failures["archive_citizens"] = RuntimeError("归档炸了")
        result = self.run_tasks()
        by = self.by_name(result)

        self.assertEqual(result["errors"], 1, result)
        self.assertEqual(by["citizens_archive"]["status"], "error")
        self.assertIn("RuntimeError", by["citizens_archive"]["error"])
        self.assertIn("归档炸了", by["citizens_archive"]["error"])
        for name in ("kb_reindex", "links_rebuild", "push_log_prune",
                     "fts_verify", "corpus_ingest", "corpus_retag"):
            self.assertEqual(by[name]["status"], "ok", by[name])

        ledger = self.ledger()["citizens_archive"]
        self.assertEqual(ledger["last_status"], "error")
        self.assertIn("RuntimeError", ledger["last_error"])
        self.assertIsNotNone(ledger["last_seconds"])
        # 失败之后的后续任务确实跑过（不是"跳过后面全部"）
        self.assertIsNotNone(self.ledger()["fts_verify"]["last_run_at"])
        self.assertTrue(self.rec.calls["corpus_ingest"])


# ============================================================ M3 台账
class M3LedgerTest(_Base):
    def test_M3_seven_rows_seeded_idempotently(self):
        first = fm.ensure_maintenance_tables(self.conn)
        self.assertEqual(first["tasks"], 7)
        self.assertTrue(first["created"], "首次必须真的建表")
        self.assertEqual(first["seeded"], 7)
        second = fm.ensure_maintenance_tables(self.conn)
        self.assertFalse(second["created"], "第二次不该再建表")
        self.assertEqual(second["seeded"], 0, "seed 必须 INSERT OR IGNORE")

        rows = self.rows("SELECT task_name, enabled, interval_hours "
                         "FROM maintenance_task")
        self.assertEqual({row[0] for row in rows}, set(fm.TASK_ORDER))
        for name, enabled, interval in rows:
            self.assertGreaterEqual(int(interval), 1, name)
            self.assertIn(int(enabled), (0, 1), name)
        self.assertEqual({row[0] for row in rows if int(row[1]) == 0},
                         {"corpus_ingest", "corpus_retag"},
                         "两个 corpus 任务必须默认关（55 号 §2.2.1）")

    def test_M3_readings_are_readable_after_run(self):
        status = fm.maintenance_status(self.conn)
        self.assertEqual(status["count"], 7)
        self.assertEqual(len(status["tasks"]), 7)
        self.assertTrue(all(task["due"] for task in status["tasks"]
                            if task["enabled"]), "从未运行 = 到期")

        self.run_tasks()
        for name in ("kb_reindex", "links_rebuild", "push_log_prune",
                     "citizens_archive", "fts_verify"):
            row = self.rows(
                "SELECT last_run_at,last_status,last_affected,last_seconds "
                "FROM maintenance_task WHERE task_name=?", (name,))[0]
            self.assertIsNotNone(row[0], name)
            self.assertEqual(row[1], "ok", name)
            self.assertIsNotNone(row[2], name)
            self.assertIsNotNone(row[3], name)

        after = {task["name"]: task
                 for task in fm.maintenance_status(
                     self.conn, now=fm._parse_utc(NOW))["tasks"]}
        for name in ("kb_reindex", "links_rebuild", "push_log_prune",
                     "citizens_archive", "fts_verify"):
            self.assertFalse(after[name]["due"], name)
            self.assertIsNotNone(after[name]["next_run_at"], name)
        self.assertFalse(after["corpus_ingest"]["due"],
                         "关闭的任务不该显示 due（没开就没有'该跑'）")


# ============================================================ M4 corpus 默认关
class M4CorpusGateTest(_Base):
    def test_M4_corpus_ingest_default_off_then_config_on(self):
        # 第一道闸门：种子 `enabled=0`（默认关）
        seeded = self.run_tasks(only=["corpus_ingest"])
        self.assertEqual(seeded["tasks"][0]["reason"], fm.REASON_DISABLED,
                         "种子必须默认关（55 号 §2.2.1）")

        # 第二道闸门：台账打开后，配置键默认 false 仍然把它关着
        fm.set_task_enabled(self.conn, "corpus_ingest", True)
        result = self.run_tasks(only=["corpus_ingest"])
        task = result["tasks"][0]
        self.assertEqual(task["status"], "skipped", task)
        self.assertEqual(task["reason"], fm.REASON_CONFIG, task)
        self.assertEqual(self.rec.calls["corpus_ingest"], [],
                         "默认关时**一次都不许**调采集")

        self.rec.config["corpus.daily_enabled"] = True
        result = self.run_tasks(only=["corpus_ingest"], force=True)
        task = result["tasks"][0]
        self.assertEqual(task["status"], "ok", task)
        self.assertEqual(task["affected"], 4)
        call = self.rec.calls["corpus_ingest"][0]
        self.assertEqual(Path(call["root"]), self.root)
        self.assertEqual(call["days"], fish_corpus.DEFAULT_WINDOW_DAYS,
                         "采集窗口必须显式传 fish_corpus.DEFAULT_WINDOW_DAYS")
        self.assertEqual(call["days"], 3)

    def test_M4b_corpus_retag_default_off_then_config_on(self):
        fm.set_task_enabled(self.conn, "corpus_retag", True)
        result = self.run_tasks(only=["corpus_retag"])
        self.assertEqual(result["tasks"][0]["reason"], fm.REASON_CONFIG)
        self.assertEqual(self.rec.calls["corpus_retag"], [])

        self.rec.config["corpus.daily_retag"] = True
        result = self.run_tasks(only=["corpus_retag"], force=True)
        self.assertEqual(result["tasks"][0]["status"], "ok")
        self.assertEqual(result["tasks"][0]["affected"], 1)
        self.assertEqual(Path(self.rec.calls["corpus_retag"][0]["root"]), self.root)


# ============================================================ links
class LinksRebuildTest(_Base):
    def test_suppresses_dangling_and_restores_never_deletes(self):
        self.code("L-A")
        self.code("L-B")
        self.code("L-C", status="withdrawn")
        self.link("L-A", "L-B")
        self.link("L-A", "L-C")
        self.link("L-B", "L-C")

        deps = self.rec.deps(without=("build_links_on_write",))
        result = fm.run_maintenance(self.conn, self.root, only=["links_rebuild"],
                                    now=NOW, deps=deps)
        task = result["tasks"][0]
        self.assertEqual(task["status"], "ok", task)
        self.assertEqual(task["affected"], 2)
        self.assertEqual(task["detail"]["edge_backfill"], "skipped_dep")

        flags = {(src, dst): suppressed for src, dst, suppressed
                 in self.rows("SELECT src,dst,suppressed FROM links")}
        self.assertEqual(len(flags), 3, "边只许标 suppressed，绝不许物理删")
        self.assertEqual(flags[("L-A", "L-B")], 0)
        self.assertEqual(flags[("L-A", "L-C")], 1)
        self.assertEqual(flags[("L-B", "L-C")], 1)

        # 一端恢复 active → 下次重算把边放回来（仍不删）
        self.conn.execute("UPDATE code_index SET status='active' WHERE code='L-C'")
        self.conn.commit()
        result = fm.run_maintenance(self.conn, self.root, only=["links_rebuild"],
                                    now=NOW, deps=deps, force=True)
        self.assertEqual(result["tasks"][0]["affected"], 2)
        self.assertEqual({row[2] for row in
                          self.rows("SELECT src,dst,suppressed FROM links")}, {0})
        self.assertEqual(len(self.rows("SELECT 1 FROM links")), 3)

    def test_backfills_recent_codes_with_title_and_body(self):
        self.code("L-A", title="A")
        self.code("L-old", title="旧", recorded_at="2026-10-10T00:00:00Z")
        self.code("L-D", title="D 标题", recorded_at="2026-10-26T00:00:00Z",
                  file_path="mem/L-D.md")
        body_dir = self.root / "mem"
        body_dir.mkdir(parents=True)
        (body_dir / "L-D.md").write_text("正文提到 L-A", encoding="utf-8")

        result = fm.run_maintenance(self.conn, self.root, only=["links_rebuild"],
                                    now=NOW, deps=self.rec.deps())
        task = result["tasks"][0]
        self.assertEqual(task["status"], "ok", task)
        self.assertEqual(task["detail"]["edge_backfill"], "ran")
        self.assertEqual(task["detail"]["backfill_codes"], 1,
                         "只有最近 7 天新增的编码才补建（L-old 不该被扫）")
        self.assertEqual(task["detail"]["edges_created"], 2)
        self.assertEqual(task["affected"], 2)
        call = self.rec.calls["build_links_on_write"][0]
        self.assertEqual(call["code"], "L-D")
        self.assertEqual(call["title"], "D 标题")
        self.assertIn("正文提到 L-A", call["body"])

    def test_backfill_failure_is_counted_not_fatal(self):
        self.code("L-D", title="D", recorded_at="2026-10-26T00:00:00Z")
        self.rec.failures["build_links_on_write"] = ValueError("边炸了")
        result = fm.run_maintenance(self.conn, self.root, only=["links_rebuild"],
                                    now=NOW, deps=self.rec.deps())
        task = result["tasks"][0]
        self.assertEqual(task["status"], "ok", "单条补建失败不该让任务失败")
        self.assertEqual(task["detail"]["backfill_errors"], 1)
        self.assertIn("ValueError", task["detail"]["backfill_error"])


# ============================================================ push_log
class PushLogPruneTest(_Base):
    def test_prune_three_rules(self):
        self.push("ANYI-old-judged", "2026-01-01T00:00:00Z", "accepted")
        self.push("kb:old-judged", "2026-01-01T00:00:00Z", "accepted")
        self.push("ANYI-old-unjudged", "2026-01-01T00:00:00Z", None)
        self.push("ANYI-recent-judged", "2026-10-25T00:00:00Z", "accepted")
        self.push("activity:old", "2026-10-01T00:00:00Z", None)
        self.push("activity:recent", "2026-10-26T00:00:00Z", None)

        result = fm.run_maintenance(self.conn, self.root,
                                    only=["push_log_prune"], now=NOW,
                                    deps=self.rec.deps())
        task = result["tasks"][0]
        self.assertEqual(task["status"], "ok", task)
        self.assertEqual(task["detail"]["reaction_rows"], 1,
                         "只删『已判定 + 超 90 天 + 非 kb:』")
        self.assertEqual(task["detail"]["activity_rows"], 1,
                         "activity:* 单独一段，按 14 天保留期")
        self.assertEqual(task["affected"], 2)
        left = {row[0] for row in self.rows("SELECT code FROM push_log")}
        self.assertEqual(left, {"kb:old-judged", "ANYI-old-unjudged",
                                "ANYI-recent-judged", "activity:recent"})

    def test_activity_segment_skipped_when_retention_below_one(self):
        self.rec.config["activity.retention_days"] = 0
        self.push("activity:old", "2026-10-01T00:00:00Z", None)
        result = fm.run_maintenance(self.conn, self.root,
                                    only=["push_log_prune"], now=NOW,
                                    deps=self.rec.deps())
        task = result["tasks"][0]
        self.assertEqual(task["status"], "ok", task)
        self.assertEqual(task["affected"], 0)
        self.assertEqual(task["detail"]["activity_skipped"], "retention_days<1")
        self.assertEqual(len(self.rows("SELECT 1 FROM push_log")), 1,
                         "保留期 <1 = 不清理该段（不是删光）")


# ============================================================ fts
class FtsVerifyTest(_Base):
    def test_mismatch_triggers_reindex(self):
        self.code("F-A")
        self.code("F-B")
        self.rec.returns["reindex"] = 2
        result = fm.run_maintenance(self.conn, self.root, only=["fts_verify"],
                                    now=NOW, deps=self.rec.deps())
        task = result["tasks"][0]
        self.assertEqual(task["status"], "ok", task)
        self.assertEqual(task["detail"]["code_rows"], 2)
        self.assertEqual(task["detail"]["fts_rows"], 0)
        self.assertEqual(task["affected"], 2)
        self.assertEqual(len(self.rec.calls["reindex_fts"]), 1,
                         "行数不等必须真的重建")

    def test_equal_counts_do_not_rebuild(self):
        result = fm.run_maintenance(self.conn, self.root, only=["fts_verify"],
                                    now=NOW, deps=self.rec.deps())
        task = result["tasks"][0]
        self.assertEqual(task["status"], "ok", task)
        self.assertEqual(task["affected"], 0)
        self.assertEqual(self.rec.calls["reindex_fts"], [],
                         "行数相等时不该碰全量重建（那是 O(全库)）")


# ============================================================ citizens 闸门
class CitizensArchiveTest(_Base):
    def test_zero_days_skips_without_calling_archive(self):
        self.rec.config["session.archive_after_days"] = 0
        result = fm.run_maintenance(self.conn, self.root, only=["citizens_archive"],
                                    now=NOW, deps=self.rec.deps())
        task = result["tasks"][0]
        self.assertEqual(task["status"], "skipped", task)
        self.assertEqual(task["reason"], fm.REASON_CONFIG, task)
        self.assertEqual(self.rec.calls["archive_citizens"], [],
                         "0 的语义是『不自动归档』，不许拿它当阈值调下去")

    def test_configured_days_calls_archive(self):
        result = fm.run_maintenance(self.conn, self.root, only=["citizens_archive"],
                                    now=NOW, deps=self.rec.deps())
        task = result["tasks"][0]
        self.assertEqual(task["status"], "ok", task)
        self.assertEqual(task["affected"], 2)
        self.assertEqual(self.rec.calls["archive_citizens"][0]["days"], 30)


# ============================================================ 台账控制面
class LedgerControlTest(_Base):
    def test_disabled_task_skips_even_forced(self):
        fm.set_task_enabled(self.conn, "kb_reindex", False)
        result = self.run_tasks(only=["kb_reindex"], force=True)
        self.assertEqual(result["tasks"][0]["reason"], fm.REASON_DISABLED,
                         "enabled=0 是人的决定，force 不该越过")
        fm.set_task_enabled(self.conn, "kb_reindex", True)
        result = self.run_tasks(only=["kb_reindex"], force=True)
        self.assertEqual(result["tasks"][0]["status"], "ok")

    def test_unknown_task_is_rejected(self):
        with self.assertRaises(ValueError):
            fm.set_task_enabled(self.conn, "nope", False)
        with self.assertRaises(ValueError):
            self.run_tasks(only=["nope"])

    def test_only_filters_but_keeps_registry_order(self):
        result = self.run_tasks(only=["fts_verify", "kb_reindex"])
        self.assertEqual([task["name"] for task in result["tasks"]],
                         ["kb_reindex", "fts_verify"])
        self.assertEqual(result["ran"], 2)


# ============================================================ 依赖缺失
class DepMissingTest(_Base):
    def test_missing_deps_skip_only_dependent_tasks(self):
        # 两个 corpus 任务先过掉种子闸门，才能证到"依赖缺失"这一层
        fm.set_task_enabled(self.conn, "corpus_ingest", True)
        fm.set_task_enabled(self.conn, "corpus_retag", True)
        result = fm.run_maintenance(self.conn, self.root, now=NOW, deps={})
        by = self.by_name(result)
        for name in ("kb_reindex", "links_rebuild", "push_log_prune", "fts_verify"):
            self.assertEqual(by[name]["status"], "ok", by[name])
        for name in ("citizens_archive", "corpus_ingest", "corpus_retag"):
            self.assertEqual(by[name]["status"], "skipped", by[name])
            self.assertEqual(by[name]["reason"], "dep_missing:get_config", by[name])
        self.assertEqual(result["errors"], 0, "依赖缺失是跳过，不是 error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
