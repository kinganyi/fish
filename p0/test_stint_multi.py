#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目档期「一个会话多项目」判据（2026-10-24）。

背景
----
旧机制：`ux_stint_open = UNIQUE(citizen_id) WHERE to_ts IS NULL` —— 一个公民同时
只能有**一个**开档期，而 `open_project_stint` 靠"先关旧、再开新"来绕过这条约束。
问题在真实工作形态上炸开：AI 常常跨项目干活（既改 ANYI，又查企业智能），
于是"档期"只能靠反复切换伪造，追溯反而失真；`project_stints` 因此长期 0 行。

新语义（本次变更）：
  · 索引变成 `UNIQUE(citizen_id, project) WHERE to_ts IS NULL` —— **一人一项目一个**；
  · `open_project_stint` 不再关旧档期：同项目 → `reused`，异项目 → `opened`；
  · `start_session(projects=[...])` 可以一次开多个档期（CLI 上 `--project` 可重复给）；
  · **每条记忆仍只归属一个项目**（code_index.project 是单值，本次不动）。
  会话结束时 `end_session` 依然"关所有"——多项目并行只在会话活着时成立。

判据与变异（变异驱动见 mutation_stint_multi.py，真源码变异 → 必红 → 字节还原）
--------------------------------------------------------------------------------
A 同会话多项目：两条开档期并存   | 变异：索引退回一列 → 第二个插入被拒 → 红
B 同项目只一个：第二次 reused     | 变异：去掉复用分支 → 重复插入被索引拒 → 红
C 切项目不关旧档期（核心）        | 变异：把"先关后开"加回去 → ANYI 被关 → 红
D end_session 关所有              | 变异：只关最新一条 → 有一条留开 → 红
E start_session 多项目            | 变异：只处理第一个项目 → 长度 1 → 红
F current_open_stint 兼容         | 变异：单数版返回 None → 断空指针 → 红
G 旧库迁移（一列 → 两列）         | 变异：迁移不 DROP 旧索引 → 仍一列 → 红

运行：`python -m pytest -q test_stint_multi.py`
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FISH = HERE / "fish.py"

_spec = importlib.util.spec_from_file_location("fish", FISH)
fish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fish)

# 宿主身份类变量：跑 CLI 子进程时要清掉，否则"我这个进程算谁"会被外层宿主污染。
SCRUB_ENVS = ("DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID",
              "FISH_SESSION_ACTOR", "FISH_ACTOR")

# 档期读列（与 fish._STINT_READ_FIELDS 同构，但**独立抄一份**：判据不该拿被测实现的
# 常量去解释自己的查询结果，否则它一改，判据就跟着"自适应"地失明）。
STINT_COLS = ("id", "citizen_id", "account_id", "project", "from_ts", "set_by")


def clean_env(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENVS}
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


def index_columns(conn, name):
    """库自己报出来的索引列（列序即建索引时的列序）。索引不存在 → 空元组。"""
    return tuple(r[2] for r in conn.execute(f"PRAGMA index_info({name})"))


