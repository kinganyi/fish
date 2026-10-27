#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反馈修复 B 批（2026-10-26）：FB-01 / FB-02 / FB-16 的判据。

三条都是"**回包没把话说全**"这一族，判据因此都读**印出来的文本**（不是读实现变量）：
读者能看见什么，才算修好了。

  · **FB-01 列出模式**：`project` / `tags` 这两个过滤器一直在，但"不给关键词时
    只想看某个项目里带某标签的条目"这件事做不到——没 query 时走"未找到 + 最近条目"，
    tags 单独用直接报错。三种调用形态各有各的判据：
      ① 只有 tags → **引导**（不抛错，且给出下一步怎么写）
      ② project[+tags] 无 query → **列出** + 首行声明"按什么过滤、共几条"
      ③ 有 query → 仍是**检索**（回包不带列出声明），且结果与 `search_memories` 一致
    CLI（`fish recall --project X --tags t`）与 MCP 同源，一起钉住。

  · **FB-02 注入包自解释**：规矩固定 3 条且不说、看板可无上限吃预算、无法选段、
    回包不声明截断。四条判据对应四件事（见下）。

  · **FB-16 as-of**：注入包没有"数据截至何时"，读者无法判断它是否过期。

## 判据与变异（变异自证见交付报告，每次红→还原）

A1 只有 tags → 引导文案（不抛错）
   | 变异：把"只有 tags"重新改成抛错 → 红
A2 project+tags 无 query → 只出该项目且带 tag 的条目 + 声明行
   | 变异：`_list_where` 去掉 tags 过滤 → 别的不带 tag 的条目混进来 → 红
A3 有 query → 检索（无列出声明），结果与 `fish.search_memories` 一致
   | 变异：query 也走列出模式 → 声明行出现 / 结果变成"按项目列举" → 红
A4 CLI `recall --project --tags`（无关键词）与 MCP 同口径（同一份 list_memories）
   | 变异：CLI 改回"必须给关键词" → 红

B1 规矩段包含 `显示 N/M 条`，且 N<M 时声明"其余因预算/上限省略"
   | 变异：不写段头 → 红
B2 `sections=["status"]` → 包内**无**看板/规矩段，但 L0 仍在
   | 变异：忽略 `sections`（当全段处理）→ 看板/规矩回来 → 红
B3 500 条动态的看板 → 段长 ≤ 预算×0.25（+段头标记的余量），且段头有"已按预算截断"
   | 变异：不传 max_tokens（看板无上限）→ 段长超出 → 红
B4 `meta["truncated"]` 与包尾声明、与包里实际印出的条数**三者一致**；
   无省略时写"本次无截断"
   | 变异：meta 写死 0 / 声明与 meta 分开算 → 红

C1 包内出现 `as-of <ISO8601Z>` 且能被 `datetime.strptime` 解析；`meta["as_of"]` 同值；
   MCP 回包里它在 `<!-- tokens/budget -->` 之前（包正文的结尾）
   | 变异：删掉 as-of 行 → 红

