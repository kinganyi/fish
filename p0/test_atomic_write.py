#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P0-1 判据：记忆写入必须**单事务**。

## 审查方实测的问题（已复核成立）

`record_memory` 旧实现是三次独立提交：

    conn.execute("INSERT INTO code_index ...")
    conn.commit()                            # ← 投影先落
    append_event(conn, "memory.recorded")    # ← 事件后写（内部 commit）
    _index_one_fts(conn, code, ...)          # ← FTS 最后（内部硬编码 commit）

事件或 FTS 失败 → **投影有、事件无、FTS 无**。而 `query` 照常返回那条记忆，
所以**没有任何东西会红**——直到有人去重建或审计才发现。

## 这批判据为什么能失败

关键是**注入真失败**，不是"看代码写了 try"。所以用 monkeypatch 把
`_index_one_fts` / `append_event` 换成会抛异常的版本，然后断言三张表
**行数全部回到写前**、且**正文文件不残留**。

如果实现退回"先 commit 投影"，第一条断言立刻红。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402


class AtomicWriteTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        # 编码分配要查 code_registry；测试里直接登记前缀（`cmd_project_add` 会自己
        # 开连接，不适合内存/临时库）
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq)"
            " VALUES ('P','P','2026-11-18',0)")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _counts(self):
        return {
            "code_index": self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0],
            "events": self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE type='memory.recorded'").fetchone()[0],
            "fts": self.conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0],
        }

    def _files(self):
        p = self.data / "projects" / "P"
        return sorted(x.name for x in p.glob("*.md")) if p.is_dir() else []

    def _record(self):
        return fish.record_memory(self.conn, self.data, project="P", title="原子性测试",
                                  body="# 正文\n\n内容\n", mem_type="fact")

    # ------------------------------------------------------------- 主判据
    def test_fts_failure_rolls_back_everything(self):
        """**F-P0-1 的直接探针**：FTS 写失败时，投影/事件/正文全部不许留下。"""
        before = self._counts()
        real = fish._index_one_fts

        def boom(*a, **kw):
            raise sqlite3.OperationalError("模拟 FTS 写失败")

        with mock.patch.object(fish, "_index_one_fts", boom):
            with self.assertRaises(sqlite3.OperationalError):
                self._record()

        after = self._counts()
        self.assertEqual(after, before,
                         f"FTS 失败后状态不一致：{before} → {after}"
                         "（投影/事件/FTS 应全部回滚）")
        self.assertEqual(self._files(), [], "正文文件没被清理（留下孤儿文件）")

    def test_event_failure_rolls_back_everything(self):
        """事件写失败同样必须整体回滚——这是"投影先 commit"最典型的暴露口。"""
        before = self._counts()
        real_append = fish.append_event

        def boom(*a, **kw):
            raise sqlite3.OperationalError("模拟事件写失败")

        with mock.patch.object(fish, "append_event", boom):
            with self.assertRaises(sqlite3.OperationalError):
                self._record()

        after = self._counts()
        self.assertEqual(after, before, f"事件失败后状态不一致：{before} → {after}")
        self.assertEqual(self._files(), [], "正文文件没被清理")

    # ------------------------------------------------------------- 对照组
    def test_success_writes_all_three(self):
        """**未变异的对照组必须绿**——否则"全红"可能只是判据读空。"""
        before = self._counts()
        code, *_ = self._record()
        after = self._counts()
        self.assertEqual(after["code_index"], before["code_index"] + 1)
        self.assertEqual(after["events"], before["events"] + 1)
        self.assertEqual(after["fts"], before["fts"] + 1)
        self.assertEqual(len(self._files()), 1)
        hits = self.conn.execute(
            "SELECT code FROM memory_fts WHERE code=?", (code,)).fetchall()
        self.assertEqual(len(hits), 1, "写成功却查不到全文索引")

    def test_retry_after_failure_does_not_duplicate(self):
        """失败后重试不许留下重复（审查方的验收判据之一）。"""
        def boom(*a, **kw):
            raise sqlite3.OperationalError("模拟失败")

        with mock.patch.object(fish, "_index_one_fts", boom):
            with self.assertRaises(sqlite3.OperationalError):
                self._record()
        # 重试（这次不注入失败）
        self._record()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0], 1,
            "重试后出现重复投影")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events WHERE type='memory.recorded'")
            .fetchone()[0], 1, "重试后出现重复事件")


