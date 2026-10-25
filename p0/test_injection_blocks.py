#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入包两段（③规矩 / ④该注意什么）的判据。

背景
----
2026-10-24 全局复盘：新 AI 接入时「这里的规矩」与「我该注意什么」两段完全缺失，
是"磕绊"最大的结构性原因。段③/④ 由此补上。

2026-10-25 修正（本轮）：段③ 的分栏口径原来是错的。
  上一版：主人规则 ← `owner = resolve_owner()`；项目规则 ← `project = 当前项目`。
  **错在哪**：`code_index.owner` 记的是"谁写的"，不是"这条规范约束谁"。
  生产库实证：resolve_owner() = 安逸，安逸名下 rule = 0 条 → 「主人」栏永远空；
  全库 39 条 active rule 的作者是 workbuddy-main(37) / codex-main(1) / 无归属(1)。
  现行口径（**按适用范围，不按作者**）：

    本项目规矩（ANYI）：          ← mem_type='rule' AND project = 当前项目
    - ANYI-261022-85 ER-31 · ...
    全局规矩：                    ← mem_type='rule' AND project IS NULL
    - XXXX-...

  全局栏在生产库目前是 **0 条**（所有 rule 都带 project）→ **空就不出现**，这是正常态。
  段④ 的 B 类「最近的坑」同时改为**同项目优先**：排序键 = (是否同项目 DESC, recorded_at DESC)，
  同项目最多取 2 条，不足 2 条再用跨项目补齐（跨项目的通用教训仍保留，只是排序靠后）。

2026-10-25 二修（T9）：段④ 的 B 类「最近的坑」**独立成段⑤「错题本」**。
  理由：`rule`（该怎么做）与 `lesson`（别再这样）是**两种性质的约束**，混在一段里
  AI 分不清"通用教训"与"本项目的事"，也让「规矩（长期）/ 注意（临时）」这条已有
  分层被第三种东西糊住。拆开后：③规矩 = 规范，④该注意什么 = 临时信号，⑤错题本 = 踩过的坑。

  **判据随之改读段⑤（`LESSON_HEADER`），强度不变**：同项目优先 / 跨项目补齐 /
  meta 一致这些断言一条没松，只是换了"从哪个段读"。这不是可选项——若保留旧读法，
  B′ 的 `assertNotIn` 会变成恒真（段④里本来就不会有 lesson），那是**把判据改成空断言**，
  比改坏更危险（看起来永远是绿的）。

  新增 L1–L4（见文末）：空段不出现 / 段④不再含 lesson / 同编码不跨段重复 / exclude 生效。

判据（每条都在 mutation_injection_blocks.py 里有变异自证）
----------------------------------------------------------
A′ 规矩段只列**本项目**的 rule（按适用范围，不按作者）
   | 变异：改回按 owner 筛 → owner=主人但别的项目的规矩被选中 → 红
A2 `project IS NULL` 的 rule 出现在「全局规矩」栏
   | 变异：不查 NULL（`project = NULL`）→ 漏 → 红
A3 没有 `project IS NULL` 的 rule → **不出现「全局规矩」字样**
   | 变异：无条件输出全局表头 → 红
B  规矩段只取标题，不取正文 | 变异：把正文塞进段里 → 超长 → 红
B′ B 类 lesson **同项目优先**（同项目 3 条比跨项目 5 条都旧 → 仍取同项目）
   | 变异：只按时间排 → 取到跨项目 → 红
B2 同项目只有 1 条 lesson → 第 2 条用**跨项目补齐**（不空着）
   | 变异：严格只取同项目 → 只有 1 条 → 红
C  规矩段空时整段不出现 | 变异：无条件输出表头 → 红
D  未闭合事项按**标题**命中 | 变异：不筛标题（所有 active 都算）→ 红
E  高价值未读复用 S1 的 `_knowledge_gaps` | 变异：自己重写一份（同类范围不一致）→ 红
F  注意事项三类都空时整段不出现 | 变异：无条件输出表头 → 红
G  预算裁剪顺序 L2 → 注意事项 → 规矩 | 变异：先砍规矩 → 红
H  meta 记录段③④里印进包的编码 | 变异：不记 → 红