## 最小运行（**不要**跑全量：603 条归 QA）

    & "C:\\Users\\15596\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" `
      -m unittest test_feedback_261026_b -v

必须在 `fish-fix/p0` 目录下跑（`fish.py` 按相对路径加载，与其他测试一致）。
"""
import argparse
import contextlib
import datetime
import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server        # noqa: E402

# 编码形如 `PROJA-261026-01`。判据读的是**印出来的文本**，所以"抽回编码"
# 这个动作要自己从文本里做——不拿实现里的列表自证（那是同一处自证同一处）。
CODE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*-\d{6}-\d+\b")
SHOW_RE = re.compile(r"显示 (\d+)/(\d+) 条")
AS_OF_RE = re.compile(r"as-of (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")

# 包内"下一段开始了"的信号（用于把看板段从整包里切出来）。
# 刻意用**段头文本**而不是固定行数：段长本身就是要测的东西，不能拿它当坐标。
_NEXT_SECTION_STARTS = ("规矩段：", "本项目规矩（", "全局规矩：", "该注意什么：",
                        "错题本：", "可能对你有用", "可查项目：", "衰减(天)：",
                        "【操作指引】", "本次截断", "本次无截断", "as-of ")


class _Case(unittest.TestCase):
    """共用夹具：一个 tempfile 根 + 一台 FishServer（测试产物只在临时目录里）。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "data"
        self.srv = mcp_server.FishServer(self.root)
        self.conn = self.srv.conn
        # 身份用"未归属"这一档：本批三条判据都与身份无关，且这样 L0 也会出现
        # （"未带凭证"本身是 L0 的一种合法形态，判据要的只是"L0 在"）。
        self.ident = self.srv._resolve_identity(None)

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    # ------------------------------------------------------------ 造数据的小工具
    def _project(self, prefix="PROJA"):
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
            "status) VALUES (?,?,NULL,0,'active')", (prefix, prefix))
        self.conn.commit()
        return prefix

    def _citizen(self, name="测试居民"):
        cid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, name, "resident", None, None, "active", fish.now_utc()))
        self.conn.commit()
        return cid

    def _mem(self, code, title, *, project="PROJA", mem_type="fact", tags=(),
             status="active", recorded_at=None, dormant=0, owner=None):
        ts = recorded_at or fish.now_utc()
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared',?,?,NULL,?,?,0,?)",
            (code, title, project, owner or "01OWNER000000000000000001", mem_type,
             status, json.dumps(list(tags), ensure_ascii=False), ts, ts, dormant))
        self.conn.commit()
        return code

    def _rules(self, n, *, project="PROJA"):
        """造 n 条本项目的规矩（编码按序号递增 → recorded_at 同刻时仍有序）。"""
        ts = fish.now_utc()
        codes = []
        for i in range(1, n + 1):
            codes.append(self._mem(f"{project}-261026-{i:02d}", f"本项目规矩{i}",
                                   project=project, mem_type="rule", recorded_at=ts))
        return codes

    def _recall(self, **args):
        return self.srv.call_tool("recall", args, self.ident)

    def _pack(self, **kw):
        kw.setdefault("project", "PROJA")
        return fish.build_injection_pack(self.conn, self.root, self.ident.citizen_id, **kw)

    # ------------------------------------------------------------ 从包里切段
    def _board_section(self, pack):
        """把看板段从整包里切出来（段头 → 下一段开始之前）。"""
        out, cur = [], False
        for raw in pack.splitlines():
            line = raw.strip()
            if not cur and line.startswith("看板（"):
                cur = True
            if cur:
                if line.startswith(_NEXT_SECTION_STARTS):
                    break
                out.append(line)
        return "\n".join(out)

    def _rules_section(self, pack):
        """规矩段（含段头声明 + 两栏表头 + 条目行）。"""
        out, cur = [], False
        for raw in pack.splitlines():
            line = raw.strip()
            if line.startswith("规矩段："):
                cur = True
                out.append(line)
                continue
            if cur:
                if not line or not (line.startswith("- ") or line.endswith("：")):
                    break
                out.append(line)
        return "\n".join(out)


# ==================================================== FB-01 列出模式（三种调用形态）
class FB01ListModeTest(_Case):
    """FB-01：无 query 时能"列"，tags 单独用给引导，有 query 时行为不变。"""

    def setUp(self):
        super().setUp()
        self._project("PROJA")
        self._project("PROJB")
        # 甲项目：两条带 fish-feedback、一条不带；乙项目：一条带 fish-feedback
        self._mem("PROJA-261026-01", "甲一（反馈）", tags=["fish-feedback"])
        self._mem("PROJA-261026-02", "甲二（反馈）", tags=["fish-feedback", "b2"])
        self._mem("PROJA-261026-03", "甲三（无标签）")
        self._mem("PROJB-261026-01", "乙一（反馈）", project="PROJB", tags=["fish-feedback"])

    # ---------------------------------------------------------------- A1
    def test_A1_tags_only_returns_guidance_not_error(self):
        """只有 tags → 引导文案，**不抛错**（这是"用法缺一半"，不是"参数非法"）。"""
        text = self._recall(tags=["fish-feedback"])
        self.assertIn("tags 只作附加过滤", text,
                      f"tags 单独用应回引导；实际：{text[:200]}")
        self.assertIn("project=", text, "引导里必须给出**怎么写**的例子，而不只是说不行")
        self.assertNotIn("未找到", text, "这不是「没找到」，而是「没给范围」——别混成一句话")

    # ---------------------------------------------------------------- A2
    def test_A2_project_and_tags_without_query_lists_filtered(self):
        """project+tags 无 query → 只出该项目且带 tag 的条目 + 首行声明。"""
        text = self._recall(project="PROJA", tags=["fish-feedback"])
        first = text.splitlines()[0]
        self.assertIn("已按 project=PROJA", first,
                      f"首行必须声明按什么过滤；实际首行：{first!r}")
        self.assertIn("tags=fish-feedback", first, "声明里 tags 也要写出来")
        self.assertIn("共 2 条（时间倒序）", first,
                      f"条数要写在声明里；实际首行：{first!r}")
        codes = CODE_RE.findall(text)
        self.assertEqual(sorted(set(codes)), ["PROJA-261026-01", "PROJA-261026-02"],
                         f"只该列出该项目且带该标签的条目，实际 {codes}")
        self.assertNotIn("甲三", text, "同项目但没打这个标签的条目不该出现")
        self.assertNotIn("乙一", text, "别的项目即使带同标签也不该出现")
        self.assertNotIn("未找到", text, "列出模式下不该再出现「未找到」")

    # ---------------------------------------------------------------- A3
    def test_A3_query_path_unchanged(self):
        """有 query → 仍是检索：回包不带列出声明，结果与 `search_memories` 一致。"""
        self._mem("PROJA-261026-09", "回归用的关键词条目", tags=["fish-feedback"])
        # FTS 是**外部索引**：直接插投影不会自动建索引（MCP 的 recall 会先 ensure_fts）。
        # 判据要和 MCP 走同一条路，所以这里也先建一次——否则测的是"索引没建"，
        # 而不是"query 路径没变"。
        fish.ensure_fts(self.conn, self.root)
        rows, _ = fish.search_memories(self.conn, query="回归", project="PROJA",
                                      tags=["fish-feedback"], limit=10)
        expected = [r[0] for r in rows]
        self.assertTrue(expected, "夹具前提：关键词应能命中")
        text = self._recall(query="回归", project="PROJA", tags=["fish-feedback"])
        self.assertNotIn("已按 project=", text,
                         f"检索模式不该出现列出声明；实际：{text[:200]}")
        for code in expected:
            self.assertIn(code, text, f"检索结果应与 search_memories 一致，缺 {code}")
        self.assertIn("关键词匹配", text, "检索模式的第一行仍是「这是关键词匹配」的交代")

    # ---------------------------------------------------------------- A4
    def test_A4_cli_recall_lists_without_keyword(self):
        """CLI（无关键词）：与 MCP 同一份 `list_memories`，回包同一套声明。"""
        args = argparse.Namespace(root=str(self.root), query=None, code=None,
                                  project="PROJA", tags=["fish-feedback"],
                                  scope="all", limit=10)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_recall(args)
        out = buf.getvalue()
        self.assertIn("已按 project=PROJA、tags=fish-feedback 过滤，共 2 条（时间倒序）",
                      out, f"CLI 也应给同一句声明；实际：{out[:300]}")
        codes = sorted(set(CODE_RE.findall(out)))
        self.assertEqual(codes, ["PROJA-261026-01", "PROJA-261026-02"],
                         f"CLI 列出的条目应与 MCP 一致，实际 {codes}")
        # 只有 tags（没有 project）时 CLI 也给引导，不报错
        args2 = argparse.Namespace(root=str(self.root), query=None, code=None,
                                   project=None, tags=["fish-feedback"],
                                   scope="all", limit=10)
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            fish.cmd_recall(args2)
        self.assertIn("tags 只作附加过滤", buf2.getvalue(), "CLI 的 tags 单独用也应是引导")


# ==================================================== FB-02 注入包自解释（四条判据）
class FB02InjectionDeclaredTest(_Case):
    """FB-02：规矩段声明 N/M、可分段、看板有上限、截断有声明且与 meta 一致。"""

    # ---------------------------------------------------------------- B1
    def test_B1_rules_block_declares_shown_vs_total(self):
        """规矩段含 `显示 N/M 条`；N<M 时并声明"其余因预算/上限省略"。"""
        self._rules(7)
        pack, meta = self._pack()
        section = self._rules_section(pack)
        self.assertTrue(section, f"夹具前提：规矩段应出现；包片段：\n{pack[:500]}")
        m = SHOW_RE.search(section)
        self.assertIsNotNone(m, f"规矩段头必须写「显示 N/M 条」；实际段：\n{section}")
        shown, total = int(m.group(1)), int(m.group(2))
        self.assertEqual((shown, total), (3, 7),
                         f"默认每栏 3 条、共有 7 条 → 应写 3/7；实际 {shown}/{total}")
        self.assertIn("其余因预算/上限省略", section,
                      "确实少了 4 条，就必须说「其余被省掉」——否则读者以为规矩只有 3 条")
        # 声明里的 M 与库里真实条数一致（不是把 limit 当成了总数）
        self.assertEqual(fish.count_rule_rows(self.conn, project="PROJA"), 7)

    # ---------------------------------------------------------------- B1b
    def test_B1b_rules_per_source_is_a_config_key_with_range(self):
        """`inject.rules_per_source` 真的生效（默认 3），且越界被**夹到 1–10**。"""
        self.assertEqual(fish.CONFIG_DEFAULTS["inject.rules_per_source"][:2], (3, "int"),
                         "配置键必须在 CONFIG_DEFAULTS 里登记（config list 才读得到）")
        self.assertEqual(fish.RULES_PER_SOURCE_LIMIT, 3, "常量应派生自配置默认值（同一份真相）")
        self._rules(12)
        pack, _ = self._pack()
        self.assertIn("显示 3/12 条", self._rules_section(pack),
                      "默认每栏 3 条")
        fish.set_config(self.conn, "inject.rules_per_source", 5)
        pack5, _ = self._pack()
        self.assertIn("显示 5/12 条", self._rules_section(pack5),
                      "调成 5 后段头必须跟着变（否则这个键是死的）")
        # 越界不该让注入包报错，但也不许"静默给全部"：夹到边界并如实显示
        fish.set_config(self.conn, "inject.rules_per_source", 99)
        pack_hi, _ = self._pack()
        self.assertIn(f"显示 {fish.RULES_PER_SOURCE_MAX}/12 条", self._rules_section(pack_hi),
                      "99 应被夹到上限 10")
        fish.set_config(self.conn, "inject.rules_per_source", 0)
        pack_lo, _ = self._pack()
        self.assertIn(f"显示 {fish.RULES_PER_SOURCE_MIN}/12 条", self._rules_section(pack_lo),
                      "0 应被夹到下限 1（0 条规矩等于这段没意义）")

    # ---------------------------------------------------------------- B2
    def test_B2_sections_selects_only_requested(self):
        """`sections=["status"]` → 无看板/规矩段，但 **L0 身份段仍在**（不可关闭）。"""
        self._rules(3)
        board = fish.board_ensure(self.conn, "project", "PROJA")
        fish.board_post(self.conn, board, self._citizen(), "progress", "在写 B 批判据")
        pack, meta = self._pack(sections=["status"])
        self.assertIn("三条铁律", pack, "L0 铁律永远保留（身份不可关闭）")
        self.assertIn("记忆根目录", pack, "L0 的根目录行也在")
        self.assertNotIn("看板（", pack, "没要 board 段，包内不该有看板")
        self.assertNotIn("本项目规矩（", pack, "没要 rules 段，包内不该有规矩")
        self.assertNotIn("【操作指引】", pack, "没要 index 段，包内不该有 L2")
        self.assertEqual(meta["sections"], ["status"], "meta 要如实报本次要了哪几段")
        # 反向：不传 sections = 全段（旧行为不变）
        full, meta_full = self._pack()
        self.assertIn("本项目规矩（", full, "不传 sections 时必须仍是全段")
        self.assertIn("【操作指引】", full)
        self.assertEqual(len(meta_full["sections"]), len(fish.INJECTION_SECTIONS))
        # 拼错的段名必须响亮失败（静默少一段是最难查的坏）
        with self.assertRaises(ValueError):
            self._pack(sections=["nope"])

    # ---------------------------------------------------------------- B3
    def test_B3_board_section_respects_ratio_and_says_truncated(self):
        """500 条动态 → 看板段 ≤ 预算×0.25，且段头有"已按预算截断"。"""
        self._project("PROJA")
        board = fish.board_ensure(self.conn, "project", "PROJA")
        cid = self._citizen()
        bulk = [(board["id"], cid, "progress",
                 f"第 {i} 条动态：把这块推进到可交付的状态", fish.now_utc())
                for i in range(500)]
        self.conn.executemany(
            "INSERT INTO board_posts (board_id,citizen_id,kind,text,created_at) "
            "VALUES (?,?,?,?,?)", bulk)
        self.conn.commit()
        # 预算 700：既让看板段**活下来**（L0 约 250 token，太小的预算会把它整段裁掉，
        # 那样测的就不是"段内截断"而是"整段消失"），又让它必须截断（cap=175，
        # 5 条动态 + 段头 + 页脚约 200 token）。
        budget = 700
        pack, meta = self._pack(budget=budget, sections=["status", "board"])
        section = self._board_section(pack)
        self.assertTrue(section, f"夹具前提：看板段应出现；包片段：\n{pack[:600]}")
        cap = int(budget * float(fish.CONFIG_DEFAULTS["inject.board_max_ratio"][0]))
        tokens = fish.count_tokens(section)
        # +15 的余量：段头那句"（已按预算截断）"是在截断之后才加上的，
        # 它本身也要占 token——上限管的是"内容"，不是这句自我声明。
        self.assertLessEqual(tokens, cap + 15,
                             f"看板段 {tokens} token 超了预算×0.25（{cap}）：\n{section}")
        self.assertIn("已按预算截断", section,
                      f"截断过就必须在段头说；实际段头：{section.splitlines()[0]!r}")
        self.assertGreater(meta["truncated"]["board"], 0,
                           "meta 也要报出被截掉的行数（与段头两处一致）")

    # ---------------------------------------------------------------- B4
    def test_B4_truncated_meta_matches_pack_and_tail(self):
        """`meta["truncated"]` = 包尾声明 = 包里实际省略的条数（三者一致）。"""
        self._rules(7)          # 本项目 7 条 → 默认列 3 条 → 省 4
        pack, meta = self._pack()
        section = self._rules_section(pack)
        printed = len(set(CODE_RE.findall(section)))
        self.assertEqual(printed, 3, f"夹具前提：段内应印 3 条；实际段：\n{section}")
        self.assertEqual(meta["truncated"]["rules"], 7 - printed,
                         f"meta.truncated.rules 应等于 7-{printed}；实际 {meta['truncated']}")
        tail = [ln for ln in pack.splitlines() if ln.startswith("本次截断")]
        self.assertEqual(len(tail), 1, f"有截断就必须有一行声明；包尾：\n{pack[-300:]}")
        self.assertIn(f"规矩 {7 - printed} 条", tail[0],
                      f"包尾声明的数字要与 meta 一致；实际：{tail[0]!r}")
        # 无省略时：写"本次无截断"（"没有发生"也要说，否则读者分不清"没截断"和"没说"）
        self.tearDown()
        self.setUp()
        self._rules(2)          # 2 条 < 每栏上限 3 → 一条都没省
        pack2, meta2 = self._pack()
        self.assertIn("本次无截断", pack2,
                      f"没截断时应显式说「本次无截断」；包尾：\n{pack2[-300:]}")
        self.assertEqual(sum(meta2["truncated"].values()), 0,
                         f"没截断时 meta.truncated 应全 0；实际 {meta2['truncated']}")

    # ---------------------------------------------------------------- B5
    def test_B5_budget_900_and_1200_self_explain(self):
        """900 / 1200 两个预算：**不超预算**，且包尾两行都能自解释。

        为什么单独钉两个预算值：这是产品上真实会用到的两档（普通会话 / 长上下文）。
        "能自解释"= 读者从包里就能看出"这份包砍过没有、截至什么时候"，
        而不是靠 meta 或外部文档。包尾声明也**算在预算里**（否则声明一行就超了）。
        """
        self._rules(9)
        for i in range(20, 26):
            self._mem(f"PROJA-261026-{i:02d}", f"未闭合：待办事项{i}")
        board = fish.board_ensure(self.conn, "project", "PROJA")
        cid = self._citizen()
        for i in range(30):
            fish.board_post(self.conn, board, cid, "progress", f"第 {i} 条动态：推进中")
        for budget in (900, 1200):
            pack, meta = self._pack(budget=budget)
            self.assertLessEqual(meta["tokens"], budget,
                                 f"预算 {budget} 却出了 {meta['tokens']} token")
            self.assertFalse(meta["over_budget"], f"预算 {budget} 不该报超预算")
            self.assertEqual(meta["budget"], budget, "meta.budget 必须是调用方给的那个数")
            self.assertRegex(pack, r"(本次截断：|本次无截断)",
                             "包尾必须声明这次砍了什么")
            m = AS_OF_RE.search(pack)
            self.assertIsNotNone(m, "包尾必须有 as-of 行")
            self.assertEqual(meta["as_of"], m.group(1), "meta.as_of 与包内 as-of 同值")


# ==================================================== FB-16 as-of（数据截止时刻）
class FB16AsOfTest(_Case):
    """FB-16：包内出现 `as-of <ISO8601Z>`，可被 strptime 解析，meta 同值。"""

    def test_C1_as_of_line_parsable_and_in_meta(self):
        self._project("PROJA")
        self._mem("PROJA-261026-01", "一条事实")
        pack, meta = self._pack()
        m = AS_OF_RE.search(pack)
        self.assertIsNotNone(m, f"包内必须有 as-of 行；包尾：\n{pack[-300:]}")
        parsed = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ")
        self.assertIsInstance(parsed, datetime.datetime)
        self.assertEqual(meta["as_of"], m.group(1),
                         f"meta.as_of 必须与包内 as-of 同值；"
                         f"meta={meta['as_of']!r} 包内={m.group(1)!r}")
        self.assertIn("数据截至", pack, "as-of 行要说明它的语义（这是数据截止时刻）")
        # 与当前时刻的偏差应很小（写死一个旧时间就失去意义了）
        now = datetime.datetime.strptime(fish.now_utc(), "%Y-%m-%dT%H:%M:%SZ")
        self.assertLess(abs((now - parsed).total_seconds()), 120,
                        "as-of 必须是本次构建的时刻，不能是写死的旧值")

    def test_C2_as_of_sits_before_mcp_token_comment(self):
        """MCP 回包里 as-of 在 `<!-- tokens/budget -->` **之前**（= 包正文的结尾）。"""
        self._project("PROJA")
        self._mem("PROJA-261026-01", "一条事实")
        text = self.srv.call_tool("inject", {"project": "PROJA"}, self.ident)
        i_as_of = text.find("as-of ")
        i_comment = text.find("<!--")
        self.assertGreater(i_as_of, 0, f"回包里应有 as-of 行；尾部：\n{text[-300:]}")
        self.assertGreater(i_comment, 0, "MCP 仍应在包尾追加 tokens/budget 注释")
        self.assertLess(i_as_of, i_comment,
                        "as-of 属于包正文，必须排在 tokens/budget 注释之前")


if __name__ == "__main__":
    unittest.main(verbosity=2)
