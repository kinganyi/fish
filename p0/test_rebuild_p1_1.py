#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 P1-1 的判据：`fish rebuild` 必须是**可证明无害**的恢复工具。

## 事故链（审查实测，2026-11-25）

1. **owner 降级**：`owner = actor.get("citizen_id") or old.get("owner")` ——
   `"unattributed"` 是**非空字符串**，`or` 认它，于是"这次事件没带凭证"就把快照里
   那个真实的归属覆写成"未归属"。生产实测 **8 条**被降级。
2. **先提交、后对账**：`conn.commit()` 在对账之前（480/517），于是 `exit 1` 报出
   不一致的时候，坏数据**已经落盘**。"恢复工具变成破坏工具"。
3. **无 dry-run、无备份**：`fish rebuild` 唯一行为就是写，也没有回滚点。

## 判据设计（成对；任一侧失效都会红）

 P1A-1  actor=unattributed + 快照 owner=有效公民 → 重建后**仍是那个公民**，且
        `applied=True`（"没被降级"与"这一炉确实提交了"必须同时成立——
        只断言 owner 的话，回滚也能让它保持不变，判据就变成假绿）
 P1A-2  对照组：actor 是有效公民时确实覆盖（不许为了保 owner 而无视 actor）
 P1A-3  两者都没有 → `unknown`（不凭空造 unattributed）
 P1A-4  dry-run（缺省）**零写**：code_index / memory_fts / `_backups` 全都不变
 P1A-5  注入一行差异 → 回滚 + 退出码 2，且库逐字段不变
 P1A-6  `--apply` 成功 → 行数 + 逐行五字段与重建前相等（幂等），且留下备份
 P1A-7  CLI 真实入口：`fish rebuild` 缺省 dry-run、`--apply` 才写、差异时 exit 2
 P1A-8  备份是 SQLite backup API 的产物（能独立打开、行数 = 重建前行数）

变异（证明判据有判别力）：
 P1A-M1 owner 优选改回旧写法 → P1A-1 那一对读数必红
 P1A-M2 先 commit 后对账 → P1A-5 的"库未变 + aborted"必红
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish          # noqa: E402
import fish_rebuild  # noqa: E402

UNATTRIBUTED = "unattributed"


class RebuildSafeBase(unittest.TestCase):
    """临时库 + 两个公民 + 最小事件构造。"""

    ALICE_NAME = "爱丽丝"
    BOB_NAME = "鲍勃"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-p1a-"))
        self.root = self.tmp
        self.conn = fish.connect(self.root / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.alice = fish.ulid()
        self.bob = fish.ulid()
        for cid, name in ((self.alice, self.ALICE_NAME), (self.bob, self.BOB_NAME)):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,status) VALUES (?,?,?)",
                (cid, name, "active"))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _seed(self, code, actor, title="判据样本"):
        """造一条 `memory.recorded`。`actor` 传公民 id / UNATTRIBUTED / None。"""
        actor_obj = {} if actor is None else {"citizen_id": actor}
        fish.append_event(
            self.conn, "memory.recorded", actor_obj, {"kind": "memory", "id": code},
            {"code": code, "title": title, "occurred_at": "2026-11-01T00:00:00Z"})
        self.conn.commit()

    def _owner(self, code):
        row = self.conn.execute(
            "SELECT owner FROM code_index WHERE code=?", (code,)).fetchone()
        return row[0] if row else None

    def _set_owner(self, code, owner):
        self.conn.execute("UPDATE code_index SET owner=? WHERE code=?", (owner, code))
        self.conn.commit()

    def _dump(self, *, include_backups=True):
        """库的**可比快照**：投影逐行（全部列）+ FTS 编码集合 + 备份目录清单。

        `include_backups=False` 用于"abort 也允许留备份"的场景：abort 前先备份是
        设计要求（安全网），不算"库被改了"；只比较投影与 FTS。
        """
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(code_index)")]
        rows = self.conn.execute(
            "SELECT %s FROM code_index ORDER BY code" % ",".join(cols)).fetchall()
        try:
            fts = self.conn.execute("SELECT code FROM memory_fts ORDER BY code").fetchall()
        except sqlite3.Error:
            fts = []
        bdir = Path(self.root) / "data" / "_backups"
        backups = sorted(p.name for p in bdir.glob("*")) if bdir.exists() else []
        out = {"cols": cols, "rows": rows, "fts": fts}
        if include_backups:
            out["backups"] = backups
        return out

    # ------------------------------------------------------------- 场景（可变异）
    def _scenario_unattributed_actor_keeps_owner(self):
        """场景：快照 owner=alice，但事件 actor 是 unattributed → 不许降级。

        返回 `(重建后 owner, rep)`：判据吃这一对，变异也吃这一对。
        """
        code = "p1a-261125-01"
        self._seed(code, actor=UNATTRIBUTED)
        fish.rebuild_projection(self.conn, self.root)      # 先物化（owner=unknown）
        self._set_owner(code, self.alice)                  # 快照里的"真归属"
        rep = fish.rebuild_projection(self.conn, self.root)
        return self._owner(code), rep, code

    def _scenario_diff_injected(self):
        """场景：投影被改脏（owner=ghost）→ 对账必然不一致 → 必须回滚。"""
        code = "p1a-261125-02"
        self._seed(code, actor=self.bob)
        fish.rebuild_projection(self.conn, self.root)      # owner=bob
        self._set_owner(code, "ghost-owner")               # 注入一行差异
        before = self._dump(include_backups=False)
        rep = fish.rebuild_projection(self.conn, self.root, apply=True)
        return {"rep": rep, "before": before, "after": self._dump(include_backups=False),
                "owner": self._owner(code), "code": code}


