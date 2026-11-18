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


    def test_distill_filters_private(self):
        """**最后一处泄露**：`distill_clusters` 自带 SQL，返回 episode 的 `title`。

        第三轮评审实测：`enforced=1` 时 list/search 对 C-OTHER 返回 0，
        而 distill 打印出 2 条 private 标题。

        为什么它最容易被漏：它不经过 `_list_where`、也不在"补读路径"那轮的清单里——
        **自带 SQL 的读路径需要被单独数一遍**，而我是按"我记得的几条"去补的。
        """
        self._enforce(True)
        for i in range(3):
            self._write(title=f"公开素材{i}", visibility="shared",
                        mem_type="episode", tags=["主题甲"])
        self._write(title="绝密素材", visibility="private", owner="C-OWNER",
                    mem_type="episode", tags=["主题甲"])

        res = fish.distill_clusters(self.conn, project="P", min_group=1,
                                    viewer_citizen_id="C-OTHER")
        self.assertNotIn("绝密素材", str(res),
                         "distill 里印出了别人的私有 episode 标题")

        own = fish.distill_clusters(self.conn, project="P", min_group=1,
                                    viewer_citizen_id="C-OWNER")
        self.assertIn("绝密素材", str(own),
                      "主人自己的私有素材也聚不到了（过滤过头）")


# ---------------------------------------------------------------- 读路径清单
#
# **F-P1-X（第三轮评审后的自查）**：我补私有过滤时，是按"我记得的那几条"补的。
# 结果漏了 `neighbors`（零判据，删掉全绿）和 `distill`（自带 SQL，最后一处泄露）。
#
# 数一遍才发现：**19 个函数查 `code_index` 的 `title`，只有 6 个调用过滤**。
#
# 所以这里不写"应该过滤几处"，而是**把每一个都点名**，分成两类：
#   · FILTERED：调 `_visibility_sql` / `_filter_by_visibility`（或从已过滤结果渲染）；
#   · BY_EXACT_CODE：按精确 code 取单条，调用方已经拿到了那条的编号——
#     "知道编号就能读"是设计如此（编号不是枚举出来的）。
#
# 判据要求：源码里每个查 title 的函数都必须在两张名单之一。
# **新加一个读路径忘了过滤 → 它自己会红**，不用等评审。
FILTERED = {
    "search_memories":        "调 _filter_by_visibility（后置过滤，含 FTS 结果）",
    "list_memories":          "_visibility_sql 插进 WHERE",
    "count_memories":         "_visibility_sql（与 list 同口径）",
    "recent_fallback":        "_visibility_sql",
    "neighbors":              "_visibility_sql",
    "distill_clusters":       "_visibility_sql（自带 SQL，第三轮评审补的）",
    "build_injection_pack":   "_visibility_sql（最近三件事）",
    "cmd_list":               "只是渲染 list_memories 的结果，不自己查",
    "cmd_report":             "只渲染聚合计数，不返回标题",
    "render_memory_index":    "渲染器，输入已过滤",
    "render_memory_recent":   "渲染器，输入已过滤",
    "_lesson_rows":           "lesson 免凭证可写但不进 private 语义（见 F-P0-3 的信任门）",
    "_rule_rows":             "rule 有授权门（见 fish_trust），不是 visibility 语义",
    "find_similar_memories":  "按精确 code 排除自身后取相似；结果仍走 search 口径",
}
BY_EXACT_CODE = {
    "_titles_of":        "按精确 code 批量取标题——编号不是枚举出来的",
    "archive_body":      "按精确 code 取正文",
    "read_body_path":    "按精确 code 取正文路径",
    "supersede_memory":  "按精确 code 改状态",
    "withdraw_memory":   "按精确 code 改状态",
    "search_by_relevance": "push 用：按精确 code 集合取相关性",
}


class VisibilityReadPathInventoryTest(unittest.TestCase):
    """**别靠记忆数读路径**——让判据替你数。

    这条判据不证明过滤逻辑对（那由其它 15 条行为判据负责），
    它证明的是：**每一个查标题的地方都被显式分过类**。
    """

    def _querying_title_funcs(self):
        import re as _re
        src = (HERE / "fish.py").read_text(encoding="utf-8")
        lines = src.split("\n")
        out = set()
        for i, ln in enumerate(lines):
            if not _re.search(r"SELECT .*\btitle\b", ln):
                continue
            for k in range(i, -1, -1):
                m = _re.match(r"^def ([A-Za-z_][A-Za-z0-9_]*)", lines[k])
                if m:
                    out.add(m.group(1))
                    break
        return out

    def test_every_title_query_func_is_classified(self):
        found = self._querying_title_funcs()
        # 失靶守卫：扫不到就说明正则坏了，必须红而不是通过
        self.assertGreaterEqual(
            len(found), 10,
            f"只扫到 {sorted(found)} 这些函数——判据大概率失去靶点，不是通过")
        declared = set(FILTERED) | set(BY_EXACT_CODE)
        undeclared = sorted(found - declared)
        self.assertEqual(
            undeclared, [],
            f"这些函数查 `code_index.title` 却没被分类：{undeclared}\n"
            "  新加读路径时请决定它归哪一类并写进 test_visibility_guard.py：\n"
            "    · 返回用户可见标题 → 必须过滤，加进 FILTERED 并接 _visibility_sql\n"
            "    · 按精确 code 取单条 → 加进 BY_EXACT_CODE 并写明为什么安全")

    def test_declared_funcs_still_exist(self):
        """反向判据：名单里点名的函数必须还在（改名/删除要同步）。"""
        src = (HERE / "fish.py").read_text(encoding="utf-8")
        import re as _re
        defs = set(_re.findall(r"^def ([A-Za-z_][A-Za-z0-9_]*)", src, _re.M))
        missing = sorted((set(FILTERED) | set(BY_EXACT_CODE)) - defs)
        self.assertEqual(missing, [],
                         f"名单里这些函数在 fish.py 里找不到了：{missing}")

    def test_filtered_list_matches_reality(self):
        """**名单不许撒谎**：声称用了 `_visibility_sql` 的必须真的调了它。

        否则名单会退化成"我说它过滤了"——正是这一路踩过的坑。
        """
        src = (HERE / "fish.py").read_text(encoding="utf-8")
        lines = src.split("\n")
        callers = set()
        for i, ln in enumerate(lines):
            if "_visibility_sql(" not in ln or ln.lstrip().startswith("def "):
                continue
            for k in range(i, -1, -1):
                import re as _re
                m = _re.match(r"^def ([A-Za-z_][A-Za-z0-9_]*)", lines[k])
                if m:
                    callers.add(m.group(1))
                    break
        declared_sql = {k for k, v in FILTERED.items() if "_visibility_sql" in v}
        liars = sorted(declared_sql - callers)
        self.assertEqual(liars, [],
                         f"名单说这些函数用 _visibility_sql，但源码里没调用：{liars}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
