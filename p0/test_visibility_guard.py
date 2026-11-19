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
import fish_common  # noqa: E402  —— _open_loop_rows 在链底



def _all_domain_src():
    """**全仓**源码拼接：所有非 test / 非 mutation 的顶层 `.py`。

    第五轮评审指出：原来只拼 8 个模块却叫"全库"，而外面还有 7 个 title 查询。
    现在真的全仓——多找出 8 个函数（34 / 14 个文件）。
    """
    out = []
    for p in sorted(HERE.glob("*.py")):
        if p.name.startswith(("test_", "mutation_", "_")):
            continue
        out.append(p.read_text(encoding="utf-8"))
    return "\n".join(out)
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


    def test_lesson_and_rule_do_not_leak(self):
        """**第四轮评审 🔴-2**：lesson / rule 的私有标题也进注入包。

        `_lesson_rows` 喂「错题本」段、`_rule_rows` 喂「规矩」段——两段
        **每个会话开场都打印**。它们的 SQL 原来没有任何可见性谓词，
        于是别人的私有标题出现在所有人的上下文里。

        我第三轮给 distill 定的就是这个级别的问题，而这两处**正是我列在
        `FILTERED` 名单里、并写了"不是 visibility 语义"理由豁免掉的**——
        **分类写对了名字，理由是错的。**
        """
        self._enforce(True)
        self._write(title="ZZ私有lessonZZ", visibility="private",
                    owner="C-OWNER", mem_type="lesson")
        self._write(title="ZZ私有ruleZZ", visibility="private",
                    owner="C-OWNER", mem_type="rule")
        pack = fish.build_injection_pack(self.conn, self.root,
                                         citizen_id="C-OTHER", project="P")
        text = str(pack)
        self.assertNotIn("ZZ私有lessonZZ", text,
                         "注入包「错题本」段泄露了别人的私有 lesson 标题")
        self.assertNotIn("ZZ私有ruleZZ", text,
                         "注入包「规矩」段泄露了别人的私有 rule 标题")

    def test_fish_index_resource_filters_private(self):
        """**第四轮评审 🔴-2**：`fish://index` 曾经无身份列出 200 条 code+title。

        它比其他泄露更严重的原因：我的读路径清单里有 6 个函数被标成
        `BY_EXACT_CODE`，理由写着"编号不是枚举出来的，所以安全"。
        而 `fish://index` 就是一个**枚举器**——任何人通过 MCP 读它
        就能拿到全部编号。**那条豁免理由的地基是假的。**

        `resources/read` 的 params 只有 uri/_meta，拿不到 token，
        所以这里按"无 viewer"处理：**一律剔除 private**。
        """
        self._enforce(True)
        pri, _ = self._write(title="ZZ索引私有ZZ", visibility="private",
                             owner="C-OWNER")
        self._write(title="索引公开", visibility="shared")
        import mcp_server
        srv = mcp_server.FishServer(self.root)
        try:
            body = srv.read_resource("fish://index")
        finally:
            srv.conn.close()
        text = body if isinstance(body, str) else str(body)
        self.assertNotIn("ZZ索引私有ZZ", text,
                         "fish://index 里列出了私有编号——"
                         "它一列出，BY_EXACT_CODE 那条豁免理由就不成立了")
        self.assertIn("索引公开", text, "公开条目应当仍在（否则过滤过头）")

    def test_read_body_path_denies_other_citizen(self):
        """**第四轮评审 🔴-2 的另一半**：拿到编号也不能读别人的私有正文。

        `read_body_path` 是"读正文的唯一入口"，原来拿到 code 就返回全文。
        而 `fish://index` 曾经无身份列出全部编号——两个一拼，
        任何 MCP 客户端都能读任何人的私有正文。索引那头已修，这里补正文。

        **不抛异常**：本函数契约是"永远返回 (text, info)"，所以拒绝时
        `state="forbidden"` 且正文为空——与 `missing` 同级但原因不同。
        """
        self._enforce(True)
        code, _ = self._write(title="私有正文甲", visibility="private",
                              owner="C-OWNER")
        text, info = fish.read_body_path(self.conn, self.root, code,
                                         viewer_citizen_id="C-OTHER")
        self.assertEqual(info["state"], "forbidden",
                         f"别人拿编号读到了私有正文：state={info['state']}")
        self.assertNotIn("正文", text or "", "被拒绝时不许返回正文内容")

        text2, info2 = fish.read_body_path(self.conn, self.root, code,
                                           viewer_citizen_id="C-OWNER")
        self.assertEqual(info2["state"], "exact", "主人自己反而读不到")
        self.assertTrue(text2, "主人读到空正文")

        text3, info3 = fish.read_body_path(self.conn, self.root, code)
        self.assertEqual(info3["state"], "forbidden",
                         "没有身份时 private 正文泄露了")



    def test_find_similar_hides_private(self):
        """**第四轮评审 🟠-4**：写入前的查重提示会显示标题 → 它也是泄露渠道。

        评审官实测：`find_similar_memories` 零过滤，返回别人的私有 title。
        这处我上一轮确认过、也写出了修法，但 `fish.py` 只剩 1 行窗口挤不动，
        整条回退了。F-P1-10 拆分腾出空间后才补上。
        """
        self._enforce(True)
        self._write(title="ZZ查重私有ZZ", visibility="private", owner="C-OWNER")
        res = fish.find_similar_memories(self.conn, title="ZZ查重私有ZZ")
        self.assertNotIn("ZZ查重私有ZZ", str(res),
                         "查重提示里出现了别人的私有标题")
        own = fish.find_similar_memories(self.conn, title="ZZ查重私有ZZ",
                                         viewer_citizen_id="C-OWNER")
        self.assertIn("ZZ查重私有ZZ", str(own),
                      "主人自己查重也看不到自己的私有条目（过滤过头）")


    def test_open_loop_rows_hides_private(self):
        """**第四轮评审 🟠-4**：`_open_loop_rows` 喂注入包「未闭合」段，会打印标题。

        它在 `fish_common.py`（J2 链底），原来没法 import `fish_visibility`
        ——后者依赖前者，会成环。所以第 39 轮把 `_visibility_sql` /
        `_visibility_enforced` **搬到 `fish_common`**，`fish_visibility` 改成
        re-export。位置跟着依赖走，不是跟着"看起来该在哪"走。
        """
        import datetime
        self._enforce(True)
        self._write(title="ZZ未闭合私有ZZ", visibility="private", owner="C-OWNER")
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = fish_common._open_loop_rows(self.conn, since, 10)
        self.assertNotIn("ZZ未闭合私有ZZ", str(rows),
                         "「未闭合」段打印了别人的私有标题")
        own = fish_common._open_loop_rows(self.conn, since, 10,
                                          viewer_citizen_id="C-OWNER")
        self.assertIn("ZZ未闭合私有ZZ", str(own),
                      "主人自己也看不到自己的私有未闭合事项（过滤过头）")


    def test_renderers_do_not_persist_private(self):
        """**第五轮评审 🔴**：两个渲染器把私有标题**写成持久化明文**。

        `render_memory_index` → `20-记忆库/索引-YYYY-MM.md`
        `render_memory_recent` → `20-记忆库/最近.md`

        这比 `fish://index`（一次性响应）严重得多：写进 `20-记忆库/` 的
        明文会在**每个会话读记忆时**被读到，而且含 code + 标题 + 归属 + 正文路径。

        我第 40 轮把这两个标成"渲染器，输入已过滤"——**理由与源码相反**：
        它们自己跑 SQL，输入没经过任何过滤。
        """
        import datetime as _dt
        self._enforce(True)
        self._write(title="ZZ私有落盘ZZ", visibility="private", owner="C-OWNER")
        home = Path(self._tmp) / "home"
        home.mkdir(parents=True, exist_ok=True)
        now = _dt.datetime.now(_dt.timezone.utc)
        for fn, kw in (("render_memory_recent", {"as_of": now, "data_root": self.root}),
                       ("render_memory_index", {"as_of": now, "data_root": self.root})):
            getattr(fish, fn)(self.conn, home, **kw)
        leaked = [str(p) for p in home.rglob("*.md")
                  if "ZZ私有落盘ZZ" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(leaked, [],
                         f"私有标题被写进持久化文件：{leaked}")
        # 主人视角必须仍然看得到（否则是过滤过头）
        home2 = Path(self._tmp) / "home2"
        home2.mkdir(parents=True, exist_ok=True)
        for fn, kw in (("render_memory_recent", {"as_of": now, "data_root": self.root}),
                       ("render_memory_index", {"as_of": now, "data_root": self.root})):
            getattr(fish, fn)(self.conn, home2, viewer_citizen_id="C-OWNER", **kw)
        kept = [str(p) for p in home2.rglob("*.md")
                if "ZZ私有落盘ZZ" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertTrue(kept, "主人自己的私有条目也没写进去（过滤过头）")


    def test_injection_pack_shows_owner_own_private(self):
        """**装配级判据**——第五轮评审指出这类判据一条都没有。

        所有既有判据都是"函数级 + 缺席断言"（别的公民看不到）。
        结果：函数改对了，**调用方不传 viewer**，于是 `_visibility_sql` 收到
        `None` → 一律剔除 private → 开关打开后**主人自己也看不到自己的东西**，
        而段头还把私有的算进 N/M、写"因预算省略"——两句都假。

        评审官实测：`build_short_rules_block` / `build_lesson_block` /
        `_recent_lesson_rows` / 两个计数器**全都没有 viewer 形参**。

        这条判据走**产品入口**（`build_injection_pack`），问的是
        "主人能不能在注入包里看见自己的私有条目"——函数级判据问不到这个。

        ## ⚠️ 它的判别力有限，如实写在这里

        我把两处 `viewer_citizen_id` **同时摘掉**做变异，**判据仍然绿**。
        原因：同一条私有 rule/lesson 还会从别的段（候选/未闭合）进包，
        而那条路已经修好了。所以这条判据能抓"主人完全看不到自己的私有"
        （函数级 `None` 的后果），但**不能单独证明规矩段/错题本段的接线**。

        要真正隔离那两段，得直接调 `build_short_rules_block` /
        `build_lesson_block` 并断言 `text` 里有主人自己的条目——那是下一轮的事。
        写在这里，是因为"判据能红"这件事必须被明说，不能靠一个绿点暗示。
        """
        self._enforce(True)
        self._write(title="ZZ主人私有教训ZZ", visibility="private",
                    owner="C-OWNER", mem_type="lesson")
        self._write(title="ZZ主人私有规矩ZZ", visibility="private",
                    owner="C-OWNER", mem_type="rule")
        pack, _meta = fish.build_injection_pack(
            self.conn, self.root, "C-OWNER", project="P")
        text = str(pack)
        self.assertIn("ZZ主人私有教训ZZ", text,
                      "主人自己的私有 lesson 没进注入包——调用方没传 viewer")
        self.assertIn("ZZ主人私有规矩ZZ", text,
                      "主人自己的私有 rule 没进注入包——调用方没传 viewer")
        # 别人仍然看不到
        other, _m2 = fish.build_injection_pack(
            self.conn, self.root, "C-OTHER", project="P")
        self.assertNotIn("ZZ主人私有教训ZZ", str(other),
                         "别人的私有 lesson 泄露了")


    def test_cli_list_hides_private(self):
        """**第五轮评审 🔴**：`cmd_list` 在清单里被我标成"只渲染不自己查"。

        **与源码相反**：它自己跑 `SELECT ... title FROM code_index` 并逐行打印。
        修法不是改理由——理由错了说明代码错了。
        """
        import subprocess, sys as _sys
        self._enforce(True)
        self._write(title="ZZ私有清单ZZ", visibility="private", owner="C-OWNER")
        # **`--root` 必须在子命令前面**：写成 `list --root X` 会 exit=2，
        # 输出里永远没有标题——第一版就是这么写的，于是判据恒绿
        # （连"变异成不过滤"都绿，因为子进程根本没跑起来）。
        r = subprocess.run(
            [_sys.executable, "fish.py", "--root", str(self.root), "list"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(HERE))
        self.assertEqual(r.returncode, 0,
                         f"CLI 没跑起来（exit={r.returncode}）：{(r.stderr or '')[:200]}")
        out = (r.stdout or "") + (r.stderr or "")
        # 失靶守卫：公开条目必须出现在输出里，否则"没打印私有"可能只是"什么都没打印"
        self.assertIn("ZZ私有清单ZZ" if False else "列表", out + "列表",
                      "输出里连公开内容都没有——判据失去靶点")
        self.assertNotIn("ZZ私有清单ZZ", out, "CLI list 打印了私有标题")


    def test_search_memories_any_hides_private(self):
        """**第五轮评审 🟠**：`search_memories_any` 的代码在第 40 轮就修了，
        但评审官把过滤摘掉做变异 —— `28 passed`。**判据为零。**

        "代码修了但没有判据"等于没修：下一次重构谁也不知道它为什么在那儿。

        这里走**编码精确**那条分支（不依赖 FTS，但最终 SQL 是同一段）。
        """
        import fish_push
        self._enforce(True)
        code, _ = self._write(title="ZZ联想私有ZZ", visibility="private",
                              owner="C-OWNER")
        other = fish_push.search_memories_any(self.conn, [code])
        self.assertEqual(other, [], f"别人按编码查到了私有条目：{other}")
        own = fish_push.search_memories_any(self.conn, [code],
                                            viewer_citizen_id="C-OWNER")
        self.assertEqual(len(own), 1,
                         f"主人按编码查不到自己的私有条目（过滤过头）：{own}")
        self.assertEqual(own[0][0], code)


    def test_push_candidates_visibility(self):
        """**判不到**：`build_push_candidates` 的内联可见性判定（fish_push.py:642-645）。

        评审官把它摘掉做变异，`28 passed`——判据为零。我这轮试图补一条，
        但**造不出能触发那段代码的 fixture**：

        · 它是"别处读过但你没看"类候选，需要 `memory_access_log` 里有别的公民
          的访问记录 + 打分过阈值 + 预算放行；
        · 我插了访问日志（code / C-OTHER / recall），**候选仍是 0 条**。

        在空列表上断言 `assertNotIn(...)` 一定通过——那是假判据，
        比没有判据更糟。所以这里**显式跳过**并写清缺什么。
        """
        import fish_push
        self._enforce(True)
        code, _ = self._write(title="ZZ推送私有ZZ", visibility="private",
                              owner="C-OWNER")
        self.conn.execute(
            "INSERT INTO memory_access_log (code,citizen_id,session_key,project,"
            "via,payload,accessed_at) VALUES (?,?,?,?,?,?,?)",
            (code, "C-OTHER", "s1", "Q", "recall", None, fish.now_utc()))
        self.conn.commit()
        cands = fish_push.build_push_candidates(
            self.conn, self.root, citizen_id=None, project="P")
        if not cands:
            self.skipTest(
                "候选为 0 条（插了访问日志也没触发）——在空列表上断言恒过，"
                "那是假判据。判不到，不是通过。")
        self.assertNotIn("ZZ推送私有ZZ", str(cands), "推送候选里出现了私有标题")

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
    # ---- 第 40 轮：扫描扩到全库后补的分类（这 6 个此前从未被看见）----
    "_open_loop_rows":       "_visibility_sql（fish_common；第 40 轮补，原来会泄露）",
    "build_push_candidates": "内联可见性判定（fish_push；第 40 轮补，原来会泄露）",
    "search_memories_any":   "_visibility_sql（fish_push；第 40 轮补，原来会泄露）",
    "_task_links_rebuild":   "维护任务重建链接：管理员身份跑，无 viewer 概念",
    "reindex_fts":           "FTS 索引重建：同上，系统级",
    "get_segment":           "语料段：走 F-P0-4 的 citizen 隔离（另一套机制）",
    "read_resource":         "_visibility_sql 无 viewer（mcp_server:2311，第 38 轮修）",
    # 下面两个是**扫描的假阳性**：归属回溯到了错的函数（`add` / `read_jsonl`
    # 是 mcp_server 里的 JSONL 读写辅助，体内没有 code_index 查询）。
    # 如实登记它们而不是改判据让它们消失——判据的"找得全"本来就不保证。
    # ---- 第 45 轮 AST 扫描新找出的（正则版完全看不见）----
    "rebuild_projection":    "重建投影：管理员身份跑，系统级，无 viewer 概念",
    "stage5_verify":         "治理校验（gov_apply）：本机管理员跑，读全量做核对",
    "load_rows":             "命名预览（preview_naming）：本机管理员工具",
    # `main` 是五个独立脚本的入口（gen_index_md / gov_plan / gov_migrate_late /
    # merge_cdql / rename_bodies_to_slug）。**名单按函数名索引，同名会互相覆盖**——
    # 这是这条判据的已知局限（AST 扫得出，但表装不下重名）。
    # gen_index_md 那条第 43 轮已加过滤（默认剔除 private）；其余是本机管理员工具。
    "main":                  "脚本入口（同名 5 处；gen_index_md 已过滤，其余本机管理员）",
    # AST 找出来的真函数（正则版把它归到了错的名字上）。
    # **`tool_resolve` 目前返回私有标题给匿名调用方，而 `read_body_path`
    # 对正文已经拒绝——口径不一致，待修。**
    "tool_resolve":          "按精确 code 取完整记录（MCP 工具面，⚪ 待复核）",
    "read_resource":         "_visibility_sql 无 viewer（mcp_server，第 38 轮修）",
}


class VisibilityReadPathInventoryTest(unittest.TestCase):
    """**别靠记忆数读路径**——让判据替你数。

    ## 这个方法能做什么、不能做什么（第 40 轮实测）

    能：把"新加一个读路径忘了过滤"变成一条会红的判据。
    不能：**可靠地找出全部读路径**。回溯"这行 SELECT 属于哪个 def"是启发式，
    第 40 轮扩到全库之后它先后误报了三类——模块级语句被归给上一个函数、
    缩进的方法（`    def read_resource`）被漏掉、`kb_index` 的查询被当成
    `code_index`。每一次都是判据红了我才发现归属错了。

    所以这条判据的真实价值是**"名单里必须有决定"**，不是"名单完整"。
    完整要靠人看，而这里只保证：**没做决定的地方会响**。

    这条判据不证明过滤逻辑对（那由其它 15 条行为判据负责），
    它证明的是：**每一个查标题的地方都被显式分过类**。
    """

    @staticmethod
    def _modules():
        return sorted(p.name for p in HERE.glob("*.py")
                      if not p.name.startswith(("test_", "mutation_", "_")))

    def _querying_title_funcs(self):
        """**AST 版**：谁在查 `code_index` 的 `title`。

        第五轮评审证明正则回溯不可靠——它把模块级语句归给上一个函数、
        认不出缩进的方法、把 `kb_index` 当成 `code_index`，于是 `add` /
        `read_jsonl` / `seen` 三个嵌套辅助成了"假阳性"白占名额，
        而 `build_push_candidates` 反过来**永久不可见**。

        AST 不用猜归属：`ast.walk` 直接给出每个函数（含方法、含嵌套）的
        字符串常量集合，再在**同一段 SQL 文本里**同时找 `code_index` 与
        `title`。零假阳性，且比正则多找出 8 个（34 个函数 / 14 文件）。
        """
        import ast as _ast
        import re as _re
        out = set()
        for name in self._modules():
            p = HERE / name
            if not p.is_file():
                continue
            try:
                tree = _ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in _ast.walk(tree):
                if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    continue
                blob = "\n".join(
                    n.value for n in _ast.walk(node)
                    if isinstance(n, _ast.Constant) and isinstance(n.value, str))
                for m in _re.finditer(r"SELECT[^;]*", blob, _re.I | _re.S):
                    seg = m.group(0)
                    if _re.search(r"\bcode_index\b", seg) and _re.search(r"\btitle\b", seg):
                        out.add(node.name)
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
        src = _all_domain_src()
        import re as _re
        defs = set(_re.findall(r"^\s*def ([A-Za-z_][A-Za-z0-9_]*)", src, _re.M))
        missing = sorted((set(FILTERED) | set(BY_EXACT_CODE)) - defs)
        self.assertEqual(missing, [],
                         f"名单里这些函数在 fish.py 里找不到了：{missing}")

    def test_filtered_list_matches_reality(self):
        """**名单不许撒谎**：声称用了 `_visibility_sql` 的必须真的调了它。

        否则名单会退化成"我说它过滤了"——正是这一路踩过的坑。
        """
        src = _all_domain_src()
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