class RebuildOwnerPreferenceTest(RebuildSafeBase):
    """owner 优选：**只有有效公民才允许覆盖**（P1-1 第 1 条）。"""

    def test_P1A_1_unattributed_actor_keeps_snapshot_owner(self):
        """actor=unattributed + 快照 owner=有效公民 → 仍是那个公民，且这一炉真提交了。"""
        owner, rep, code = self._scenario_unattributed_actor_keeps_owner()
        self.assertEqual(owner, self.alice,
                         f"P1-1 回归：rebuild 把 {code} 的归属降级成 {owner!r}")
        self.assertNotEqual(owner, UNATTRIBUTED)
        self.assertTrue(rep["applied"],
                        f"应一致提交；实际 aborted={rep['aborted']} mismatch={rep['mismatch']}")
        self.assertFalse(rep["aborted"])

    def test_P1A_2_valid_actor_still_wins(self):
        """对照组：actor 是有效公民时必须覆盖快照——否则修法就变成"谁都不许改"。"""
        code = "p1a-261125-03"
        self._seed(code, actor=self.bob)
        rep = fish.rebuild_projection(self.conn, self.root)     # 无快照：actor 是唯一来源
        self.assertEqual(self._owner(code), self.bob, "有效 actor 没有被采用")
        self.assertTrue(rep["applied"])

        # 有快照但 actor 可信：事件更新 → 覆盖，且这条差异必须被对账**看见**
        self._set_owner(code, self.alice)
        rep = fish.rebuild_projection(self.conn, self.root, apply=False)
        self.assertTrue(
            any("owner" in m and self.bob in m for m in rep["mismatch"]),
            f"对账没看见 alice→bob 的覆盖：{rep['mismatch']}")

    def test_P1A_3_neither_side_known_becomes_unknown(self):
        """两边都没有 → `unknown`（诚实），不凭空写 unattributed。"""
        code = "p1a-261125-04"
        self._seed(code, actor=None)                            # actor 是 {}
        fish.rebuild_projection(self.conn, self.root)
        self.assertEqual(self._owner(code), "unknown")
        self.assertNotEqual(self._owner(code), UNATTRIBUTED)


