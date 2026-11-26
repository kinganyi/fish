#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-5 判据 · 有效性判定统一（2026-11-26 外部审查 W2-b）。

## 背景

`fish_activity.SQL_SESSION_TOKEN_CITIZEN` 旧 SQL 只看「未吊销 + 未过期」，
而 `fish.validate_session_token` 还要求**公民状态 = active**。
于是同一个活 token，MCP 面拒绝、hook 采集面却仍把它解析成公民——判定漂移。

## 判据

  V1  retired 公民的活 token → `SQL_SESSION_TOKEN_CITIZEN` 返回 None
  V2  active 公民的活 token → 返回 citizen_id
  V3  `resolve_citizen` 与 `fish.validate_session_token` 对拍（active / retired / missing / revoked / expired / archived 六态）
  V4  吊销 / 过期 token 仍按旧口径失效（不能被新 JOIN 误伤）
  V5  **归档公民**的活 token → SQL None，且与 `validate_session_token` 一致
      （归档是搬行：冷表里的行可能仍 `status='active'`，只查 status 会误判有效）

变异：去掉 `c.status='active'` → V1 必须红（见 `mutation_perf_validity.py`）；
把 `JOIN citizens` 改成 `JOIN citizens_all`（放进冷表归档行）→ V5 必须红。
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_activity  # noqa: E402


def _iso(offset_sec=0):
    import datetime
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_sec)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ActivityValidityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-validity-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        now = _iso()
        self.addCleanup(self._cleanup)
        rows = [
            ("01CIT_ACTIVE", "active-citizen", "visitor", "active", None, now),
            ("01CIT_RETIRED", "retired-citizen", "visitor", "retired", None, now),
            ("01CIT_MISSING", "missing-citizen", "visitor", "missing", None, now),
        ]
        for citizen_id, name, kind, status, host_session_key, created in rows:
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,"
                "host_session_key,created_at) VALUES (?,?,?,?,?,?)",
                (citizen_id, name, kind, status, host_session_key, created))
        # 归档公民：**只在冷表里**，且 status 仍是 'active'（归档是搬行，不清 status）。
        # 这正是「只查 citizens_all.status」会误判的场景：热表查不到它。
        self.conn.execute(
            "INSERT INTO citizens_archive (citizen_id,display_name,kind,status,"
            "created_at,archived_at) VALUES (?,?,?,?,?,?)",
            ("01CIT_ARCHIVED", "archived-citizen", "visitor", "active",
             _iso(), _iso()))
        self.tokens = {
            "active": self._insert_token("01CIT_ACTIVE", "dsh:valid-active"),
            "retired": self._insert_token("01CIT_RETIRED", "dsh:valid-retired"),
            "missing": self._insert_token("01CIT_MISSING", "dsh:valid-missing"),
            "archived": self._insert_token("01CIT_ARCHIVED", "dsh:valid-archived"),
            "revoked": self._insert_token("01CIT_ACTIVE", "dsh:valid-revoked",
                                          revoked_at=now),
            "expired": self._insert_token("01CIT_ACTIVE", "dsh:valid-expired",
                                          expires_at=_iso(-60)),
        }
        self.conn.commit()

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert_token(self, citizen_id, session_key, *, revoked_at=None, expires_at=None):
        plain = "fsh_TEST_" + session_key.replace(":", "_")
        self.conn.execute(
            "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,"
            "channel,issued_at,expires_at,revoked_at) VALUES (?,?,?,?,?,?,?,?)",
            (fish.hash_session_token(plain), citizen_id, session_key, "machine", "hook",
             _iso(), expires_at, revoked_at))
        return plain

    def _sql_lookup(self, session_key):
        row = self.conn.execute(
            fish_activity.SQL_SESSION_TOKEN_CITIZEN, (session_key, _iso())).fetchone()
        return row[0] if row else None

    # ---- V1 / V2：SQL 本身 ----

    def test_v1_retired_citizen_token_is_invalid(self):
        self.assertIsNone(self._sql_lookup("dsh:valid-retired"),
                          "退役公民的活 token 仍被 SQL 判成有效（缺 status JOIN）")

    def test_v2_active_citizen_token_is_valid(self):
        self.assertEqual(self._sql_lookup("dsh:valid-active"), "01CIT_ACTIVE")

    # ---- V3：与 fish.validate_session_token 对拍 ----

    def test_v3_resolve_citizen_matches_validate_session_token(self):
        cases = [
            ("dsh:valid-active", self.tokens["active"]),
            ("dsh:valid-retired", self.tokens["retired"]),
            ("dsh:valid-missing", self.tokens["missing"]),
            ("dsh:valid-archived", self.tokens["archived"]),
            ("dsh:valid-revoked", self.tokens["revoked"]),
            ("dsh:valid-expired", self.tokens["expired"]),
        ]
        for session_key, token in cases:
            by_sql = self._sql_lookup(session_key)
            by_validate = fish.validate_session_token(self.conn, token)
            self.assertEqual(by_sql, by_validate,
                             f"{session_key}: hook 侧={by_sql!r} 与 MCP 侧={by_validate!r} 漂移")

    # ---- V4：旧口径（吊销/过期）不被新 JOIN 误伤 ----

    def test_v4_revoked_and_expired_still_invalid(self):
        self.assertIsNone(self._sql_lookup("dsh:valid-revoked"))
        self.assertIsNone(self._sql_lookup("dsh:valid-expired"))

    # ---- V5：归档公民（冷表 status 仍 active）必须判无效 ----

    def test_v5_archived_citizen_token_is_invalid_and_matches_validate(self):
        """归档反例：冷表 status='active' ≠ 有效；SQL 与 validate 必须一致。"""
        row = self.conn.execute(
            "SELECT status,archived_at FROM citizens_all WHERE citizen_id='01CIT_ARCHIVED'"
        ).fetchone()
        self.assertIsNotNone(row, "前置：归档公民必须在 citizens_all 里（否则不是反例）")
        self.assertEqual(row[0], "active", "前置：归档行 status 仍是 active")
        self.assertIsNotNone(row[1], "前置：归档行必须带 archived_at")
        self.assertIsNone(self._sql_lookup("dsh:valid-archived"),
                          "归档公民的活 token 仍被 SQL 判成有效（缺 archived_at 过滤）")
        self.assertIsNone(fish.validate_session_token(self.conn, self.tokens["archived"]),
                          "validate_session_token 也不该把归档公民判成有效")
        self.assertEqual(self._sql_lookup("dsh:valid-archived"),
                         fish.validate_session_token(self.conn, self.tokens["archived"]),
                         "归档公民：hook 侧与 MCP 侧判定漂移")

    def test_v4b_active_citizen_join_does_not_require_host_session_key(self):
        """JOIN 只按 citizen_id+status，不要求 citizens.host_session_key（那是另一级兜底）。"""
        row = self.conn.execute(
            "SELECT host_session_key FROM citizens WHERE citizen_id='01CIT_ACTIVE'").fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(self._sql_lookup("dsh:valid-active"), "01CIT_ACTIVE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