class StintMultiProjectTest(unittest.TestCase):
    """项目档期多项目并行的 7 条判据。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-stint-")
        self.root = Path(self.tmp) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.cid = "01STINTMULTI0000000000001"
        self._insert_citizen(self.conn, self.cid)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _insert_citizen(self, conn, citizen_id, name="档期测试会话"):
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (citizen_id, name, "visitor", "engineer", "dsh", "active", fish.now_utc()))
        conn.commit()

    def _stints(self, citizen_id, only_open=False):
        """独立取数：不经过被测实现的读函数（避免"用实现证实现"）。"""
        sql = (f"SELECT {','.join(STINT_COLS)},to_ts,closed_reason FROM project_stints "
               "WHERE citizen_id=?")
        if only_open:
            sql += " AND to_ts IS NULL"
        sql += " ORDER BY from_ts ASC, id ASC"
        rows = self.conn.execute(sql, (citizen_id,)).fetchall()
        keys = STINT_COLS + ("to_ts", "closed_reason")
        return [dict(zip(keys, r)) for r in rows]

    def _open_projects(self, citizen_id):
        return [r["project"] for r in self._stints(citizen_id, only_open=True)]

    def _run_cli(self, root, args, env=None):
        proc = subprocess.run(
            [sys.executable, str(FISH), "--root", str(root)] + list(args),
            capture_output=True, encoding="utf-8", errors="replace",
            cwd=str(self.tmp), env=clean_env() if env is None else env)
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    # ------------------------------------------------------------ A

    def test_A_same_session_multi_projects(self):
        """判据 A：同一会话可以同时在多个项目上开档期（两条并存）。

        "同一会话"用的是同一个 citizen_id —— 这正是旧索引（按 citizen 唯一）拦下的那件事。
        """
        first = fish.open_project_stint(self.conn, self.cid, "ANYI")
        second = fish.open_project_stint(self.conn, self.cid, "企业智能")
        self.conn.commit()
        self.assertEqual((first["action"], second["action"]), ("opened", "opened"),
                         "两个项目都应是新开（不是复用）")
        self.assertNotEqual(first["stint_id"], second["stint_id"], "必须是两条不同的档期")
        self.assertEqual(self._open_projects(self.cid), ["ANYI", "企业智能"],
                         "同一会话必须能同时开着两个项目")
        # 索引维度自证：库里的索引确实是两列（一列版会让上面第二个插入直接抛异常）
        self.assertEqual(index_columns(self.conn, "ux_stint_open"),
                         ("citizen_id", "project"),
                         "开档期唯一索引必须是 (citizen_id, project) 两列版")

    # ------------------------------------------------------------ B

    def test_B_same_project_single_open(self):
        """判据 B：同一项目只能有一个开档期；重复开是 reused，不写新行。

        两段：写入口（reused 分支）与库层（绕过入口直接插必须被拒）。
        只测前者的话，把索引删掉也能过——那时"唯一"其实只剩 Python 自觉。
        """
        first = fish.open_project_stint(self.conn, self.cid, "ANYI")
        again = fish.open_project_stint(self.conn, self.cid, "ANYI")
        self.conn.commit()
        self.assertEqual(again["action"], "reused", "同项目重开应是 reused")
        self.assertEqual(again["stint_id"], first["stint_id"], "复用必须指向同一条档期")
        self.assertEqual(again["from_ts"], first["from_ts"], "复用不许改开档时间")
        self.assertEqual(len(self._stints(self.cid)), 1, "同项目重开不该写第二行")
        with self.assertRaises(sqlite3.IntegrityError,
                               msg="绕过写入口插同项目第二个开档期却成功了——库层没锁"):
            self.conn.execute(
                "INSERT INTO project_stints (citizen_id,project,from_ts) VALUES (?,?,?)",
                (self.cid, "ANYI", fish.now_utc()))
        self.conn.rollback()
        self.assertEqual(len(self._stints(self.cid)), 1, "被拒的插入不该留下痕迹")

    # ------------------------------------------------------------ C

    def test_C_switch_keeps_old_stint_open(self):
        """判据 C（**本轮最核心的语义变更**）：切项目**不关**旧档期。

        旧实现里这句是必须的（不关就撞一列唯一索引）；新语义下它是错的：
        切到企业智能不等于"离开 ANYI"，AI 之后还会回 ANYI 干活。
        所以断言三件事：旧档期 to_ts 仍是 NULL、没有 closed_reason、两条同时开着。
        """
        fish.open_project_stint(self.conn, self.cid, "ANYI")
        self.conn.commit()
        fish.open_project_stint(self.conn, self.cid, "企业智能")
        self.conn.commit()
        rows = self._stints(self.cid)
        self.assertEqual(len(rows), 2)
        anyi = [r for r in rows if r["project"] == "ANYI"][0]
        self.assertIsNone(anyi["to_ts"],
                          "切到企业智能后，ANYI 的档期必须仍是开着的（to_ts IS NULL）")
        self.assertIsNone(anyi["closed_reason"],
                          "旧档期没关就不该有关闭原因（旧实现会写 project_switch）")
        self.assertEqual(self._open_projects(self.cid), ["ANYI", "企业智能"])

    # ------------------------------------------------------------ D

    def test_D_end_session_closes_all(self):
        """判据 D：`session end` 关**所有**开档期（多项目一起收尾）。

        多项目并行只在会话活着时成立；会话结束了，它在任何项目上的档期都该收尾。
        """
        res = fish.start_session(self.conn, projects=["ANYI", "企业智能"],
                                 environ={"DSH_SESSION_ID": "sess-stint-D"})
        cid = res["citizen_id"]
        self.assertEqual(len(res["stints"]), 2, "前提：start 已经开出两个档期")
        end = fish.end_session(self.conn, token=res["token"])
        self.assertEqual(end["stints_closed"], 2, "两个开档期都必须被关掉")
        rows = self._stints(cid)
        self.assertEqual(len(rows), 2, "关档期不是删档期：行还在")
        self.assertEqual([r["project"] for r in rows], ["ANYI", "企业智能"])
        for row in rows:
            self.assertIsNotNone(row["to_ts"], f"{row['project']} 档期必须有 to_ts")
            self.assertEqual(row["closed_reason"], "session_end",
                             "关闭原因要留痕（与 project_switch 区分）")
        self.assertEqual(self._open_projects(cid), [], "会话结束后不许还留着开档期")

    # ------------------------------------------------------------ E

    def test_E_start_session_multi_projects(self):
        """判据 E：`start_session(projects=[...])` 一次开出多条档期（进程内 + CLI 两层）。"""
        env = {"DSH_SESSION_ID": "sess-stint-E"}
        res = fish.start_session(self.conn, projects=["ANYI", "企业智能"], environ=env)
        self.assertEqual([s["project"] for s in res["stints"]], ["ANYI", "企业智能"],
                         "stints 必须按传入顺序逐条返回")
        self.assertEqual([s["action"] for s in res["stints"]], ["opened", "opened"])
        self.assertEqual(self._open_projects(res["citizen_id"]), ["ANYI", "企业智能"])
        # 兼容口径：单值字段仍在（多项目时为 None），第一条档期仍能从 stint 取到
        self.assertIsNone(res["project"], "多项目时 project 单值字段应为 None")
        self.assertEqual(res["stint"]["project"], "ANYI", "stint 是列表的第一条")
        self.assertEqual(res["projects"], ["ANYI", "企业智能"])
        # 审计：事件里必须留全量（不然事后说不清"这次 start 开了几个档期"）
        payload = self.conn.execute(
            "SELECT payload FROM events WHERE type='session.opened' ORDER BY seq DESC "
            "LIMIT 1").fetchone()[0]
        ev = json.loads(payload)
        self.assertEqual(ev["projects"], ["ANYI", "企业智能"])
        self.assertEqual(ev["project"], None, "多项目时事件里的 project 也应为 None")
        self.assertEqual([a["action"] for a in ev["stint_actions"]], ["opened", "opened"])

        # 单项目（旧关键字 project=）仍然只开一条：兼容路径不能变成"开两条"
        single = fish.start_session(self.conn, project="ANYI2",
                                    environ={"DSH_SESSION_ID": "sess-stint-E2"})
        self.assertEqual(len(single["stints"]), 1)
        self.assertEqual(single["project"], "ANYI2")

        # 去重保序：同一个项目传两次只开一条（**复用同一会话**：否则是另一个公民，
        # 那条档期对它是"新开"，reused 根本不该出现）
        dup = fish.start_session(self.conn, projects=["ANYI", "ANYI", "  "], environ=env)
        self.assertEqual([s["project"] for s in dup["stints"]], ["ANYI"])
        self.assertEqual(dup["stints"][0]["action"], "reused",
                         "重复给同一个项目应是 reused（同一公民同项目只一条开档期）")

        # CLI 层：--project 可重复给（action="append"）
        cli_root = Path(self.tmp) / "cli-data"
        rc, out, err = self._run_cli(cli_root, ["session", "start",
                                                "--project", "ANYI",
                                                "--project", "企业智能"])
        self.assertEqual(rc, 0, f"CLI 多项目 start 失败：{err}")
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("stint: ")]), 2,
                         f"CLI 应打印两条 stint 行：{out}")
        self.assertIn("stint: opened ANYI", out)
        self.assertIn("stint: opened 企业智能", out)
        self.assertRegex(out, r"(?m)^project: ANYI, 企业智能$",
                         "project 字段要能看出两个项目")
        conn = fish.connect(cli_root)
        try:
            self.assertEqual(
                [r[0] for r in conn.execute(
                    "SELECT project FROM project_stints WHERE to_ts IS NULL "
                    "ORDER BY from_ts, id")],
                ["ANYI", "企业智能"], "CLI 参数必须真的落成两条开档期")
        finally:
            conn.close()

    # ------------------------------------------------------------ F

    def test_F_current_open_stint_compat(self):
        """判据 F：`current_open_stint` 保留可用，返回**最近开的**那一个（不崩）。"""
        fish.open_project_stint(self.conn, self.cid, "ANYI")
        self.conn.commit()
        fish.open_project_stint(self.conn, self.cid, "企业智能")
        self.conn.commit()
        # 把两条档期的 from_ts 拉开，让"最近"有确定答案（now_utc 是秒级，同秒会打平）
        self.conn.execute("UPDATE project_stints SET from_ts='2026-10-24T03:00:00Z' "
                          "WHERE citizen_id=? AND project='ANYI'", (self.cid,))
        self.conn.execute("UPDATE project_stints SET from_ts='2026-10-24T05:00:00Z' "
                          "WHERE citizen_id=? AND project='企业智能'", (self.cid,))
        self.conn.commit()
        st = fish.current_open_stint(self.conn, self.cid)
        self.assertIsNotNone(st, "多项目时 current_open_stint 不许返回 None")
        self.assertEqual(st["project"], "企业智能", "单数版返回**最近开的**那一个")
        self.assertEqual(st["from_ts"], "2026-10-24T05:00:00Z")
        self.assertEqual([s["project"] for s in fish.current_open_stints(self.conn, self.cid)],
                         ["ANYI", "企业智能"], "列表版按 from_ts 升序")
        # 没有档期时两个函数都要安全（调用方不必先判空）
        self.assertIsNone(fish.current_open_stint(self.conn, "no-such-citizen"))
        self.assertEqual(fish.current_open_stints(self.conn, "no-such-citizen"), [])
        # status 事实层两个口径都要在（stints 全量 / stint 最近一条）
        status = fish.session_status(self.conn, environ={"DSH_SESSION_ID": "sess-unknown"})
        self.assertEqual(status["stints"], [], "未登记的会话没有档期")
        self.assertIsNone(status["stint"])

    # ------------------------------------------------------------ G

    def test_G_legacy_one_column_index_migrated(self):
        """判据 G：旧库（一列 ux_stint_open + 已有开档期）→ migrate → 索引变两列且多项目可用。

        这是**升级路径**的判据，不是"新建库"的判据：新建库怎么建都对，真事故在旧库上。
        旧库的关键特征是"索引已经存在且是一列版"，而 `CREATE UNIQUE INDEX IF NOT EXISTS`
        见同名索引就跳过——只 CREATE 不 DROP，旧索引会带着旧语义活到升级之后。
        """
        legacy = Path(self.tmp) / "legacy-data"
        conn = fish.connect(legacy)
        try:
            conn.executescript(fish.SCHEMA)            # 只建表（索引归 migrate）
            self.assertEqual(index_columns(conn, "ux_stint_open"), (),
                             "夹具前提：SCHEMA 不建索引，此刻应还没有 ux_stint_open")
            conn.execute("CREATE UNIQUE INDEX ux_stint_open ON project_stints(citizen_id) "
                         "WHERE to_ts IS NULL")       # ← 造旧库的一列版
            conn.commit()
            self.assertEqual(index_columns(conn, "ux_stint_open"), ("citizen_id",),
                             "夹具前提：旧库应处于一列索引状态")
            # 旧库里还有真实数据：一个公民、一条开档期（迁移必须不丢、不关）
            self._insert_citizen(conn, "C-LEGACY", "旧库遗留会话")
            conn.execute("INSERT INTO project_stints (citizen_id,project,from_ts) "
                         "VALUES (?,?,?)", ("C-LEGACY", "ANYI", "2026-10-20T01:00:00Z"))
            conn.commit()

            fish.migrate(conn)                          # ← 升级

            self.assertEqual(index_columns(conn, "ux_stint_open"), ("citizen_id", "project"),
                             "迁移必须 DROP 掉一列索引再建两列版（只 CREATE 必失败于此处）")
            # 迁移幂等：再跑一次不回退、不报错
            fish.migrate(conn)
            self.assertEqual(index_columns(conn, "ux_stint_open"), ("citizen_id", "project"))
            # 数据没被迁移动过：旧的开档期还在、还是开着的
            rows = conn.execute(
                "SELECT project,from_ts,to_ts FROM project_stints WHERE citizen_id=?",
                ("C-LEGACY",)).fetchall()
            self.assertEqual(rows, [("ANYI", "2026-10-20T01:00:00Z", None)],
                             "迁移不许关档期、不许改写已有行")
            # 升级之后真能用：同一会话再开一个项目
            fish.open_project_stint(conn, "C-LEGACY", "企业智能")
            conn.commit()
            self.assertEqual(
                [r[0] for r in conn.execute(
                    "SELECT project FROM project_stints WHERE citizen_id=? AND to_ts IS NULL "
                    "ORDER BY from_ts, id", ("C-LEGACY",))],
                ["ANYI", "企业智能"], "升级后的库必须支持多项目并行")
            # 反向：同项目重复开仍被拒（两列索引没把边界放松成"随便开"）
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO project_stints (citizen_id,project,from_ts) "
                             "VALUES (?,?,?)", ("C-LEGACY", "ANYI", fish.now_utc()))
            conn.rollback()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