class RebuildDryRunAndBackupTest(RebuildSafeBase):
    """dry-run 零写 / 备份 / 提交后逐行相等（P1-1 第 2、3 条）。"""

    def test_P1A_4_dry_run_writes_nothing(self):
        """缺省 dry-run：投影、FTS、`_backups` 一个字节都不许变。"""
        self._seed("p1a-261125-05", actor=self.alice)
        fish.rebuild_projection(self.conn, self.root)           # 物化（apply）
        before = self._dump()
        rep = fish.rebuild_projection(self.conn, self.root, apply=False)
        after = self._dump()
        self.assertEqual(before, after,
                         "dry-run 动了库（投影/FTS/备份三处任一都算红）")
        self.assertFalse(rep["applied"])
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["mismatch"], [])
        self.assertIsNone(rep["backup_path"], "dry-run 不该产生备份文件")

    def test_P1A_5_diff_injected_rolls_back(self):
        """注入一行差异 → 回滚：`aborted` 为真、库逐字段不变。"""
        seen = self._scenario_diff_injected()
        rep = seen["rep"]
        self.assertTrue(rep["mismatch"], "对账没发现注入的差异")
        self.assertTrue(rep["aborted"], "对账不一致却仍提交了")
        self.assertFalse(rep["applied"])
        self.assertEqual(seen["before"], seen["after"],
                         "对账不一致时投影/FTS 被改了（必须先 commit 后对账的反面）")
        self.assertTrue(rep["backup_path"] and Path(rep["backup_path"]).exists(),
                        "abort 前应先留备份（安全网）")
        self.assertEqual(seen["owner"], "ghost-owner", "回滚后投影应保持重建前状态")

    def test_P1A_6_apply_is_idempotent_and_backs_up(self):
        """apply 成功：逐行五字段与重建前相等，且备份可独立打开、行数对得上。"""
        for i, actor in enumerate((self.alice, self.bob, UNATTRIBUTED), start=1):
            self._seed(f"p1a-261125-1{i}", actor=actor)
        fish.rebuild_projection(self.conn, self.root)           # 物化
        before = self._dump()
        rep = fish.rebuild_projection(self.conn, self.root, apply=True)
        after = self._dump()
        self.assertTrue(rep["applied"], f"应提交：{rep['mismatch']}")
        self.assertEqual(rep["mismatch"], [])
        self.assertEqual(before["rows"], after["rows"], "apply 后投影与重建前不一致")
        self.assertEqual(len(before["rows"]), len(after["rows"]))
        self.assertEqual(rep["rows_after"], rep["rows_before"])

        # 备份：SQLite backup API 的产物（独立打开、行数 = 重建前行数）
        self.assertTrue(rep["backup_path"] and Path(rep["backup_path"]).exists(),
                        f"没留下写前快照：{rep['backup_path']}")
        self.assertEqual(Path(rep["backup_path"]).parent.name, "_backups")
        self.assertIn("index-before-rebuild-", Path(rep["backup_path"]).name)
        self.assertEqual(rep["backup_rows"], rep["rows_before"])
        bconn = sqlite3.connect(rep["backup_path"])
        try:
            self.assertEqual(
                bconn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0],
                rep["rows_before"], "备份里的行数 ≠ 重建前行数")
        finally:
            bconn.close()


