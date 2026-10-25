#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish 会话画像（S1）· 判据测试（2026-10-24）
================================================
被测函数：`fish.compute_session_profile()` + 它的三个子算法（13 §3.3 / 16 §5.3）。

判据（每一条都配真源码变异自证，见 `mutation_profile_s1.py`）：

  A  tool_usage 按会话切分：甲的画像只有甲自己的调用，乙的不许算进来
  B  工具名归一化：`resolve_code` 与 `resolve` 是同一个工具，合并成一个键
  C  盲区：同类其他会话用过 ≥5 次、本会话 0 次
     · 本会话的**失败**调用不算"用过"（否则盲区被自己的失败调用吃掉）
  C2 盲区不退回全库：没有任何同类证据时返回空列表（3.2 第 3 级）
  D  盲区门槛是 5：同类其他会话只调 4 次 → 不算盲区
  E  知识遗漏：同类其他会话读 ≥3 次、本会话 0 次、该条 active
     · 本会话读过一次就不是遗漏（③ 的判别力）
  F  遗漏只报 active：withdrawn 的条目被别人读 5 次也不报
  G  审计文件不存在不崩：返回空画像，但 computed_at 仍然给出
  H  真实数据读数：生产库**只读**跑 `anyi-studio-开发AI` → tool_usage 非空，
     且与独立复算（自己解析 jsonl、自己归一化）逐键相等
  I  （附加）只读 + 字段固定：不写任何表；返回的键集合恰好是契约里的那几层

纪律：
  · 夹具全部落在 tempdir（判据 H 例外：生产库用 `file:...?mode=ro` 只读打开）；
  · 断言真值来自**独立复算**（独立 SQL / 独立解析），不复用被测函数的返回结果自证；
  · 判据 H 可用环境变量 `FISH_PROD_DB` 指向另一个只读库（默认 `p0/data/index.sqlite`）。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fish  # noqa: E402

HERE = Path(__file__).resolve().parent
PROD_DB = HERE / "data" / "index.sqlite"
PROD_SESSION = "anyi-studio-开发AI"

# 「别的会话读得很多、我没读」那条记忆的标题（判据 E/F 的人话标志，只为可读性）
GAP_TITLE = "遗漏见证甲"
NOT_GAP_TITLE = "本会话读过的丙"
RETIRED_TITLE = "已作废的乙"

NOW = fish.now_utc()


