#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P1-4 判据：知识库迁移必须可中断、可诊断、可回滚。

## 审查方描述的问题（已复核成立）

`kb_vault_relocate` 的结构是：

    for rel in plan["rels"]:
        shutil.move(...)           # ← 逐个搬
    ...
    UPDATE kb_vault SET path=?     # ← 搬完才改登记

**中断点就在这两者之间**。搬到一半进程死了 → 文件一半在源、一半在目标，
而登记**仍指向源**（那个已经被搬空的目录）。对外表现是"库突然少了东西"，
且没有任何记录说明发生过什么。

10.7 GB / 2,526 个文件，这个窗口很长。

## 一个必须先说的设计陷阱（我做第一版时踩到了）

最直觉的修法是"把 `status` 置成 `relocating` 当锁"。但那会引入**更糟**的问题：

    kb_vault_get(conn, key) →
        if vault["status"] != "active":
            raise KBError("vault_missing", ...)

一旦崩在中途，这个库就从**所有**查询里消失了——比"文件搬一半"更难救。
`status` 是用户面的启用/停用开关，不该被运维过程占用。

所以锁放在 `note` 字段的 journal 里（`state` = moving / done / rolled_back），
`status` 始终是 `active`。**"库消失"不是"迁移进行中"的表达方式。**

## 这批判据为什么能失败

第二条注入一次真实的搬移失败（让第 2 个文件 move 抛 OSError），然后断言：
源目录的文件**一个不少**（真回滚了），note 记成 `rolled_back`。
第一版（没有回滚）在这条上必红——半搬的文件留在目标侧，源侧永久缺失。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish_kb as K  # noqa: E402


def _conn():
    """只建 schema 的临时库（与 test_vault_w2 同口径）。"""
    import fish
    tmp = tempfile.mkdtemp(prefix="fish-p14-db-")
    data = Path(tmp) / "data"
    data.mkdir(parents=True)
    conn = fish.connect(data)
    conn.executescript(fish.SCHEMA)
    fish.migrate(conn)
    return conn


class RelocateJournalTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-p14-")
        self.root = Path(self._tmp)
        self.conn = _conn()
        self.init = K.kb_vault_init(self.conn, self.root, name="迁移库")
        self.src = Path(self.init["root"])

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed(self, n=3):
        """在源库里放 n 个文件（每层目录一个，覆盖 mkdir 逻辑）。"""
        for i in range(n):
            p = self.src / f"d{i}" / f"n{i}.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# 第 {i} 篇\n\n内容 {i}\n", encoding="utf-8")
        return n

    def _src_files(self):
        """源库里的**全部**文件（不只是 .md）。

        迁移计划按全部文件算——库初始化会自己写非 .md 的附属文件。
        只数 .md 会让"总数对不上"看起来像 bug，其实是我数错了。
        """
        return sorted(str(p.relative_to(self.src)).replace("\\", "/")
                      for p in self.src.rglob("*") if p.is_file())

    def _note(self):
        return json.loads(K.kb_vault_get(self.conn, "迁移库")["note"])

    # ------------------------------------------------------------- 正常路径
    def test_successful_relocate_records_done(self):
        """成功搬完后 note 必须记 `done` 与总数——否则事后无法回答"搬完了吗"。"""
        self._seed()
        total_before = len(self._src_files())   # 库初始化自带文件，不能假定等于 seed 数
        dst = self.root / "目标库"
        K.kb_vault_relocate(self.conn, "迁移库", dst,
                            move_files=True, dry_run=False, verify=False)
        seg = self._note()["relocate"]
        self.assertEqual(seg["state"], "done", f"状态不对：{seg}")
        self.assertEqual(seg["total"], total_before,
                         f"plan 总数与源文件数不一致：{seg} vs {total_before}")
        self.assertEqual(seg["moved"], seg["total"], "moved 没走满")
        self.assertTrue((dst / "d0" / "n0.md").exists(), "文件没到目标侧")

    def test_status_stays_active(self):
        """**反向判据**：迁移前后 `status` 都必须是 `active`。

        如果谁把锁做回 status 上，中途崩了库就会从所有查询里消失——
        这条就是那个坑的哨兵。
        """
        self._seed()
        before = K.kb_vault_get(self.conn, "迁移库")["status"]
        K.kb_vault_relocate(self.conn, "迁移库", self.root / "目标库",
                            move_files=True, dry_run=False, verify=False)
        after = K.kb_vault_get(self.conn, "迁移库")["status"]
        self.assertEqual((before, after), ("active", "active"),
                         "迁移把 status 改了——崩在中途时库会消失")

    # ------------------------------------------------------------- 核心：回滚
    def test_interrupted_relocate_rolls_back(self):
        """**核心判据**：搬到一半失败 → 源目录文件一个不少。

        注入方式：让 `shutil.move` 在第 2 次调用时抛 OSError（真实故障形态：
        目标盘满 / 权限拒绝 / 文件被占用）。第一版没有回滚，这条必红。
        """
        self._seed()
        before = self._src_files()
        self.assertGreaterEqual(len(before), 3, "种数据没生效")

        import fish_kb
        real_move = fish_kb.shutil.move
        calls = {"n": 0}

        def flaky(src, dst, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "模拟目标盘满")
            return real_move(src, dst, *a, **kw)

        fish_kb.shutil.move = flaky
        try:
            with self.assertRaises(OSError):
                K.kb_vault_relocate(self.conn, "迁移库", self.root / "目标库",
                                    move_files=True, dry_run=False, verify=False)
        finally:
            fish_kb.shutil.move = real_move

        after = self._src_files()
        self.assertEqual(after, before,
                         f"**回滚失败**：源目录少/多了文件\n  前 {before}\n  后 {after}")

    def test_interrupted_relocate_records_rolled_back(self):
        """回滚之后必须留下证据——否则"库变少了"又变成无法解释的事。"""
        self._seed()
        import fish_kb
        real_move = fish_kb.shutil.move
        calls = {"n": 0}

        def flaky(src, dst, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "模拟目标盘满")
            return real_move(src, dst, *a, **kw)

        fish_kb.shutil.move = flaky
        try:
            with self.assertRaises(OSError):
                K.kb_vault_relocate(self.conn, "迁移库", self.root / "目标库",
                                    move_files=True, dry_run=False, verify=False)
        finally:
            fish_kb.shutil.move = real_move

        seg = self._note()["relocate"]
        self.assertTrue(seg["state"].startswith("rolled_back"),
                        f"note 没记回滚：{seg}")

    # ------------------------------------------------------------- 锁
    def test_relocate_refuses_when_a_previous_run_is_unfinished(self):
        """上次没收尾时，不许当成"干净现场"再搬一次。"""
        self._seed()
        vault = K.kb_vault_get(self.conn, "迁移库")
        self.conn.execute(
            "UPDATE kb_vault SET note=? WHERE vault_id=?",
            (json.dumps({"relocate": {"from": "a", "to": "b", "moved": 1,
                                      "total": 3, "state": "moving"}},
                        ensure_ascii=False), vault["vault_id"]))
        self.conn.commit()
        with self.assertRaises(K.KBError) as ctx:
            K.kb_vault_relocate(self.conn, "迁移库", self.root / "目标库",
                                move_files=True, dry_run=False, verify=False)
        self.assertIn("没有收尾", str(ctx.exception))

    def test_write_refuses_while_relocating(self):
        """**搬到一半还在写**会让两侧都不完整，而且回滚会把这次写入一起搬回去。"""
        self._seed()
        vault = K.kb_vault_get(self.conn, "迁移库")
        self.conn.execute(
            "UPDATE kb_vault SET note=? WHERE vault_id=?",
            (json.dumps({"relocate": {"from": "a", "to": "b", "moved": 1,
                                      "total": 3, "state": "moving"}},
                        ensure_ascii=False), vault["vault_id"]))
        self.conn.commit()
        row = K.kb_vault_get(self.conn, "迁移库")
        with self.assertRaises(K.KBError) as ctx:
            K._kb_write_impl(self.conn, self.root, row, "x.md", action="create",
                             content="# x\n", frontmatter=None, base_sha256=None,
                             reason=None, allow_sensitive=False, citizen_id=None,
                             account_id=None, via="test")
        self.assertIn("迁移进行中", str(ctx.exception))

    # ------------------------------------------------------------- 解析器
    def test_relocate_state_does_not_guess(self):
        """读不出来一律返回空——"没有迁移记录"与"迁移完成了"是两种事实。"""
        for note in (None, "", "不是 JSON", "[]", '{"other": 1}',
                     '{"relocate": "x"}'):
            self.assertEqual(K._relocate_state({"note": note}), "",
                             f"note={note!r} 被猜成了有状态")


if __name__ == "__main__":
    unittest.main(verbosity=2)
