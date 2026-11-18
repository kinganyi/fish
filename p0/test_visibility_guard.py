#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P1-2 判据：`visibility=private` 不许成为一个骗人的标签。

## 审查方指出的问题（已复核，但现状比原描述复杂）

审查方说"写进去之后没有任何读路径按它过滤，只是一个标签"。

核实后的**准确现状**（我第一版据此写的注释错了，已改正）：

  · `_filter_by_visibility` **确实接在** `search_memories` 上，实现正确；
  · 但它只在 `privacy.visibility_enforced` 打开时生效，而该键**默认关**；
  · `list_memories` / `neighbors` / `recent_fallback` / 注入各段**不过滤**。

所以缺的不是过滤器，是**"默认关着时写入方以为它生效了"这一层谎**。

## 修法为什么选"拒绝写入"

审查方给了两个选项："拒绝写入" 或 "补齐全部读路径过滤"。选前者的理由是**可验证性**：

补 17 处读路径，漏一处就是"以为私有了其实没有"，而这个错误**不会报错**——
它安静地泄露。拒绝写入只有一个判定点，它错不了。

这也**没有推翻**用户 2026-10-29 的"做成开关、不强制"决定：开关还在，
只是它现在必须真的有效才允许写 private。

## 这批判据为什么能失败

第一条是核心：默认配置下写 private 必须被拒。把那段校验删掉，它立刻红——
而它红的时候，用户不会在不知情中以为自己的记忆是私有的。
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402


class PrivateWriteGuardTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-p12-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq)"
            " VALUES ('P','P','2026-11-18',0)")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, **kw):
        kw.setdefault("project", "P")
        kw.setdefault("title", "标题")
        kw.setdefault("body", "正文\n")
        return fish.record_memory(self.conn, self.root, **kw)

    def _enforce(self, on):
        self.conn.execute(
            "INSERT OR REPLACE INTO config (key,value,type,updated_at) VALUES (?,?,?,?)",
            # 注意：`get_config` 对 bool 的解析是 `raw == "true"`，
            # 写 "1" 会被判成 False——这个坑实测踩过，别改回去。
            ("privacy.visibility_enforced", "true" if on else "false", "bool",
             fish.now_utc()))
        self.conn.commit()

    # ------------------------------------------------------------- 核心
    def test_private_write_is_refused_when_not_enforced(self):
        """**核心判据**：默认（开关关）写 private 必须被拒。

        删掉那段校验后，这条红——而它红的时候，用户不会在不知情中
        以为自己的记忆是私有的。
        """
        self._enforce(False)
        with self.assertRaises(ValueError) as ctx:
            self._write(visibility="private")
        msg = str(ctx.exception)
        self.assertIn("不会生效", msg, f"报错没说清后果：{msg[:160]}")
        self.assertIn("privacy.visibility_enforced", msg,
                      "报错要指出该改哪个配置键（否则用户不知道怎么修）")

    def test_shared_write_is_unaffected(self):
        """对照组：`shared` 不该因为这条校验被挡——否则"全部拒绝"也能让上面变绿。"""
        self._enforce(False)
        code, _rel = self._write(visibility="shared")
        self.assertTrue(code)

    def test_private_write_allowed_when_enforced(self):
        """开关打开时 private 可以写——那时过滤是真的。"""
        self._enforce(True)
        code, _rel = self._write(visibility="private")
        self.assertTrue(code)
        row = self.conn.execute(
            "SELECT visibility FROM code_index WHERE code=?", (code,)).fetchone()
        self.assertEqual(row[0], "private")

    def test_explicit_label_only_opt_in(self):
        """要"只要标签"必须显式说出口——默认不许悄悄退化成标签。"""
        self._enforce(False)
        code, _rel = self._write(visibility="private", allow_unenforced_private=True)
        self.assertTrue(code)

    # ------------------------------------------------------------- 锁住既有过滤
    def test_search_filters_private_when_enforced(self):
        """开关打开时，别人的 private **搜不到**；自己的能搜到。

        这条锁的是 `_filter_by_visibility` 已接在 `search_memories` 上的既有行为——
        我第一版注释误说它"零调用点"，这条判据就是为了不再靠人记。
        """
        self._enforce(True)
        code, _rel = self._write(title="私有标题甲", visibility="private",
                                 owner="C-OWNER")
        hit_other = fish.search_memories(self.conn, query="私有标题甲",
                                         viewer_citizen_id="C-OTHER")
        rows_other = hit_other[0] if isinstance(hit_other, tuple) else hit_other
        codes_other = {r[0] for r in rows_other}
        self.assertNotIn(code, codes_other, "别人的 private 被搜到了")

        hit_own = fish.search_memories(self.conn, query="私有标题甲",
                                       viewer_citizen_id="C-OWNER")
        rows_own = hit_own[0] if isinstance(hit_own, tuple) else hit_own
        codes_own = {r[0] for r in rows_own}
        self.assertIn(code, codes_own, "自己的 private 也搜不到了（过滤过头）")

    def test_search_without_viewer_hides_all_private(self):
        """没给 viewer 时**一律剔除 private**——猜错方向是泄露，代价不对称。"""
        self._enforce(True)
        code, _rel = self._write(title="私有标题乙", visibility="private",
                                 owner="C-OWNER")
        res = fish.search_memories(self.conn, query="私有标题乙")
        rows = res[0] if isinstance(res, tuple) else res
        self.assertNotIn(code, {r[0] for r in rows},
                         "没有 viewer 时 private 泄露了")

    # ------------------------------------------------------------- 已知缺口
    def test_list_mode_does_not_filter_yet(self):
        """**如实记录已知缺口**：`list_memories` 目前不过滤 private。

        这条判据不是在为缺口辩护——它是在**固定现状**：谁哪天补上了过滤，
        这条会红，于是必须回来把注释和文档一起改掉。缺口被写进判据，
        比缺口只写在文档里更难被忘掉。
        """
        self._enforce(True)
        code, _rel = self._write(title="私有标题丙", visibility="private",
                                 owner="C-OWNER")
        rows = fish.list_memories(self.conn, project="P", limit=50)
        codes = {r[0] for r in rows}
        self.assertIn(code, codes,
                      "list_memories 居然开始过滤了——说明缺口被补上了，"
                      "请把 fish.py 里 F-P1-2 的注释与本判据一起更新")


if __name__ == "__main__":
    unittest.main(verbosity=2)