class FishProfileS1Test(unittest.TestCase):
    """判据 A–I。类名与文件名被 mutation_profile_s1.py 逐条点名调用。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-profile-")
        self.root = Path(self.tmp) / "data"
        self.root.mkdir(parents=True, exist_ok=True)
        conn = fish.connect(self.root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,?,?,?)", ("TEST", "TEST", "", 0, "active"))
            conn.commit()
        finally:
            conn.close()
        self.conn = fish.connect(self.root)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _audit_path(self, root=None):
        root = Path(root or self.root)
        return root / fish.AUDIT_DIR_NAME / fish.AUDIT_TOOL_CALLS_FILE

    def _write_audit(self, records, root=None):
        """把审计行写进夹具目录（结构按 20 §2.1：ts / actor / kind / name / ok）。"""
        path = self._audit_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
        path.write_text(text + "\n", encoding="utf-8")
        return path

    def _call(self, session, tool, *, actor=None, ok=True, kind="tool", ts=None):
        """造一条审计行。name 单独给是为了能造 `resolve_code` 这种历史名字。"""
        rec = {"ts": ts or NOW, "kind": kind, "name": tool, "ok": ok,
               "session_actor": session}
        if actor is not None:
            rec["actor"] = actor
        return rec

    def _citizen(self, citizen_id, display_name, kind="role"):
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at) "
            "VALUES (?,?,?,'active',?)", (citizen_id, display_name, kind, NOW))
        self.conn.commit()

    def _remember(self, title, body="判据夹具正文：只用来占位。", project="TEST"):
        code, _ = fish.record_memory(self.conn, self.root, project=project,
                                     title=title, body=body, mem_type="fact")
        return code

    def _access(self, code, session_key, citizen_id, times=1, via="recall"):
        for _ in range(times):
            fish.append_access_log(self.conn, [code], via=via, citizen_id=citizen_id,
                                   session_key=session_key, accessed_at=NOW)

    def _profile(self, **kwargs):
        kwargs.setdefault("root", self.root)
        return fish.compute_session_profile(self.conn, **kwargs)

    def _peer_count_in_file(self, tool, my_session, actor):
        """独立复算：审计文件里「同 actor、不是本会话、该工具、成功」的行数。

        不使用 fish 的归一化/过滤函数——真值必须独立于被测实现。
        """
        n = 0
        path = self._audit_path()
        if not path.exists():
            return 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("kind") != "tool" or rec.get("ok") is not True:
                continue
            if (rec.get("session_actor") or rec.get("actor")) == my_session:
                continue
            if rec.get("actor") != actor:
                continue
            if rec.get("name") != tool:
                continue
            n += 1
        return n

    # ------------------------------------------------------------ 判据 A

    def test_A_tool_usage_splits_by_session(self):
        """判据 A：甲 3 次 remember、乙 2 次 recall → 甲的画像只有 remember=3。"""
        self._write_audit([
            self._call("甲", "remember"), self._call("甲", "remember"),
            self._call("甲", "remember"),
            self._call("乙", "recall"), self._call("乙", "recall"),
        ])

        mine = self._profile(session_actor="甲")
        self.assertEqual(mine["tool_usage"], {"remember": 3},
                         f"甲的画像混进了别的会话的调用：{mine['tool_usage']}")
        self.assertEqual(mine["stats"]["tool_calls_total"], 3)

        # 反证夹具不是"乙的行根本没被读到"：乙自己的画像必须是 recall=2。
        other = self._profile(session_actor="乙")
        self.assertEqual(other["tool_usage"], {"recall": 2},
                         f"夹具失效：乙的画像也算不出自己的调用（{other['tool_usage']}）")

    # ------------------------------------------------------------ 判据 B

    def test_B_tool_name_normalized(self):
        """判据 B：resolve_code 5 次 + resolve 1 次 → 只有一个键 resolve=6。"""
        self._write_audit(
            [self._call("甲", "resolve_code") for _ in range(5)]
            + [self._call("甲", "resolve")]
        )

        usage = self._profile(session_actor="甲")["tool_usage"]
        self.assertEqual(usage, {"resolve": 6}, f"工具名没有归一化：{usage}")
        self.assertNotIn("resolve_code", usage,
                         "画像里出现了历史工具名 resolve_code：两个名字就是两个键")

    # ------------------------------------------------------------ 判据 C

    def test_C_blind_spot_from_same_account_peers(self):
        """判据 C：甲 0 次 neighbors（另有 5 次**失败**调用）、乙（同 actor）6 次 → 报盲区。"""
        records = [self._call("甲", "remember", actor="acc1") for _ in range(3)]
        # 本会话 5 次邻居调用全部失败：失败不算"用过"（3.1 的铁律）。
        records += [self._call("甲", "neighbors", actor="acc1", ok=False)
                    for _ in range(5)]
        records += [self._call("乙", "neighbors", actor="acc1") for _ in range(6)]
        self._write_audit(records)

        profile = self._profile(session_actor="甲", root=self.root)
        spots = {s["tool"]: s for s in profile["tool_blind_spots"]}
        self.assertIn("neighbors", spots,
                      f"同账号其他会话用过 6 次，甲一次没用过，却没报盲区："
                      f"{profile['tool_blind_spots']}")
        self.assertEqual(spots["neighbors"]["peer_calls"], 6,
                         "peer_calls 必须只数同类**其他**会话")
        self.assertEqual(spots["neighbors"]["my_calls"], 0,
                         "本会话 5 次调用全是失败，my_calls 必须是 0")
        self.assertIn("同账号", spots["neighbors"]["why"], "why 里要能看出跟谁比")
        self.assertIsNone(profile["tool_usage"].get("neighbors"),
                          f"失败的调用被算成「用过」了：{profile['tool_usage']}")
        self.assertEqual(self._peer_count_in_file("neighbors", "甲", "acc1"), 6,
                         "夹具失效：审计文件里同账号其他会话不是 6 次")

    # ------------------------------------------------------------ 判据 C2

    def test_C2_blind_spot_never_falls_back_to_whole_db(self):
        """判据 C2：没有任何同类证据时返回空（不许把全库当同类）。

        夹具：甲（actor=acc1）自己没调过 neighbors；丙（actor=acc2）调了 8 次。
        acc2 不是甲的同类 → 不许报盲区。
        """
        records = [self._call("甲", "remember", actor="acc1")]
        records += [self._call("丙", "neighbors", actor="acc2") for _ in range(8)]
        self._write_audit(records)

        profile = self._profile(session_actor="甲", root=self.root)
        self.assertEqual(profile["tool_blind_spots"], [],
                         f"把不相干的账号当成了同类（退回全库）："
                         f"{profile['tool_blind_spots']}")

    # ------------------------------------------------------------ 判据 D

    def test_D_blind_spot_threshold_is_five(self):
        """判据 D：同类其他会话只调 4 次 → 不算盲区（门槛 5）。"""
        records = [self._call("甲", "remember", actor="acc1")]
        records += [self._call("乙", "neighbors", actor="acc1") for _ in range(4)]
        self._write_audit(records)

        profile = self._profile(session_actor="甲", root=self.root)
        self.assertEqual(profile["tool_blind_spots"], [],
                         f"门槛不是 5（或 peer 计数偏高）：{profile['tool_blind_spots']}")
        # 反证夹具有效：4 次确实存在，只是没到门槛——否则这条判据是恒真。
        self.assertEqual(self._peer_count_in_file("neighbors", "甲", "acc1"), 4,
                         "夹具失效：同类其他会话不是 4 次，判据 D 没有判别力")
        self.assertIsNone(profile["tool_usage"].get("neighbors"))

    # ------------------------------------------------------------ 判据 E

    def test_E_knowledge_gap_requires_zero_own_reads(self):
        """判据 E：乙会话读 A 3 次、甲 0 次 → 甲有遗漏 A；丙读了 1 次 → 不是遗漏。"""
        self._citizen("CID-A", "甲")
        gap = self._remember(GAP_TITLE)
        not_gap = self._remember(NOT_GAP_TITLE)
        # 同一个账号（同 citizen_id）的其他会话：乙
        self._access(gap, "乙", "CID-A", times=3)
        self._access(not_gap, "乙", "CID-A", times=3)
        self._access(not_gap, "甲", "CID-A", times=1)     # 甲自己读过一次

        profile = self._profile(session_actor="甲", citizen_id="CID-A", root=self.root)
        codes = [g["code"] for g in profile["knowledge_gaps"]]
        self.assertIn(gap, codes, f"别人读 3 次、自己 0 次的条目没进遗漏：{codes}")
        self.assertNotIn(not_gap, codes,
                         f"自己读过一次的条目被判成遗漏了：{codes}")
        item = next(g for g in profile["knowledge_gaps"] if g["code"] == gap)
        self.assertEqual((item["others_read"], item["my_reads"]), (3, 0))
        self.assertEqual(item["title"], GAP_TITLE, "遗漏条目要带标题（AI 只看标题级信息）")
        self.assertIn("同账号", item["why"])

    # ------------------------------------------------------------ 判据 F

    def test_F_knowledge_gap_only_active(self):
        """判据 F：withdrawn 的条目被别人读 5 次也不报。"""
        self._citizen("CID-A", "甲")
        retired = self._remember(RETIRED_TITLE)
        fish.withdraw_memory(self.conn, retired, reason="判据 F：造一个作废条目")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM code_index WHERE code=?", (retired,)).fetchone()[0],
            "withdrawn")
        self._access(retired, "乙", "CID-A", times=5)

        profile = self._profile(session_actor="甲", citizen_id="CID-A", root=self.root)
        codes = [g["code"] for g in profile["knowledge_gaps"]]
        self.assertNotIn(retired, codes, f"作废条目被当成知识遗漏了：{codes}")
        # 反证夹具有效：它本来满足其余三条（别人读 5 次 ≥3、自己 0 次、库里存在）。
        n = self.conn.execute(
            "SELECT COUNT(*) FROM memory_access_log WHERE code=? AND session_key='乙'",
            (retired,)).fetchone()[0]
        self.assertEqual(n, 5, "夹具失效：作废条目没有被读 5 次，判据 F 没有判别力")

    # ------------------------------------------------------------ 判据 G

    def test_G_missing_audit_does_not_crash(self):
        """判据 G：审计文件不存在 → 空画像 + computed_at，不抛异常。"""
        empty_root = Path(self.tmp) / "no-audit"
        empty_root.mkdir(parents=True, exist_ok=True)
        self.assertFalse(self._audit_path(empty_root).exists())

        profile = fish.compute_session_profile(
            self.conn, session_actor="甲", root=empty_root)

        self.assertEqual(profile["tool_usage"], {}, "没有审计文件时 tool_usage 必须是空")
        self.assertEqual(profile["tool_blind_spots"], [], "没有审计证据时不许报盲区")
        self.assertEqual(profile["stats"]["tool_calls_total"], 0)
        stamp = profile["stats"]["computed_at"]
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                         f"computed_at 必须是 UTC 时间戳（空画像也要能证明「算过」）：{stamp}")

    # ------------------------------------------------------------ 判据 H

    def test_H_prod_read_only_reading(self):
        """判据 H：生产库只读跑真实会话 → tool_usage 非空，且等于独立复算。"""
        db = Path(os.environ.get("FISH_PROD_DB") or PROD_DB)
        if not db.exists():
            self.fail(f"判据 H 需要真实库：{db} 不存在。可用 FISH_PROD_DB 指向只读副本。")
        prod_root = db.parent
        audit = prod_root / fish.AUDIT_DIR_NAME / fish.AUDIT_TOOL_CALLS_FILE
        self.assertTrue(audit.exists(), f"判据 H 需要真实审计文件：{audit}")

        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        try:
            profile = fish.compute_session_profile(
                conn, session_actor=PROD_SESSION, root=prod_root)
        finally:
            conn.close()

        self.assertTrue(profile["tool_usage"],
                        f"生产库上 {PROD_SESSION} 的画像不该为空：{profile}")

        # 独立复算：自己解析 jsonl、自己按别名归一化、自己数（不借 fish 的函数）。
        expected = Counter()
        for line in audit.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != "tool" or rec.get("ok") is not True:
                continue
            if (rec.get("session_actor") or rec.get("actor")) != PROD_SESSION:
                continue
            name = rec.get("name")
            expected["resolve" if name == "resolve_code" else name] += 1

        self.assertEqual(profile["tool_usage"], dict(expected),
                         f"真实读数与独立复算不一致：{profile['tool_usage']} vs {dict(expected)}")
        self.assertEqual(profile["stats"]["tool_calls_total"], sum(expected.values()))
        self.assertEqual(profile["session"]["session_actor"], PROD_SESSION)

        # 盲区/遗漏的守则：报出去的每一条都必须站得住（生产数据上逐条复核）。
        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        try:
            for spot in profile["tool_blind_spots"]:
                self.assertIn(spot["tool"], fish.TOOL_UNIVERSE,
                              f"报了集合外的工具：{spot}")
                self.assertEqual(spot["my_calls"], 0, f"盲区的 my_calls 必须为 0：{spot}")
                self.assertGreaterEqual(spot["peer_calls"],
                                        fish.BLIND_SPOT_MIN_PEER_CALLS, f"没过门槛：{spot}")
            for gap in profile["knowledge_gaps"]:
                row = conn.execute("SELECT status,title FROM code_index WHERE code=?",
                                   (gap["code"],)).fetchone()
                self.assertIsNotNone(row, f"遗漏里出现了库里不存在的编码：{gap}")
                self.assertEqual(row[0], "active", f"遗漏里出现了非 active 条目：{gap}")
                self.assertEqual(gap["my_reads"], 0, f"遗漏的 my_reads 必须为 0：{gap}")
                self.assertGreaterEqual(gap["others_read"],
                                        fish.KNOWLEDGE_GAP_MIN_PEER_READS, f"没过门槛：{gap}")
        finally:
            conn.close()

    # ------------------------------------------------------------ 附加判据 I

    def test_I_read_only_and_fixed_fields(self):
        """判据 I：只读（不写任何表）+ 返回的键集合恰好是契约里那几层。"""
        self._citizen("CID-A", "甲")
        code = self._remember(GAP_TITLE)
        self._access(code, "乙", "CID-A", times=3)
        self._write_audit([self._call("甲", "remember", actor="CID-A"),
                           self._call("乙", "remember", actor="CID-A")])

        def counts():
            return tuple(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in ("code_index", "memory_access_log", "push_log", "events"))

        before = counts()
        profile = self._profile(session_actor="甲", citizen_id="CID-A", root=self.root)
        self.assertEqual(counts(), before, "compute_session_profile 写了表（本步必须只读）")

        self.assertEqual(set(profile), {"session", "tool_usage", "tool_blind_spots",
                                        "knowledge_gaps", "stats"},
                         f"顶层字段与契约不一致：{sorted(profile)}")
        self.assertEqual(set(profile["session"]),
                         {"session_actor", "citizen_id", "resolved_by"})
        self.assertEqual(set(profile["stats"]),
                         {"tool_calls_total", "access_log_total", "computed_at"})
        for spot in profile["tool_blind_spots"]:
            self.assertEqual(set(spot), {"tool", "peer_calls", "my_calls", "why"})
        for gap in profile["knowledge_gaps"]:
            self.assertEqual(set(gap), {"code", "title", "others_read", "my_reads", "why"})


if __name__ == "__main__":
    unittest.main()
