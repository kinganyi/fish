#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-2：`body_sha256` 读路径校验（fish.read_body_path）。

## 审查原文（P2-2）

`p0/fish.py:1074` 计算 `body_sha256`、`:1106` 写进 `memory.recorded`，全仓**没有读取/
校验点**。完整性承诺未兑现——"有字段、无校验"的假安全。

## 判据

  B1 正常写入后读正文 → `state="exact"`、`verify="ok"`
  B2 篡改正文文件 → `state="corrupt"`（**保留正文、明确标注、不抛异常**）
  B3 旧事件没有 `body_sha256` → `state="unverified"`（**不得当成通过**）
  B4 `verify=False` → 跳过校验（state 保持 exact）
  B5 rename 归一：事件里是旧编码、读取用新编码 → 仍能取到该事件的 sha 并通过

变异自证见 `mutation_p2_2.py`（把比对改成 `if False` → B2 必红）。
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fish  # noqa: E402


class BodyShaVerifyTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-p22sha-")
        self.root = Path(self._tmp) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('P','P',NULL,0,'active')")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, title="正文哈希测试", body="原始正文\n"):
        return fish.record_memory(self.conn, self.root, project="P",
                                  title=title, body=body, mem_type="fact")

    def _body_file(self, code):
        rel = self.conn.execute(
            "SELECT file_path FROM code_index WHERE code=?", (code,)).fetchone()[0]
        return self.root / rel

    # ------------------------------------------------------------------ B1
    def test_B1_normal_read_verifies_ok(self):
        code, _ = self._write()
        text, info = fish.read_body_path(self.conn, self.root, code)
        self.assertIn("原始正文", text)
        self.assertEqual(info["state"], "exact", f"正常读取状态不对：{info}")
        self.assertEqual(info["verify"], "ok", f"正常读取应校验通过：{info}")

    # ------------------------------------------------------------------ B2
    def test_B2_tampered_body_reports_corrupt_without_raising(self):
        """篡改正文 → 仍返回正文，但 state=corrupt 且正文前有明确标注。"""
        code, _ = self._write()
        p = self._body_file(code)
        p.write_text(p.read_text(encoding="utf-8") + "\n被外部改坏的一行\n",
                     encoding="utf-8")
        text, info = fish.read_body_path(self.conn, self.root, code)   # 不得抛异常
        self.assertEqual(info["state"], "corrupt", f"篡改没被发现：{info}")
        self.assertEqual(info["verify"], "mismatch")
        self.assertIn("原始正文", text, "corrupt 也必须保留正文")
        self.assertIn("校验失败", text, "corrupt 必须在正文里明确标注")
        self.assertIn("body_sha256", info.get("hint", ""))

    # ------------------------------------------------------------------ B3
    def test_B3_legacy_event_without_sha_is_unverified(self):
        """旧事件（2026-11-19 之前）没有 body_sha256 → 不校验，但必须标 unverified。"""
        code = "P-261126-98"
        rel = "projects/P/P-261126-98__旧数据.md"
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# 旧数据\n\n老正文\n", encoding="utf-8")
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (code, "旧数据", "P", "unknown", "fact", "shared", "active", "[]",
             rel, fish.now_utc(), fish.now_utc()))
        fish.append_event(
            self.conn, "memory.recorded",
            {"citizen_id": "unknown", "evidence_level": "machine"},
            {"kind": "memory", "id": code},
            {"code": code, "title": "旧数据", "body_ref": rel},   # 故意不带 body_sha256
            cause="legacy", commit=False)
        self.conn.commit()
        text, info = fish.read_body_path(self.conn, self.root, code)
        self.assertIn("老正文", text)
        self.assertEqual(info["state"], "unverified",
                         f"旧事件必须标 unverified，不能当成通过：{info}")
        self.assertEqual(info["verify"], "absent")

    # ------------------------------------------------------------------ B4
    def test_B4_no_verify_switch_skips_check(self):
        code, _ = self._write()
        p = self._body_file(code)
        p.write_text(p.read_text(encoding="utf-8") + "\nX\n", encoding="utf-8")
        text, info = fish.read_body_path(self.conn, self.root, code, verify=False)
        self.assertIn("原始正文", text)
        self.assertEqual(info["state"], "exact", f"--no-verify 应跳过校验：{info}")
        self.assertEqual(info["verify"], "skipped")

    # ------------------------------------------------------------------ B5
    def test_B5_rename_normalization_finds_old_event_sha(self):
        """事件里存的是旧编码，读取用新编码：必须按 rename 映射找到同一条事件。"""
        old, rel = self._write(title="会改名的记忆", body="改名正文\n")
        new = "Q-261126-01"
        new_rel = f"projects/P/{new}__会改名的记忆.md"
        old_file = self.root / rel
        new_file = self.root / new_rel
        new_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.rename(new_file)
        self.conn.execute("UPDATE code_index SET code=?, file_path=? WHERE code=?",
                          (new, new_rel, old))
        fish.append_event(
            self.conn, "code.renamed.batch",
            {"citizen_id": "system", "evidence_level": "signed"},
            {"kind": "project", "id": "P"},
            {"mapping": {old: new}, "count": 1}, cause="test", commit=False)
        self.conn.commit()
        text, info = fish.read_body_path(self.conn, self.root, new)
        self.assertIn("改名正文", text)
        self.assertEqual(info["state"], "exact", f"改名后读取状态不对：{info}")
        self.assertEqual(info["verify"], "ok",
                         f"rename 归一后应找到旧编码事件的 sha：{info}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
