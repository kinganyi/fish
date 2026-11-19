#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P0-4 判据：语料按公民隔离。

## 审查方实测的问题（已复核成立）

用 VIEWER-OTHER 这个公民能读到另一个公民 496 字符的段落。根因很具体：

  `fish_corpus.search(..., citizen_id=citizen)` 的 `citizen_id` **一直只被交给
  访问日志，从没进过 WHERE**。而 `fish_corpus_store.FILTER_COLUMNS` 里其实
  早就有 `"citizen": "ss.citizen_id"`——基础设施齐全，只是 MCP 入口没传。

  表结构决定必须 join：`corpus_segment`（50,767 行）没有 citizen_id，
  归属在 `corpus_session.citizen_id`（167 行）。

## 用户 2026-11-18 拍板的处置

**无 token → 明确拒绝并指引取 token**，不静默降级成"只看未归属"
（那会让"查不到"看起来像"没有"）。

## 这批判据为什么能失败

用**真 MCP 子进程**（不直调函数）造两个公民各自的会话，然后：
  · 无 token → 必须报错且**不能泄露任何正文**
  · A 的 token 搜 → 只命中 A 的段，B 的段**一个字都不出现**
  · A 的 token 读 B 的 pointer → 必须拒绝

第二条是核心：如果实现只是"记录 citizen 而不过滤"，B 的文本会照样出现。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish                     # noqa: E402
import fish_corpus_store as store  # noqa: E402

CIT_A = "01CITA0000000000000000001"
CIT_B = "01CITB0000000000000000001"
TOK_A = "fsh_CITIZENA00000000000000000"
TOK_B = "fsh_CITIZENB00000000000000000"

TEXT_A = "甲公民的独有词 青梅煮酒"
TEXT_B = "乙公民的独有词 围魏救赵"


class CorpusIsolationTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="fish-p04-")
        self.root = Path(self._tmpdir) / "data"
        self.root.mkdir(parents=True)
        conn = fish.connect(self.root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            for cid, name in ((CIT_A, "甲"), (CIT_B, "乙")):
                conn.execute(
                    "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                    "created_at) VALUES (?,?,?,?,?,?,?)",
                    (cid, name, "resident", "owner", None, "active", fish.now_utc()))
            for tok, cid in ((TOK_A, CIT_A), (TOK_B, CIT_B)):
                conn.execute(
                    "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,"
                    "channel,parent_token,issued_at,expires_at,revoked_at) "
                    "VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (hashlib.sha256(tok.encode()).hexdigest(), cid,
                     f"dsh:{cid[-4:]}", "machine", "cli", None, fish.now_utc(), None))
            conn.commit()
        finally:
            conn.close()

        db = store.open_db(self.root)
        try:
            for sid, cid, text in (("sid-A", CIT_A, TEXT_A), ("sid-B", CIT_B, TEXT_B)):
                db.execute(
                    "INSERT INTO corpus_session (host,session_id,sid8,project,project_source,"
                    "project_note,title,segments,shadowed,citizen_id) "
                    "VALUES ('dsh',?,?,'PJ','mapped',NULL,'T',1,0,?)",
                    (sid, sid, cid))
                store.upsert_segment(db, {
                    "host": "dsh", "session_id": sid, "loc": "seq=1", "part": 0,
                    "role": "assistant", "occurred_at": "2026-10-27T00:00:00Z",
                    "text": text, "chars": len(text), "redacted_count": 0,
                    "raw_sha256": store.text_sha256(text), "byte_from": 0,
                    "byte_to": 1, "offset_space": "decompressed", "shadowed": 0})
            db.commit()
        finally:
            db.close()

        self.p = subprocess.Popen(
            [sys.executable, str(HERE / "mcp_server.py"), "--root", str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=dict(os.environ))
        self._id = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {}, "clientInfo": {"name": "p04", "version": "0"}})

    def tearDown(self):
        try:
            self.p.kill()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _rpc(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed: {self.p.stderr.read()[:400]}")
        return json.loads(line)

    def _corpus(self, **args):
        return self._rpc("tools/call", {"name": "corpus", "arguments": args})

    @staticmethod
    def _text(resp):
        return resp["result"]["content"][0]["text"]

    # ------------------------------------------------------------- 主判据
    def test_no_token_is_rejected_with_guidance(self):
        """**无 token 必须明确拒绝，并告诉调用方怎么拿 token。**"""
        text = self._text(self._corpus(action="search", query="独有词"))
        self.assertIn("凭证", text, f"没说要凭证：{text[:200]}")
        self.assertIn("token", text.lower(), "没给取 token 的指引")
        # 拒绝时**不许泄露任何正文**
        self.assertNotIn("青梅煮酒", text, "被拒绝的调用却泄露了甲的正文")
        self.assertNotIn("围魏救赵", text, "被拒绝的调用却泄露了乙的正文")

    def test_citizen_a_cannot_see_citizen_b(self):
        """**核心判据**：甲搜"独有词"，只能命中甲的段，乙的一个字都不许出现。

        如果实现只是把 citizen 记进日志而不过滤，乙的文本会照样出现。
        """
        text = self._text(self._corpus(action="search", query="独有词", token=TOK_A))
        self.assertIn("青梅煮酒", text, f"甲看不到自己的段：{text[:300]}")
        self.assertNotIn("围魏救赵", text,
                         f"**跨公民泄露**：甲搜到了乙的段落\n{text[:400]}")
        self.assertIn("hits=1", text, f"命中数不对（应只有 1 条）：{text[:300]}")

    def test_each_citizen_sees_only_their_own(self):
        """对称性：乙也只能看到自己的。"""
        text = self._text(self._corpus(action="search", query="独有词", token=TOK_B))
        self.assertIn("围魏救赵", text)
        self.assertNotIn("青梅煮酒", text, f"乙搜到了甲的段落\n{text[:400]}")

    def test_reading_another_citizens_pointer_is_refused(self):
        """`pointer` 是「任意段的地址」，读之前必须校验归属。"""
        resp = self._corpus(action="read", pointer="log:dsh:sid-B#seq=1", token=TOK_A)
        body = json.dumps(resp, ensure_ascii=False)
        self.assertNotIn("围魏救赵", body,
                         f"甲用 pointer 读到了乙的段落：{body[:400]}")
        self.assertTrue(
            resp.get("result", {}).get("isError") or "不属于" in body,
            f"读别人的段落没被拒绝：{body[:300]}")

    def test_own_pointer_is_readable(self):
        """对照组：读自己的段落必须成功，不能为了安全把功能砍掉。"""
        resp = self._corpus(action="read", pointer="log:dsh:sid-A#seq=1", token=TOK_A)
        body = json.dumps(resp, ensure_ascii=False)
        self.assertIn("青梅煮酒", body, f"读自己的段落失败了：{body[:400]}")


    def test_cli_path_is_isolated_too(self):
        """**第六轮评审 🔴-1**：隔离原来只装在 MCP 门口。

        评审官现场演示：同一个库、两个公民的语料，给甲的身份跑 CLI——

            fish corpus read log:dsh:<乙的 sid>#seq=1  → rc=0，拿到乙的原话

        **执行层没有锁，只有一个调用方有锁。** 而可见性清单里
        `get_segment` 的豁免理由正是"走 F-P0-4 的 citizen 隔离"——
        **那条豁免的地基是假的。**

        这条判据走**执行层**（`fish_corpus.run_cli`），不经过 MCP，
        所以它测的是"锁在不在执行层"，不是"MCP 记不记得上锁"。
        """
        import fish_corpus
        from fish_corpus import store as _store

        def _actor(cid):
            return lambda: cid

        # ① search：甲的检索结果里不许出现乙的独有词
        rc, lines, _acc = fish_corpus.run_cli(
            "search", root=self.root, target="独有词", actor_citizen=_actor(CIT_A))
        blob = "\n".join(lines)
        self.assertNotIn("围魏救赵", blob, "CLI search 把乙公民的语料给了甲")
        self.assertIn("青梅煮酒", blob, "甲连自己的都搜不到（过滤过头）")

        # ② read：甲读乙的门牌必须被拒
        rc2, l2, _a2 = fish_corpus.run_cli(
            "read", root=self.root, target="log:dsh:sid-B#seq=1",
            actor_citizen=_actor(CIT_A))
        self.assertNotIn("围魏救赵", "\n".join(l2),
                         "CLI read 读到了别的公民的正文原文")
        self.assertNotEqual(rc2, 0, "跨公民读取居然返回成功")

        # ③ 甲读自己的门牌必须成功（否则是过滤过头）
        _rc3, l3, _a3 = fish_corpus.run_cli(
            "read", root=self.root, target="log:dsh:sid-A#seq=1",
            actor_citizen=_actor(CIT_A))
        self.assertIn("青梅煮酒", "\n".join(l3),
                      "甲读自己的语料也被挡了（过滤过头）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