class RebuildCliTest(unittest.TestCase):
    """真实入口：`fish rebuild` 缺省 dry-run、`--apply` 才写、差异时 exit 2。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-p1a-cli-"))
        self.env = dict(os.environ)
        self.env.pop("FISH_SESSION_TOKEN", None)     # 无凭证：actor=unattributed
        self.env["PYTHONIOENCODING"] = "utf-8"
        self._cli("init")
        self._cli("project-add", "P1AC")
        self._cli("citizen-add", "主人甲", "--kind", "resident")
        self._cli("remember", "P1AC", "演练样本", "--type", "fact")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cli(self, *args):
        r = subprocess.run(
            [sys.executable, "fish.py", "--root", str(self.tmp)] + list(args),
            cwd=str(HERE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=self.env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def _db(self):
        c = sqlite3.connect(str(self.tmp / "index.sqlite"))
        c.row_factory = sqlite3.Row
        return c

    def _rows(self):
        c = self._db()
        try:
            return [tuple(r) for r in c.execute(
                "SELECT code,owner,status,occurred_at FROM code_index ORDER BY code")]
        finally:
            c.close()

    def test_P1A_7_cli_default_dry_run_then_apply(self):
        """缺省 = dry-run（库不变、无备份）；`--apply` = 写（有备份）。"""
        before = self._rows()
        rc, out = self._cli("rebuild")
        self.assertEqual(rc, 0, out)
        self.assertIn("dry-run", out, "缺省必须明确说这是演练")
        self.assertEqual(self._rows(), before, "缺省 rebuild 改了库")
        self.assertFalse((self.tmp / "_backups").exists() and
                         any((self.tmp / "_backups").glob("index-before-rebuild-*")),
                         "dry-run 不许产生备份（那是写）")

        rc, out = self._cli("rebuild", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("已提交", out)
        self.assertEqual(self._rows(), before, "--apply 后投影与重建前不一致")
        self.assertTrue(list((self.tmp / "_backups").glob("index-before-rebuild-*.sqlite")),
                        "--apply 没有留写前快照")

    def test_P1A_7b_cli_diff_injected_exit_2_and_db_unchanged(self):
        """注入一行差异 → `--apply` 必须 exit 2，且库一个字都没改。

        注入口用 `status`（而不是 owner）：owner 的差异有"归一/合并/显式修正"三类
        **有据可查**的豁免（07 D-13 与本次 P1-1 的 `merged_codes`），注入 owner 可能
        被豁免掉；`status` 没有豁免面，能干净地验证"差异 → 闸门"这条链。
        """
        c = self._db()
        try:
            c.execute("UPDATE code_index SET status='withdrawn'")
            c.commit()
        finally:
            c.close()
        before = self._rows()
        rc, out = self._cli("rebuild", "--apply")
        self.assertEqual(rc, 2, f"对账不一致的退出码必须是 2（0/1 都不行）：\n{out}")
        self.assertIn("对账不一致", out)
        self.assertEqual(self._rows(), before, "exit 2 之后库被改了")
        self.assertEqual(before[0][2], "withdrawn")


class RebuildMutationTest(RebuildSafeBase):
    """变异自证：判据在坏实现下必须**红**（否则它只是装饰）。"""

    def test_P1A_M1_old_owner_preference_goes_red(self):
        """变异：owner 优选改回旧写法（`or` 链）→ P1A-1 那一对读数必红。"""
        orig = fish_rebuild._pick_owner
        fish_rebuild._pick_owner = lambda actor, old, vids: (
            (actor or {}).get("citizen_id") or old or "unknown")   # ← 旧写法
        try:
            owner, rep, _code = self._scenario_unattributed_actor_keeps_owner()
        finally:
            fish_rebuild._pick_owner = orig
        self.assertNotEqual(
            (owner, bool(rep["applied"])), (self.alice, True),
            "变异没被抓住：旧写法下 P1A-1 仍然绿 → 判据没有判别力")
        self.assertTrue(rep["aborted"], "旧写法应被对账拦下（aborted）")
        self.assertTrue(any(UNATTRIBUTED in m for m in rep["mismatch"]),
                        f"对账没报出降级：{rep['mismatch']}")

    def test_P1A_M2_commit_before_reconcile_goes_red(self):
        """变异：先 commit 再对账 → P1A-5 的"库未变 + aborted"必红。"""
        orig = fish_rebuild._settle

        def _mutant(conn, rep, *, apply):
            conn.commit()                                   # ← 变异：先落盘
            rep.update(rows_after=rep["total"], applied=True, aborted=False,
                       would_change=[])
            return rep

        fish_rebuild._settle = _mutant
        try:
            seen = self._scenario_diff_injected()
        finally:
            fish_rebuild._settle = orig
        rep = seen["rep"]
        self.assertNotEqual(
            (bool(rep["aborted"]), seen["before"] == seen["after"]), (True, True),
            "变异没被抓住：先提交后对账仍然满足 P1A-5 → 判据没有判别力")
        self.assertFalse(rep["aborted"], "变异体本应把不一致也提交掉")
        self.assertNotEqual(seen["owner"], "ghost-owner",
                            "变异体本应把脏行覆盖掉（这正是「先提交」的损伤）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
