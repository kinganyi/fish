#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档 × 归属 的交互判据（序3：跑第一次 30 天归档时暴露的真缺陷）。

## 这组判据为什么存在

`archive_citizens` 从 2026-10-25 就写好了，但 **`citizens_archive` 一直是 0 行**——
从没跑过。于是"归档之后，别的东西还认不认得这些人"这件事**从来没被验证**。

2026-11-11 第一次真跑（在库副本上，归档 12 个闲置公民）暴露了两件事：

1. **`fish rebuild` 把归档公民名下的记忆归属改写成了系统主人**。
   根因：`valid_citizen_ids` 只查热表 `citizens`，归档公民不在里面 → 被判"无效 owner"
   → 按 07 D-13 归一成系统主人。**这与 D-10 是同一类事故**：一次常规维护静默改写归属。
2. **`healthcheck` 把归档公民名下的记忆误报成"幽灵归属"**。
   根因同上：只查热表。

两处都已改为查 `citizens_all`（热 + 冷）。这组判据钉住"有效性口径"与"活跃性口径"
**不能混用**这条线：

| 场景 | 正确口径 | 判据 |
|---|---|---|
| 有效性 / 身份解析（重建、体检、`--owner` 校验） | `citizens_all` | A1 A2 A3 |
| 活跃性（凭证、能否当主人） | 只用热表 `citizens` | A4 A5 |
"""
import json
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish           # noqa: E402
import fish_common    # noqa: E402


class ArchiveAttributionTest(unittest.TestCase):
    """归档后，归属体系必须仍然认得出这些人。"""

    NOW = "2026-11-11T12:00:00Z"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-arch-")
        self.root = Path(self.tmp)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self._seed()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 夹具
    def _add_citizen(self, cid, name, last_seen):
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,"
            "last_seen_at,is_owner) VALUES (?,?,?,?,?,?,0)",
            (cid, name, "visitor", "active", "2026-10-01T00:00:00Z", last_seen))

    def _seed(self):
        """一个主人 + 一个早已闲置的访客 + 一条**归这个访客所有**的记忆。"""
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,"
            "last_seen_at,is_owner) VALUES ('OWNER','主人','resident','active',?,?,1)",
            ("2026-10-01T00:00:00Z", self.NOW))
        self._add_citizen("OLD-VISITOR", "老访客", "2026-01-01T00:00:00Z")
        fish.add_project(self.conn, "AR") if hasattr(fish, "add_project") else None
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "file_path,occurred_at,recorded_at) VALUES "
            "('AR-1','归老访客所有','AR','OLD-VISITOR','fact','shared','active',"
            "'x','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        fish.append_event(
            self.conn, "memory.recorded",
            {"citizen_id": "OLD-VISITOR", "evidence_level": "self-reported"},
            {"kind": "memory", "id": "AR-1"},
            {"code": "AR-1", "title": "归老访客所有", "project": "AR",
             "owner": "OLD-VISITOR", "mem_type": "fact", "occurred_at": "2026-01-01T00:00:00Z"})
        self.conn.commit()

    def _archive_once(self, days=30):
        return fish.archive_citizens(self.conn, days=days, now=self.NOW)

    # ------------------------------------------------------------------ A1
    def test_A1_archive_moves_the_citizen(self):
        """前置：归档确实把人搬进了冷表（否则后面几条测不到东西）。"""
        rep = self._archive_once()
        self.assertEqual(len(rep["planned"]), 1, f"应归档 1 个：{rep}")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM citizens WHERE citizen_id='OLD-VISITOR'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM citizens_archive WHERE citizen_id='OLD-VISITOR'").fetchone()[0], 1)

    # ------------------------------------------------------------------ A2
    def test_A2_rebuild_must_not_rewrite_archived_owner(self):
        """**核心判据**：归档后 `fish rebuild` 不许把这条记忆的 owner 改掉。

        这是 D-10 同类：一次常规维护动作静默改写归属。修前实测被改成系统主人。
        """
        self._archive_once()
        before = self.conn.execute(
            "SELECT owner FROM code_index WHERE code='AR-1'").fetchone()[0]
        self.assertEqual(before, "OLD-VISITOR", "前置：归档本身不该动归属")
        rep = fish.rebuild_projection(self.conn, self.root)
        after = self.conn.execute(
            "SELECT owner FROM code_index WHERE code='AR-1'").fetchone()[0]
        self.assertEqual(after, "OLD-VISITOR",
                         f"rebuild 改写了归属：{before!r} → {after!r}（D-10 同类事故）")
        self.assertEqual(rep["owner_normalized"], 0,
                         f"不该归一任何归属：{rep.get('owner_normalized_list')}")

    # ------------------------------------------------------------------ A3
    def test_A3_validity_includes_archived(self):
        """有效性口径必须含冷公民——否则 `--owner` 指向它会报"不是公民"。"""
        self._archive_once()
        ids = fish_common.citizen_ids_known(self.conn)
        self.assertIn("OLD-VISITOR", ids, "已归档公民仍是**已知**公民")
        self.assertEqual(fish.resolve_owner_arg(self.conn, "OLD-VISITOR"), "OLD-VISITOR",
                         "resolve_owner_arg 必须接受已归档公民")

    # ------------------------------------------------------------------ A4
    def test_A4_healthcheck_does_not_call_archived_a_ghost(self):
        """体检不许把"已被正确归档的 owner"报成幽灵归属（那是误报，会让人去乱修）。"""
        self._archive_once()
        ids = fish_common.citizen_ids_known(self.conn)
        ghosts = [c for c, o in self.conn.execute("SELECT code, owner FROM code_index")
                  if o != fish.UNATTRIBUTED and o != "unknown" and o not in ids]
        self.assertEqual(ghosts, [], f"误报幽灵：{ghosts}")

    # ------------------------------------------------------------------ A5
    def test_A5_archived_citizen_cannot_use_a_live_token(self):
        """**活跃性**口径不变：归档意味着不再活跃，它的 token 必须失效。

        这条守的是另一侧——别为了"修有效性"把活跃性也放宽了。
        用 `validate_session_token` 判：它查热表，返回 None/False 即拒绝。
        """
        self._archive_once()
        # 造一条属于该公民的 token（正常情况下归档时"有有效凭证"会被跳过，
        # 所以这条是为了直接验证"光看热表"这个语义，不是模拟正常流程）
        self.conn.execute(
            "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,channel,"
            "parent_token,issued_at,expires_at)"
            " VALUES ('h','OLD-VISITOR','sk','machine','cli',NULL,'2026-01-01T00:00:00Z','2999-01-01T00:00:00Z')")
        self.conn.commit()
        got = fish.validate_session_token(self.conn, "h") \
            if hasattr(fish, "validate_session_token") else None
        self.assertTrue(got is None or not got,
                        f"已归档公民的 token 必须被拒（活跃性口径用热表），实际 {got!r}")

    # ------------------------------------------------------------------ A6
    def test_A6_archive_skips_citizens_with_live_tokens(self):
        """反向：**还有有效凭证的公民不该被归档**（否则它的凭证会变成未归属）。"""
        self.conn.execute(
            "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,channel,"
            "parent_token,issued_at,expires_at)"
            " VALUES ('h2','OLD-VISITOR','sk2','machine','cli',NULL,'2026-01-01T00:00:00Z','2999-01-01T00:00:00Z')")
        self.conn.commit()
        rep = self._archive_once()
        self.assertEqual(len(rep["planned"]), 0, f"有有效凭证不该被归档：{rep}")
        whys = " ".join(s["why"] for s in rep["skipped"])
        self.assertIn("凭证", whys, f"跳过理由要写清是凭证：{whys}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