class FtsCommitParamTest(unittest.TestCase):
    """`_index_one_fts` 必须能被调用方控制事务边界。"""

    def test_commit_false_does_not_commit(self):
        import fish_common
        conn = sqlite3.connect(":memory:")
        conn.executescript(fish.SCHEMA)
        # 先塞一条，然后不提交地删掉——若函数内部偷偷 commit，回滚就失效
        conn.execute("INSERT INTO memory_fts (code,title_idx,body_idx) VALUES ('A','a','a')")
        conn.commit()
        fish_common._index_one_fts(conn, "B", "b", [], "b", commit=False)
        conn.rollback()
        rows = [r[0] for r in conn.execute("SELECT code FROM memory_fts ORDER BY code")]
        self.assertEqual(rows, ["A"],
                         f"commit=False 仍提交了（回滚没生效）：{rows}")


class BodyAtomicityTest(unittest.TestCase):
    """**F-P2-8**：正文原来是 `Path.write_text`——直接写。

    崩溃/断电在这行的中间 → 盘上半个 Markdown，前半有后半没有，
    **而投影里那条记录看起来完全正常**。没有任何东西会告诉你它坏了。

    这条判据不模拟崩溃（那要子进程 + 信号，脆），而是问**结构问题**：
    正文目录里出现过的临时文件必须被清理，且写完的内容必须逐字节完整。
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.mkdtemp(prefix="fish-p28-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq)"
            " VALUES ('P','P','2026-11-19',0)")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass

    def test_body_is_written_atomically(self):
        """走 `fish_atomic.write_text_atomic`（结构判据），且不留 .tmp。"""
        import fish_atomic
        code, _ = fish.record_memory(
            self.conn, self.root, project="P", title="原子正文",
            body="正文内容\n第二行\n", mem_type="fact")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT file_path FROM code_index WHERE code=?", (code,)).fetchone()
        target = self.root / row[0]
        self.assertTrue(target.is_file(), "正文没写出来")
        self.assertIn("正文内容", target.read_text(encoding="utf-8"),
                      "正文内容不完整")
        leftovers = list(self.root.rglob("*.tmp"))
        self.assertEqual(leftovers, [], f"正文目录留下了临时文件：{leftovers}")
        # **接线判据**：上面的"内容对不对 / 有没有 .tmp"在非原子写下**也成立**
        # （直接 write_text 的内容同样完整、同样不留 .tmp）。实测把实现换回
        # `write_text` 做变异 → 前面两条仍绿。所以必须再问一句：
        # **record_memory 到底有没有走原子写？**
        import ast as _ast
        # 2026-11-26（W3-b · 144 刀二）：`record_memory` 搬进 fish_memory.py——
        # 判据本意是"它走没走原子写"，函数搬到哪就查哪个文件（断言一字未改）。
        src = (Path(__file__).resolve().parent / "fish_memory.py").read_text(
            encoding="utf-8")
        target = None
        for node in _ast.walk(_ast.parse(src)):
            if (isinstance(node, _ast.FunctionDef)
                    and node.name == "record_memory"):
                target = node
                break
        self.assertIsNotNone(target, "找不到 record_memory——判据失去靶点")
        names = {n.attr for n in _ast.walk(target) if isinstance(n, _ast.Attribute)}
        self.assertIn("write_text_atomic", names,
                      "record_memory 没走 fish_atomic.write_text_atomic——"
                      "正文又变回非原子写了（崩在中途会留半个文件）")

    def test_atomic_helper_cleans_up_on_failure(self):
        """写失败时**不许**留临时文件——否则崩一次攒一个。"""
        import fish_atomic
        from pathlib import Path
        d = Path(self._tmp) / "at"
        d.mkdir()

        class _Boom:
            def __str__(self):
                raise RuntimeError("boom")

        with self.assertRaises(Exception):
            fish_atomic.write_text_atomic(d / "x.md", _Boom())
        self.assertEqual(list(d.rglob("*.tmp")), [],
                         "失败后留下了临时文件")


    def test_atomic_matches_write_text_bytes(self):
        """**逐字节等价**必须被**验**，不能只在 docstring 里断言。

        第七轮评审实测：第一版 `write_text_atomic` 用 `newline=""`（关闭转换），
        而 `Path.write_text` 用 `newline=None`（转平台换行）。**Windows 上产出
        CRLF vs LF，字节不等**——而 docstring 写着"逐字节等价"。

        生产后果：778 个正文里 774 CRLF / 3 LF，3 个的 mtime 都在那次提交之后。

        这条判据直接比字节：**同一个字符串，两条路写出来必须一模一样。**
        """
        import fish_atomic
        d = Path(tempfile.mkdtemp(prefix="fish-p28b-"))
        a = d / "via_write_text.md"
        b = d / "via_atomic.md"
        text = "甲\n乙\n丙\n"
        a.write_text(text, encoding="utf-8")
        fish_atomic.write_text_atomic(b, text)
        self.assertEqual(
            a.read_bytes(), b.read_bytes(),
            f"两条路写出来的字节不同——『逐字节等价』这句话不成立：\n"
            f"  Path.write_text   {a.read_bytes()!r}\n"
            f"  write_text_atomic {b.read_bytes()!r}")


    def test_event_carries_the_body_hash(self):
        """**F-P2-8 后半**：事件里的 `body_sha256` 必须等于**落盘正文**的哈希。

        没有这个字段时，正文**写出之后**被改坏（磁盘损坏/外部篡改）查不出来——
        事件里只有 `body_ref`（一个路径），引用不校验内容。

        判据不只查字段在不在，还**真的去算**：读回正文文件，算 sha256，
        与事件里的值比。哈希算的是落盘字节，不是入参 `body`（后者少了标题行）。
        """
        import hashlib
        import json
        code, _ = fish.record_memory(
            self.conn, self.root, project="P", title="哈希测试",
            body="正文内容\n第二行\n", mem_type="fact")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT file_path FROM code_index WHERE code=?", (code,)).fetchone()
        on_disk = (self.root / row[0]).read_bytes()
        want = hashlib.sha256(on_disk).hexdigest()

        payload = json.loads(self.conn.execute(
            "SELECT payload FROM events WHERE type='memory.recorded' "
            "AND payload LIKE ?", (f"%{code}%",)).fetchone()[0])
        self.assertIn("body_sha256", payload,
                      "事件 payload 里没有 body_sha256——正文被改坏查不出来")
        self.assertEqual(payload["body_sha256"], want,
                         "事件里的哈希与落盘正文对不上")

    def test_a_tampered_body_is_detectable(self):
        """改一个字节，哈希就不再匹配——**这才是这个字段的用处**。"""
        import hashlib
        import json
        code, _ = fish.record_memory(
            self.conn, self.root, project="P", title="篡改测试",
            body="原始正文\n", mem_type="fact")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT file_path FROM code_index WHERE code=?", (code,)).fetchone()
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM events WHERE type='memory.recorded' "
            "AND payload LIKE ?", (f"%{code}%",)).fetchone()[0])
        old_hash = payload["body_sha256"]
        # 篡改：往正文里塞一个字
        p = self.root / row[0]
        p.write_text(p.read_text(encoding="utf-8") + "偷偷加的一行\n",
                     encoding="utf-8")
        now = hashlib.sha256(p.read_bytes()).hexdigest()
        self.assertNotEqual(now, old_hash,
                            "篡改后哈希没变——这个字段没有校验能力")


if __name__ == "__main__":
    unittest.main(verbosity=2)
