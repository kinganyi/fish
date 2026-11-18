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
    def test_list_mode_filters_private(self):
        """**缺口已补**：`list_memories` 现在也过滤 private。

        这条原本叫 `test_list_mode_does_not_filter_yet`，是**刻意钉住缺口**的：
        补上过滤它会红，逼人回来改这里和 fish.py 的注释。它红了，所以我改了。

        现在它验三件事：
          · 别人列不到我的 private
          · 我自己列得到
          · 不给 viewer 时一律列不到（猜错方向是泄露）
        """
        self._enforce(True)
        code, _rel = self._write(title="私有标题丙", visibility="private",
                                 owner="C-OWNER")

        other = {r[0] for r in fish.list_memories(
            self.conn, project="P", limit=50, viewer_citizen_id="C-OTHER")}
        self.assertNotIn(code, other, "别人在列表模式里看到了我的 private")

        own = {r[0] for r in fish.list_memories(
            self.conn, project="P", limit=50, viewer_citizen_id="C-OWNER")}
        self.assertIn(code, own, "我自己也列不到自己的 private（过滤过头）")

        anon = {r[0] for r in fish.list_memories(self.conn, project="P", limit=50)}
        self.assertNotIn(code, anon, "没有 viewer 时 private 泄露了")

    def test_recent_fallback_filters_private(self):
        """未命中时的"最近条目"同样必须过滤——否则绕过检索。"""
        self._enforce(True)
        code, _rel = self._write(title="私有标题丁", visibility="private",
                                 owner="C-OWNER")
        rows = fish.recent_fallback(self.conn, project="P", limit=50,
                                    viewer_citizen_id="C-OTHER")
        self.assertNotIn(code, {r[0] for r in rows},
                         "回退列表里泄露了别人的 private")

    def test_filters_are_noop_when_not_enforced(self):
        """反向判据：开关关着时，这三处过滤**一个字节都不该改行为**。"""
        self._enforce(False)
        code, _rel = self._write(title="标签标题", visibility="private",
                                 owner="C-OWNER", allow_unenforced_private=True)
        other = {r[0] for r in fish.list_memories(
            self.conn, project="P", limit=50, viewer_citizen_id="C-OTHER")}
        self.assertIn(code, other,
                      "开关关着时 list 居然过滤了——那不是等价旧行为")



    def test_injection_pack_filters_private(self):
        """**最要紧的一处**：注入包的"最近三件事"不许印出别人的私有标题。

        为什么它比 search 泄露更严重：注入包**每个会话开场都跑**，
        不需要谁去搜、也不需要谁知道编号——名字会自己出现在所有人的上下文里。

        `neighbors` 那条是同一类问题的另一面（"存在"泄露），两条一起看才算补齐。
        """
        self._enforce(True)
        code, _rel = self._write(title="绝密项目代号", visibility="private",
                                 owner="C-OWNER")
        pack = fish.build_injection_pack(self.conn, self.root, citizen_id="C-OTHER",
                                         project="P")
        text = str(pack)
        self.assertNotIn("绝密项目代号", text,
                         "注入包里出现了别人的私有标题——这比 search 泄露严重，"
                         "因为它每个会话开场都跑")


    def test_count_matches_list_for_owner(self):
        """**计数必须和列表同口径**。

        第二轮评审指出：MCP 列出模式没传 viewer，而 `_visibility_sql` 对
        "没有 viewer"的处理是**一律剔除 private**。于是主人自己的私有笔记
        从列表里消失了，而 `count_memories` 不过滤、照数——回执写"共 2 条"
        却只列 1 条。

        评审的话很准：**这不是泄露，是列表在说谎。**
        """
        self._enforce(True)
        pub_code, _ = self._write(title="公开标题", visibility="shared")
        pri_code, _ = self._write(title="私有标题", visibility="private",
                                  owner="C-OWNER")
        rows = fish.list_memories(self.conn, project="P", limit=50,
                                  viewer_citizen_id="C-OWNER")
        total = fish.count_memories(self.conn, project="P",
                                    viewer_citizen_id="C-OWNER")
        codes = {r[0] for r in rows}
        self.assertIn(pub_code, codes)
        self.assertIn(pri_code, codes, "主人看不到自己的私有笔记")
        self.assertEqual(total, len(rows),
                         f"回执说共 {total} 条，实际列了 {len(rows)} 条——列表在说谎")

    def test_other_viewer_count_excludes_private(self):
        """反向：别人看到的条数也不该把私有算进去。"""
        self._enforce(True)
        self._write(title="公开标题乙", visibility="shared")
        self._write(title="私有标题乙", visibility="private", owner="C-OWNER")
        rows = fish.list_memories(self.conn, project="P", limit=50,
                                  viewer_citizen_id="C-OTHER")
        total = fish.count_memories(self.conn, project="P",
                                    viewer_citizen_id="C-OTHER")
        self.assertEqual(total, len(rows),
                         f"别人那边也出现了 {total} vs {len(rows)} 的不一致")


    def test_neighbors_filters_private(self):
        """**第三轮评审：这处修复原本零判据。**

        评审官把 `fish.py` 里 neighbors 的可见性过滤整段删掉，
        `test_visibility_guard` 12 passed、`test_visibility` 5 passed、
        `test_p1a` 32 passed——**全绿**。

        也就是说：上一轮我报告"给 neighbors 补了过滤并验过"，
        而实际上没有任何判据盯着它。**删掉它和留着它，测试读数一样。**

        这条补上那个洞：别人看不到私有条目的标题，自己看得到，
        开关关着时两边都能看到（等价旧行为）。
        """
        self._enforce(True)
        anchor, _ = self._write(title="锚点", visibility="shared")
        pri, _ = self._write(title="私密邻居标题", visibility="private",
                             owner="C-OWNER")

        other = fish.neighbors(self.conn, anchor, n=3, viewer_citizen_id="C-OTHER")
        titles_other = str(other)
        self.assertNotIn("私密邻居标题", titles_other,
                         "别人在 neighbors 里看到了私有条目的标题——"
                         "私有藏了正文却没藏住存在")

        own = fish.neighbors(self.conn, anchor, n=3, viewer_citizen_id="C-OWNER")
        self.assertIn("私密邻居标题", str(own),
                      "主人自己也看不到自己的私有邻居（过滤过头）")

    def test_neighbors_is_noop_when_not_enforced(self):
        """反向判据：开关关着时 neighbors 不该过滤（等价旧行为）。"""
        self._enforce(False)
        anchor, _ = self._write(title="锚点乙", visibility="shared")
        pri, _ = self._write(title="标签邻居", visibility="private",
                             owner="C-OWNER", allow_unenforced_private=True)
        other = fish.neighbors(self.conn, anchor, n=3, viewer_citizen_id="C-OTHER")
        self.assertIn("标签邻居", str(other),
                      "开关关着时 neighbors 居然过滤了——那不是等价旧行为")


if __name__ == "__main__":
    unittest.main(verbosity=2)
