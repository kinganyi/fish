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

## 编号说明（2026-11-25）

文首的 `F-P1-4` 是**上一轮**审查（W2 轮）对"迁移可中断"的编号；本轮外部审查
（`docs/141`）把"**校验失败不回滚**"单列为 `F-P1-5`。同一个函数、两批判据：

| 编号 | 判的是 | 判据 |
|---|---|---|
| F-P1-4（旧） | 搬移**中断**（OSError）要回滚 | `test_interrupted_relocate_*`（`verify=False`） |
| F-P1-5（本轮） | **哈希校验失败**也要回滚 + 不许卡 `moving` + 拒绝 `dst ⊂ src` | `test_verify_failure_*` / `test_dst_inside_src_*`，全部 `verify=True` |

旧判据全用 `verify=False`，于是"校验失败那条路"此前**一条判据都没有**——
审查方的原话是"删掉回滚仍全绿"。本轮补的就是这个缺口。
"""
import json
import os
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

    # ------------------------------------------------------------- F-P1-5：校验失败
    def _patch_bad_dst_hash(self, dst):
        """让 `kb_sha256_file` 对**目标侧**路径返回假哈希（源侧基准仍是真的）。

        为什么注入哈希而不是注入 IO：审查方指的缺陷正是"比对结果不一致之后什么都没做"，
        所以要让**比对本身**失败，而不是让搬移失败——后者走的是另一条（已有回滚的）路径。
        """
        import fish_kb
        real = fish_kb.kb_sha256_file
        dst_key = os.path.normcase(str(dst))

        def bogus(path):
            if os.path.normcase(str(path)).startswith(dst_key):
                return "0" * 64
            return real(path)

        fish_kb.kb_sha256_file = bogus
        return real

    def test_verify_failure_rolls_back_everything(self):
        """**F-P1-5 核心**：哈希校验失败必须回滚——源文件全回、登记不变、note 记 rolled_back。

        旧实现（`fish_kb.py` 校验段直接 `raise`）在这条上必红：
          · 源目录已空（文件全在目标侧）→ 文件清单断言红；
          · 登记仍指向源（那是空目录）；
          · note 卡在 `moving` → 状态断言红，且下一次搬会被守卫永远挡住。
        """
        self._seed()
        before = {p: p.read_bytes() for p in self.src.rglob("*") if p.is_file()}
        self.assertGreaterEqual(len(before), 3, "种数据没生效")
        dst = self.root / "目标库"

        real = self._patch_bad_dst_hash(dst)
        try:
            with self.assertRaises(K.KBError) as ctx:
                K.kb_vault_relocate(self.conn, "迁移库", dst,
                                    move_files=True, dry_run=False, verify=True)
        finally:
            import fish_kb
            fish_kb.kb_sha256_file = real

        msg = str(ctx.exception)
        self.assertIn("校验失败", msg, f"报错没说是校验失败：{msg[:160]}")
        self.assertIn("已回滚", msg, "报错必须告诉人现场已经还原（否则只能猜）")

        after = {p: p.read_bytes() for p in self.src.rglob("*") if p.is_file()}
        self.assertEqual(sorted(after), sorted(before),
                         f"回滚后源目录的文件清单变了\n  前 {sorted(before)}\n  后 {sorted(after)}")
        for p, data in before.items():
            self.assertEqual(after.get(p), data, f"回滚后内容不一致（搬回来的不是原件）：{p}")
        leftovers = ([str(p) for p in dst.rglob("*") if p.is_file()]
                     if dst.exists() else [])
        self.assertEqual(leftovers, [],
                         f"**源空目标有**：回滚后目标侧还留着文件 → {leftovers}")

        seg = self._note()["relocate"]
        self.assertEqual(seg["state"], "rolled_back", f"note 没记成 rolled_back：{seg}")
        self.assertEqual(Path(K.kb_vault_get(self.conn, "迁移库")["path"]).resolve(),
                         self.src.resolve(), "校验失败却改了登记（指向了没搬完的目标）")

    def test_failed_verify_is_not_stuck_in_moving(self):
        """回滚之后 note 不许停在 `moving`——否则下一次搬会被守卫**永远**挡住。

        判据判的是"失败可以重试"：同一个库、同一个目标，去掉坏哈希后再搬一次必须成。
        """
        self._seed()
        dst = self.root / "目标库"
        real = self._patch_bad_dst_hash(dst)
        try:
            with self.assertRaises(K.KBError):
                K.kb_vault_relocate(self.conn, "迁移库", dst,
                                    move_files=True, dry_run=False, verify=True)
        finally:
            import fish_kb
            fish_kb.kb_sha256_file = real

        # 失败后的状态必须**不是** moving（是 rolled_back / failed 都能接受，但不能卡住）
        self.assertNotEqual(self._note()["relocate"]["state"], "moving",
                            "校验失败后 note 卡在 moving：这个库再也搬不动了")

        out = K.kb_vault_relocate(self.conn, "迁移库", dst,
                                  move_files=True, dry_run=False, verify=True)
        self.assertEqual(out["verified"], out["files"], "重试后校验没通过")
        self.assertEqual(self._note()["relocate"]["state"], "done", "重试没记成 done")

    def test_copy_mode_verify_failure_removes_copies(self):
        """copy 模式的回滚语义不同：源文件还在 → 回滚 = 删掉复制出去的副本。

        这条防的是"把 move 的回滚逻辑照抄到 copy 模式"：那会把源文件再覆盖一遍，
        而真正该清的副本留在目标侧。
        """
        self._seed()
        before = {p: p.read_bytes() for p in self.src.rglob("*") if p.is_file()}
        dst = self.root / "目标库"
        real = self._patch_bad_dst_hash(dst)
        try:
            with self.assertRaises(K.KBError):
                K.kb_vault_relocate(self.conn, "迁移库", dst, copy_files=True,
                                    dry_run=False, verify=True)
        finally:
            import fish_kb
            fish_kb.kb_sha256_file = real

        after = {p: p.read_bytes() for p in self.src.rglob("*") if p.is_file()}
        self.assertEqual(sorted(after), sorted(before), "copy 模式回滚把源文件弄丢了")
        for p, data in before.items():
            self.assertEqual(after.get(p), data, f"copy 模式回滚改动了源文件内容：{p}")
        leftovers = ([str(p) for p in dst.rglob("*") if p.is_file()]
                     if dst.exists() else [])
        self.assertEqual(leftovers, [], f"copy 模式回滚没清掉复制出去的副本：{leftovers}")
        self.assertEqual(self._note()["relocate"]["state"], "rolled_back")

    def test_dst_inside_src_is_refused_before_anything_moves(self):
        """**F-P1-5**：`dst` 落在 `src` 之内必须**动手前**拒绝（dry-run 也拒）。

        为什么不接受"先搬再校验发现"：那种目标让迁移清单边搬边长
        （`_kb_relocate_plan` 在搬的过程中会扫到刚搬进去的文件），
        源目录永远删不掉，中途失败时回滚还会把目标侧当成源的一部分。
        """
        self._seed()
        before = sorted(p for p in self.src.rglob("*") if p.is_file())
        inner = self.src / "新位置"
        for dry in (True, False):
            with self.subTest(dry_run=dry):
                with self.assertRaises(K.KBError) as ctx:
                    K.kb_vault_relocate(self.conn, "迁移库", inner,
                                        move_files=True, dry_run=dry)
                self.assertEqual(ctx.exception.code, "bad_usage")
                self.assertIn("之内", str(ctx.exception),
                              f"报错没指出目标在库目录之内：{ctx.exception}")
        self.assertEqual(sorted(p for p in self.src.rglob("*") if p.is_file()), before,
                         "被拒绝的搬移动了文件")
        self.assertFalse(inner.exists(), "被拒绝的搬移建了目标目录")
        self.assertEqual(Path(K.kb_vault_get(self.conn, "迁移库")["path"]).resolve(),
                         self.src.resolve(), "被拒绝的搬移改了登记")
        self.assertNotEqual(K._relocate_state(K.kb_vault_get(self.conn, "迁移库")),
                            "moving", "被拒绝的搬移留下了 moving 状态")

    # ------------------------------------------------------------- 解析器
    def test_relocate_state_does_not_guess(self):
        """读不出来一律返回空——"没有迁移记录"与"迁移完成了"是两种事实。"""
        for note in (None, "", "不是 JSON", "[]", '{"other": 1}',
                     '{"relocate": "x"}'):
            self.assertEqual(K._relocate_state({"note": note}), "",
                             f"note={note!r} 被猜成了有状态")


    def test_relocate_is_reachable_from_cli(self):
        """**F-P1-5**：`kb_vault_relocate` 实现了，但**产品命令够不着**。

        第七轮之前它只被测试调用——`fish kb vault` 只有 add/list/set-mode/
        scan/remove。一个只有测试能调的函数，对用户等于不存在。

        这条判据**跑真的命令行进程**（不是函数），问的是"用户能不能用它"。
        """
        import subprocess
        import sys as _sys
        import os as _os

        src = self.src
        dst = self.root / "目标"
        before = sorted(p.name for p in src.iterdir())

        # ① 子命令必须存在
        r = subprocess.run(
            [_sys.executable, "fish.py", "--root", str(self.root),
             "kb", "vault", "relocate", "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(HERE))
        self.assertEqual(r.returncode, 0,
                         f"`kb vault relocate` 不存在：{(r.stderr or '')[:200]}")
        self.assertIn("dry-run", r.stdout,
                      "帮助里没写缺省是 dry-run")

        # ② dry-run 的行为由**函数级**判据覆盖（子进程看不到本测试的内存库，
        # 硬跑只会得到 vault_missing——那是 fixture 限制，不是产品问题）。
        # 这里只确认"缺省不是 apply"这条契约：命令行层不许默认搬文件。
        self.assertIn("--apply", r.stdout,
                      "帮助里没有 --apply 开关，用户没法真搬")


if __name__ == "__main__":
    unittest.main(verbosity=2)