附加判据（本轮守卫，无独立变异；语义已被上面的变异覆盖）
------------------------------------------------------
I  段自身的硬上限（超了就少列条目并留 trimmed 标记，不截断标题）
J  同一条编码不在两栏各出现一次（本项目栏 / 全局栏谓词互斥）
K  装配次序：L0 → L1 → ③规矩 → ④该注意什么 → L2

跑法
----
    python test_injection_blocks.py             # 全部
    python -m pytest test_injection_blocks.py -q
"""
import datetime
import importlib.util
import re
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

# 编码形如 `PROJA-261024-01`。从包里"抽回印进去的编码"是判据 H 的关键动作：
# 不拿实现里的列表自证，而是**从印出来的文本反解**——那才是"AI 真的看见了什么"。
CODE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*-\d{6}-\d+\b")

OWNER = "01OWNER0000000000000001"     # 主人（is_owner=1）——**只影响 L0，不再影响规矩分栏**
ACTOR = "01ACTOR0000000000000002"     # 本会话的行动者（多数规矩的作者）
OTHER = "01OTHER0000000000000003"     # 别人（作者≠适用范围）

RULES_HEADERS = ("本项目规矩（", "全局规矩：")
PROJECT_HEADER = "本项目规矩（"
GLOBAL_HEADER = "全局规矩："
ATTENTION_HEADER = "该注意什么"
# 2026-10-25（T9）：错题本从段④独立成段⑤。判据随之改读这一段——
# **不是放宽**：断言强度原样保留（同项目优先 / 跨项目补齐 / 编码与 meta 一致），
# 只是换了"从哪个段读"。若保留旧读法，B′ 的 assertNotIn 会变成恒真（空断言）,
# 那才是真的把判据改坏了。
LESSON_HEADER = "错题本"


def _ago(minutes):
    """`minutes` 分钟前的 UTC 时刻（`recorded_at` 的显式形状）。"""
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class InjectionBlocksTest(unittest.TestCase):
    """③规矩 / ④该注意什么 的判据集。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.data = self.root / "data"
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self._citizen(OWNER, "主人甲", is_owner=1)
        self._citizen(ACTOR, "行动者乙")
        self._citizen(OTHER, "其他人丙")
        for prefix, ns in (("PROJA", "甲项目"), ("PROJB", "乙项目")):
            self.conn.execute(
                "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
                "status) VALUES (?,?,NULL,0,'active')", (prefix, ns))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------ 夹具
    def _citizen(self, cid, name, is_owner=0):
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at,"
            "is_owner) VALUES (?,?,?,?,?,?,?,?)",
            (cid, name, "resident" if is_owner else "visitor", None, None, "active",
             fish.now_utc(), is_owner))

    def _mem(self, code, title, *, project="PROJA", owner=ACTOR, mem_type="fact",
             status="active", dormant=0, recorded_at=None):
        ts = recorded_at or fish.now_utc()
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared',?,'[]',NULL,?,?,0,?)",
            (code, title, project, owner, mem_type, status, ts, ts, dormant))
        self.conn.commit()

    def _read(self, code, session_key, citizen_id=ACTOR, via="recall"):
        """写"按人"访问记录（C 类的唯一数据源）。"""
        fish.append_access_log(self.conn, [code], via=via, citizen_id=citizen_id,
                               session_key=session_key)

    def _pack(self, **kw):
        kw.setdefault("project", "PROJA")
        if "budget" in kw and kw["budget"] is None:
            kw.pop("budget")
        return fish.build_injection_pack(self.conn, self.data, ACTOR, **kw)

    # ------------------------------------------------------------ 从包里反解
    def _section_lines(self, pack, headers):
        """抽出某个段的条目行（段头行本身被 `headers` 匹配）。

        段结束判据 = 出现一行不以 `- ` 开头的内容（空行也算）。
        为什么不用"下一个段头"来判断结束：段可能不存在，只有"条目行连续与否"
        这一个信号在所有段（本项目规矩 / 全局规矩 / 该注意什么）上都成立。
        """
        out, cur = [], False
        for raw in pack.splitlines():
            line = raw.strip()
            if any(line.startswith(h) for h in headers):
                cur = True
                continue
            if cur:
                if line.startswith("- "):
                    out.append(line)
                else:
                    cur = False
        return out

    def _section_codes(self, pack, headers):
        codes = []
        for line in self._section_lines(pack, headers):
            found = CODE_RE.findall(line)
            if found:
                codes.append(found[0])
        return codes

    def _lesson_codes(self, pack):
        """段⑤「错题本」里的编码，按印出的顺序。

        2026-10-25（T9）：lesson 从段④「该注意什么」独立成段⑤，所以这里改读段⑤。
        段内行格式随之统一成 `- <编码> <标题>`（不再带「最近的坑：」前缀）——
        因为整段都是坑，前缀是冗余；这也是判据要跟着变的地方。
        """
        return self._section_codes(pack, (LESSON_HEADER,))

    # ------------------------------------------------------------ A′
    def test_A_rules_by_scope_not_author(self):
        """A′：规矩段只列「本项目」的 rule——**按适用范围，不按作者**。

        夹具刻意造一条 `owner=主人` 但**属于乙项目**的规矩：上一版按 owner 筛会把它
        选中（当作"主人的硬规则"），现行口径必须一条都不选。三条本项目的规矩作者
        都是行动者（不是主人）——按作者筛会连它们一起漏掉，所以红的是整段。
        """
        for i in (1, 2, 3):     # i=1 最新（_ago 的分钟数随 i 增大 → 更旧）
            self._mem(f"PROJA-261024-{i:02d}", f"本项目规矩{i}", owner=ACTOR,
                      mem_type="rule", recorded_at=_ago(29 + i))
        self._mem("PROJB-261024-01", "乙项目规矩一", project="PROJB", owner=OTHER,
                  mem_type="rule", recorded_at=_ago(12))
        self._mem("PROJB-261024-02", "乙项目规矩二", project="PROJB", owner=OTHER,
                  mem_type="rule", recorded_at=_ago(11))
        # 变异靶子：主人写的、但属乙项目 → 按 owner 筛必被选中
        self._mem("PROJB-261024-03", "主人写但属于乙项目的规矩", project="PROJB",
                  owner=OWNER, mem_type="rule", recorded_at=_ago(10))

        pack, meta = self._pack()
        codes = self._section_codes(pack, RULES_HEADERS)
        self.assertEqual(
            codes, ["PROJA-261024-01", "PROJA-261024-02", "PROJA-261024-03"],
            f"规矩段应只列本项目的 3 条（按 recorded_at 降序），实际 {codes}")
        self.assertIn(f"{PROJECT_HEADER}PROJA）：", pack, "本项目栏应出现且带项目名")
        # 断言"别的项目的规矩没进段"时**只看标题**，不能用编码：那些编码会出现在 L2
        # 的「今日编码」里（它们确实是今天记的）——那与"规矩段筛适用范围"是两件事。
        for leaked in ("主人写但属于乙项目的规矩", "乙项目规矩一", "乙项目规矩二"):
            self.assertNotIn(leaked, pack, f"别的项目的规矩不该出现在注入包里：{leaked}")
        self.assertEqual(meta["rules_codes"], codes,
                         f"meta.rules_codes 与包里印出的不一致：{meta['rules_codes']}")

    # ------------------------------------------------------------ A2
    def test_A2_global_rule_in_global_section(self):
        """A2：`project IS NULL` 的 rule 出现在「全局规矩」栏（且只在这一栏）。"""
        self._mem("PROJA-261024-01", "本项目规矩一", mem_type="rule", recorded_at=_ago(20))
        self._mem("GBTEST-261024-01", "全局规矩一", project=None, owner=OTHER,
                  mem_type="rule", recorded_at=_ago(10))

        pack, meta = self._pack()
        global_codes = self._section_codes(pack, (GLOBAL_HEADER,))
        self.assertEqual(global_codes, ["GBTEST-261024-01"],
                         f"全局规矩应出现在「全局规矩」栏，实际 {global_codes}；"
                         f"包片段：\n{pack[:600]}")
        self.assertIn("全局规矩一", pack)
        all_codes = self._section_codes(pack, RULES_HEADERS)
        self.assertEqual(all_codes, ["PROJA-261024-01", "GBTEST-261024-01"],
                         f"两栏合计应是本项目 1 条 + 全局 1 条，实际 {all_codes}")
        self.assertEqual(meta["rules_codes"], all_codes)

    # ------------------------------------------------------------ A3
    def test_A3_no_global_section_when_empty(self):
        """A3：没有 `project IS NULL` 的 rule → **不出现「全局规矩」字样**（不写"暂无"）。

        生产库现在就是这种状态（39 条 rule 全部带 project）：空栏必须整块消失，
        否则"有信号"和"没信号"在观感上被抹平，AI 会开始跳过这一段。
        """
        self._mem("PROJA-261024-01", "本项目规矩一", mem_type="rule", recorded_at=_ago(20))
        pack, meta = self._pack()
        self.assertIn(PROJECT_HEADER, pack, "本项目栏有规矩时必须出现")
        self.assertNotIn(GLOBAL_HEADER, pack,
                         f"没有全局规矩时不该出现「全局规矩」字样；包片段：\n{pack[:600]}")
        self.assertEqual(meta["rules_codes"], ["PROJA-261024-01"])

    # ------------------------------------------------------------ B
    def test_B_rules_only_carry_titles(self):
        """B：规矩段只给 `code + 标题`，正文再长也不进包（段长必须远小于正文）。"""
        long_body = "正文正文正文正文正文正文正文正文正文正文" * 150
        code, rel = fish.record_memory(
            self.conn, self.data, project="PROJA", owner=OWNER, mem_type="rule",
            title="长正文规矩：只该露出标题", body=long_body,
            occurred_at=None)
        self.assertTrue((self.data / rel).exists(), "夹具前提：正文文件应已落盘")

        pack, _meta = self._pack()
        block = "\n".join(self._section_lines(pack, RULES_HEADERS))
        self.assertIn(code, block, f"规矩段里应有这条（编码 {code}）；实际段内容：\n{block}")
        self.assertLess(len(block), 200,
                        f"规矩段应只含 code+标题，实际 {len(block)} 字符（正文被塞进来了？）")
        self.assertNotIn("正文正文正文正文正文正文正文正文正文正文", block,
                         "正文不该进注入包（只取标题，全文用 resolve_code 按需读）")

    # ------------------------------------------------------------ B′
    def test_B_prime_lessons_same_project_first(self):
        """B′：B 类「最近的坑」**同项目优先**——同项目 3 条全部比跨项目 5 条旧，仍取同项目。

        排序键 = (是否同项目 DESC, recorded_at DESC)。上一版只按时间排，会取到两条
        乙项目的坑（它们更新）；给甲项目工作的 AI 最该先知道甲的坑。
        """
        # 同项目 3 条：都更旧（近 7 天窗口内）；i=1 最新
        for i in (1, 2, 3):
            self._mem(f"PROJA-261024-{i:02d}", f"甲项目教训{i}", mem_type="lesson",
                      recorded_at=_ago(590 + i * 10))
        # 别的项目 5 条：都更新（纯按时间排必取它们）
        for i in range(1, 6):
            self._mem(f"PROJB-261024-{i:02d}", f"乙项目教训{i}", project="PROJB",
                      mem_type="lesson", recorded_at=_ago(10 + i))

        pack, meta = self._pack()
        codes = self._lesson_codes(pack)
        self.assertEqual(codes, ["PROJA-261024-01", "PROJA-261024-02"],
                         f"同项目优先应取甲项目最新的 2 条，实际 {codes}")
        section = "\n".join(self._section_lines(pack, (LESSON_HEADER,)))
        self.assertNotIn("乙项目教训", section,
                         f"同项目已满 2 条时不该再列跨项目 lesson；实际段：\n{section}")
        for code in codes:
            # T9：lesson 的编码记在 meta["lesson_codes"]（段⑤独立后的归属）。
            # 断言强度不变——仍然要求"印进包里的每一条都被 meta 记到"。
            self.assertIn(code, meta["lesson_codes"])

    # ------------------------------------------------------------ B2
    def test_B2_lessons_cross_project_fill(self):
        """B2：同项目只有 1 条 lesson → 第 2 条用**跨项目补齐**（不空着，不排除跨项目）。"""
        self._mem("PROJA-261024-01", "甲项目唯一的教训", mem_type="lesson",
                  recorded_at=_ago(700))
        for i in range(1, 6):
            self._mem(f"PROJB-261024-{i:02d}", f"乙项目教训{i}", project="PROJB",
                      mem_type="lesson", recorded_at=_ago(10 + i))

        pack, _meta = self._pack()
        codes = self._lesson_codes(pack)
        self.assertEqual(codes, ["PROJA-261024-01", "PROJB-261024-01"],
                         f"应是「同项目 1 条 + 跨项目最新 1 条」，实际 {codes}")
        section = "\n".join(self._section_lines(pack, (LESSON_HEADER,)))
        self.assertIn("乙项目教训1", section, "跨项目补齐的应当是最近的那条跨项目 lesson")

    # ------------------------------------------------------------ C
    def test_C_no_rules_no_section(self):
        """C：库里没有 rule → 整段不出现（不写"暂无规矩"这种噪音）。"""
        self._mem("PROJA-261024-01", "只是一条事实")
        pack, meta = self._pack()
        self.assertNotIn("规矩（", pack,
                         f"没有 rule 时不该出现规矩段；包片段：\n{pack[:400]}")
        self.assertNotIn(GLOBAL_HEADER, pack, "没有全局规矩时也不该出现全局栏")
        self.assertEqual(meta["rules_codes"], [])
        self.assertEqual(meta["layers"]["rules"], 0)

    # ------------------------------------------------------------ D
    def test_D_open_loop_matched_by_title(self):
        """D：未闭合事项按**标题里的词**命中（近 30 天、active）；非命中标题不许冒充。"""
        self._mem("PROJA-261024-01", "部署待确认：口令轮换还没做",
                  recorded_at=_ago(5))
        self._mem("PROJA-261024-02", "一条普通事实，标题里没有任何开放词",
                  recorded_at=_ago(4))
        pack, meta = self._pack()
        self.assertIn("- 未闭合：PROJA-261024-01", pack,
                      f"标题含「待确认」的记忆应进未闭合；包片段：\n{pack[:600]}")
        self.assertNotIn("- 未闭合：PROJA-261024-02", pack,
                         "标题不含开放词的记忆不是未闭合——不筛标题就会把它算进来")
        self.assertIn("PROJA-261024-01", meta["attention_codes"])

    # ------------------------------------------------------------ E
    def test_E_high_value_gap_reuses_s1_threshold(self):
        """E：C 类「别处读过但你没看」必须复用 S1 的口径（≥3 次 / 本会话 0 次）。

        夹具刻意放两条：一条别人读 **3** 次（达标）、一条读 **2** 次（**不达标**）。
        这两个次数写成**字面量**、不取 `KNOWLEDGE_GAP_MIN_PEER_READS`——否则"门槛被
        改小"的变异会同时把夹具一起改小，判据永远绿（这条坑在 mutation M-E2 上踩过）。
        判据要钉的是：段里的结论跟着 **S1 的门槛**走，而不是跟着"另一份实现"走。
        """
        self._mem("PROJA-261024-01", "别人常读的那条")
        self._mem("PROJA-261024-02", "只被读两次的那条")
        for _ in range(3):        # 3 = S1 的门槛（KNOWLEDGE_GAP_MIN_PEER_READS）
            self._read("PROJA-261024-01", "别人-会话")
        for _ in range(2):        # 2 = 门槛减一：不达标
            self._read("PROJA-261024-02", "别人-会话")

        pack, meta = self._pack(session_actor="我-会话")
        self.assertIn("- 别处读过但你没看：PROJA-261024-01", pack,
                      f"别人读够门槛的那条应出现在 C 类；包片段：\n{pack[:800]}")
        self.assertNotIn("别处读过但你没看：PROJA-261024-02", pack,
                         "别人只读了 2 次（低于 S1 门槛）不该算「别人常读」")
        self.assertIn("PROJA-261024-01", meta["attention_codes"])

    # ------------------------------------------------------------ F
    def test_F_no_attention_no_section(self):
        """F：三类信号都空 → 整段不出现。"""
        self._mem("PROJA-261024-01", "一条普通事实")
        pack, meta = self._pack()
        self.assertNotIn("该注意什么", pack,
                         f"三类都空时不该出现注意事项段；包片段：\n{pack[:400]}")
        self.assertEqual(meta["attention_codes"], [])
        self.assertEqual(meta["layers"]["attention"], 0)

    # ------------------------------------------------------------ G
    def _seed_full(self):
        """G 的夹具：规矩 + 注意事项 + L2 三样都非空。"""
        for i, name in enumerate(("规矩一", "规矩二", "规矩三"), start=1):
            self._mem(f"PROJA-261024-{i:02d}", name, owner=OWNER, mem_type="rule",
                      recorded_at=_ago(20 - i))
        self._mem("PROJA-261024-10", "下一步：把裁剪顺序钉进判据", recorded_at=_ago(3))
        self._mem("PROJA-261024-11", "判断谁在写的证据顺序（教训）", mem_type="lesson",
                  recorded_at=_ago(2))
        # L2 非空：多一个项目注册表条目即可
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('PROJC','丙项目',NULL,0,'active')")
        self.conn.commit()

    def test_G_trim_order_drops_index_before_rules(self):
        """G：裁剪顺序 L2 → ④该注意什么 → ③规矩；L2 没裁掉之前不许先砍规矩。

        做法不是"猜一个预算"，而是**扫一遍预算区间**，逐档验两条不变量：
          ① 只要 L2 还在，规矩/注意就不许被裁（顺序反了就会在这里红）；
          ② 存在"L2 已裁、规矩仍在"的档位（否则说明规矩比索引更早被牺牲）。
        """
        self._seed_full()

        kept = []
        for budget in range(80, 1400, 20):
            pack, meta = self._pack(budget=budget)
            tr = meta["trimmed"]
            if "L2" not in tr and ("规矩" in tr or "注意事项" in tr):
                self.fail(f"budget={budget}：L2 还在就先砍了规矩/注意事项（trimmed={tr}）")
            if "L2" in tr and "规矩" not in tr and "规矩（" in pack:
                kept.append(budget)
            if "注意事项" in tr and "规矩" in tr:
                self.assertLess(tr.index("注意事项"), tr.index("规矩"),
                                f"budget={budget}：注意事项必须先于规矩被裁，实际 {tr}")
        self.assertTrue(kept, "扫描区间里没有「L2 已裁但规矩仍在」的档位——规矩被砍得太早")

        # 极小预算：三段都得让位，且次序必须是 L2 → 注意事项 → 规矩
        pack, meta = self._pack(budget=10)
        tr = meta["trimmed"]
        for token in ("L2", "注意事项", "规矩"):
            self.assertIn(token, tr, f"预算 10 token 时 {token} 应被裁掉；实际 {tr}")
        self.assertLess(tr.index("L2"), tr.index("注意事项"))
        self.assertLess(tr.index("注意事项"), tr.index("规矩"))
        self.assertTrue(meta["over_budget"], "最小包仍超预算必须响亮报告")
        self.assertIn(ACTOR, pack, "身份行永不被裁剪")

    # ------------------------------------------------------------ H
    def test_H_meta_records_printed_codes(self):
        """H：`meta["rules_codes"]` / `meta["attention_codes"]` = **印进包里的**编码。"""
        self._mem("PROJA-261024-01", "本项目规矩甲", owner=OWNER, mem_type="rule",
                  recorded_at=_ago(9))
        self._mem("PROJA-261024-02", "本项目规矩乙", owner=OTHER, mem_type="rule",
                  recorded_at=_ago(8))
        self._mem("GBTEST-261024-01", "全局规矩甲", project=None, owner=OTHER,
                  mem_type="rule", recorded_at=_ago(7))
        self._mem("GBTEST-261024-02", "全局规矩乙", project=None, owner=OWNER,
                  mem_type="rule", recorded_at=_ago(6))
        self._mem("PROJA-261024-10", "TODO：把注入包的 meta 钉进判据", recorded_at=_ago(3))
        self._mem("PROJA-261024-11", "判断谁在写的证据顺序（教训）", mem_type="lesson",
                  recorded_at=_ago(2))

        pack, meta = self._pack()
        printed_rules = self._section_codes(pack, RULES_HEADERS)
        printed_attention = self._section_codes(pack, (ATTENTION_HEADER,))
        printed_lesson = self._section_codes(pack, (LESSON_HEADER,))
        self.assertEqual(printed_rules, meta["rules_codes"],
                         f"meta.rules_codes 与包里印出的编码不一致："
                         f"包={printed_rules} meta={meta['rules_codes']}")
        self.assertEqual(printed_attention, meta["attention_codes"],
                         f"meta.attention_codes 与包里印出的编码不一致："
                         f"包={printed_attention} meta={meta['attention_codes']}")
        # 2026-10-25（T9）：段⑤独立后，H 从"一段两个 meta"变成"三段三个 meta"。
        # 断言**只增不减**：原来只钉 attention，现在 rules / attention / lesson
        # 三个 meta 都要与包里印出的编码逐个相等，且 lesson 不许漏记。
        self.assertEqual(printed_lesson, meta["lesson_codes"],
                         f"meta.lesson_codes 与包里印出的编码不一致："
                         f"包={printed_lesson} meta={meta['lesson_codes']}")
        self.assertEqual(len(printed_rules), 4, "本项目的 2 条 + 全局的 2 条都应印出来")
        self.assertEqual(len(printed_attention), 1, "未闭合 1 条（最近的坑已独立成段⑤）")
        self.assertEqual(len(printed_lesson), 1, "段⑤错题本 1 条")
        self.assertEqual(sorted(printed_attention + printed_lesson),
                         ["PROJA-261024-10", "PROJA-261024-11"],
                         "两段合起来仍应覆盖原来的两条信号，且不重不漏")
        self.assertGreater(meta["layers"]["rules"], 0)
        self.assertGreater(meta["layers"]["attention"], 0)
        self.assertGreater(meta["layers"]["lesson"], 0)

    # ------------------------------------------------------------ I（附加）
    def test_I_segment_caps_trim_items_not_titles(self):
        """I：段自身硬上限超了就**少列条目**并留 trimmed 标记，标题一字不截。"""
        title = "一条很长很长的本项目规矩标题，用来把段的 token 预算顶爆"
        for i in range(1, 4):
            self._mem(f"PROJA-261024-{i:02d}", f"{title}{i}", owner=ACTOR,
                      mem_type="rule", recorded_at=_ago(10 - i))
        full = fish.build_short_rules_block(self.conn, project="PROJA",
                                            max_tokens=10 ** 6)
        self.assertEqual(len(full["codes"]), 3, "夹具前提：不受限时三条都该在")
        cap = fish.count_tokens(full["text"]) // 2   # 砍到一半：必然要少列条目
        block = fish.build_short_rules_block(self.conn, project="PROJA", max_tokens=cap)
        self.assertIn("规矩截断", block["trimmed"], "超段上限必须留裁剪标记")
        self.assertLessEqual(fish.count_tokens(block["text"]), cap,
                             "段文本必须真的落在上限内")
        self.assertLess(len(block["codes"]), 3, "超上限要少列条目，不是截断标题")
        self.assertGreater(len(block["codes"]), 0,
                           "夹具失效：一条都没留下，这条判据就证明不了「少列而非截断」")
        self.assertIn(title, block["text"], "留下的那条标题必须完整（不截断）")

    # ------------------------------------------------------------ J（附加）
    def test_J_rules_not_duplicated_across_sections(self):
        """J：同一条编码不在两栏各出现一次（本项目栏 / 全局栏谓词互斥）。"""
        self._mem("PROJA-261024-01", "本项目规矩一", mem_type="rule",
                  recorded_at=_ago(6))
        self._mem("GBTEST-261024-01", "全局规矩一", project=None, mem_type="rule",
                  recorded_at=_ago(5))
        pack, meta = self._pack()
        codes = self._section_codes(pack, RULES_HEADERS)
        self.assertEqual(len(codes), len(set(codes)), f"同一条规矩被列了两次：{codes}")
        self.assertEqual(sorted(codes), ["GBTEST-261024-01", "PROJA-261024-01"])
        self.assertEqual(sorted(meta["rules_codes"]), sorted(codes))

    # ------------------------------------------------------------ K（附加）
    def test_K_assembly_order(self):
        """K：装配次序 L0 → L1 → ③规矩 → ④该注意什么 → ⑤错题本 → L2。

        2026-10-25（T9）：错题本独立成段后，次序断言补上段⑤——**只增不减**：
        原来只钉 rules < attention < L2，现在把 lesson 夹进中间，保证它没有被
        错插到规矩之前（那会让"长期规范"让位给"临时教训"，与裁剪顺序矛盾）。
        """
        self._seed_full()
        pack, _ = self._pack()
        i_l1 = pack.index("当前项目：")
        i_rules = pack.index(PROJECT_HEADER)
        i_attn = pack.index(ATTENTION_HEADER)
        i_lesson = pack.index(LESSON_HEADER)
        i_l2 = pack.index("【操作指引】")
        self.assertLess(i_l1, i_rules, "规矩段应在 L1 状态之后")
        self.assertLess(i_rules, i_attn, "注意段应在规矩段之后")
        self.assertLess(i_attn, i_lesson, "错题本应在注意段之后")
        self.assertLess(i_lesson, i_l2, "L2 索引应在最后")


    # ------------------------------------------------------------ L（T9 新增）
    def test_L1_lesson_block_empty_when_no_lessons(self):
        """L1：没有 lesson 时段⑤整段不出现（不写"暂无错题"这种噪音）。"""
        self._mem("PROJA-261024-01", "一条普通事实", recorded_at=_ago(5))
        pack, meta = self._pack()
        self.assertNotIn(LESSON_HEADER, pack, "没有 lesson 却出现了「错题本」段")
        self.assertEqual(meta["lesson_codes"], [])

    def test_L2_lessons_no_longer_in_attention_section(self):
        """L2：lesson 不得再出现在段④里（是**搬走**，不是复制一份）。"""
        self._mem("PROJA-261024-01", "教训一", mem_type="lesson", recorded_at=_ago(5))
        pack, _ = self._pack()
        attn_lines = self._section_lines(pack, (ATTENTION_HEADER,))
        self.assertFalse(any("教训一" in ln for ln in attn_lines),
                         f"lesson 仍留在段④：{attn_lines}")
        self.assertIn("教训一", "\n".join(self._section_lines(pack, (LESSON_HEADER,))),
                      "lesson 没有出现在段⑤")

    def test_L3_same_code_never_in_two_sections(self):
        """L3：同一条编码不在段④与段⑤各出现一次（`exclude` 真的生效）。

        夹具刻意让 lesson 的标题命中「未闭合」词表——它会被 A 类先抢走，
        段⑤再拿到它，同一条记忆就占了两段预算。这正是 exclude 要防的情况。
        """
        self._mem("PROJA-261024-01", "下一步：把错题本钉进判据", mem_type="lesson",
                  recorded_at=_ago(5))
        pack, _ = self._pack()
        attn = self._section_codes(pack, (ATTENTION_HEADER,))
        les = self._section_codes(pack, (LESSON_HEADER,))
        self.assertEqual(set(attn) & set(les), set(),
                         f"同一编码同时出现在段④与段⑤：{attn} / {les}")

    def test_L4_lesson_block_excludes_given_codes(self):
        """L4：`build_lesson_block(exclude=…)` 必须真的排除。"""
        self._mem("PROJA-261024-01", "教训甲", mem_type="lesson", recorded_at=_ago(5))
        self._mem("PROJA-261024-02", "教训乙", mem_type="lesson", recorded_at=_ago(4))
        blk = fish.build_lesson_block(self.conn, project="PROJA",
                                      exclude={"PROJA-261024-01"})
        self.assertNotIn("PROJA-261024-01", blk["codes"], "exclude 没生效")
        self.assertIn("PROJA-261024-02", blk["codes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
