#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T03b 判据：推送合流（六特征重排 + 三类语义统一 + 断路器 · 34 §3.3/§3.4/§3.5）。

| # | 判据 | 怎么变红 |
|---|---|---|
| **R1** | 只对 first-phase 的 top-K 重排 | 100 候选 / top_k=50 → 只有 50 条 `score_stage=rerank`（M8 全量重排 → 红） |
| **R1b** | `top_k` 上下界：`<1 → 1`、`>500 → 500`、解析失败 → 50 | 上界写坏成 10⁹（M12）→ 红；下限写成 0 条 → 红 |
| **R2** | 权重从 config 读 | 改 `push.rerank_weights` → 排序真的翻转 |
| **R3** | `score_breakdown` 分项和 == `score`（1e-6）+ **两条独立手算 oracle** | 公式漏项/夹紧 → 红；**一致地**篡改 breakdown+score（M15）→ 红 |
| **R4** | 重复惩罚生效（负向特征是活的） | 24h 推 5 次 → 分低 0.30；权重 0 → 不罚（M9b → 红） |
| **R5** | 同查询重排前后 top-3 有变化 | 重排没接进选择器 → 红 |
| **R6** | hook 预算：决策路径 < 2s；**rerank on/off 配对增量 ≤ 0.2s**；hook 子进程只记分布 | 断路器失效 → R6c 红（M9a）；vault 全遍历回潮 → 2s 红；rerank 变重 → 配对增量红 |
| **R7** | mem / kb / corpus 三类 `kind_class` 各 ≥1，`kind` 一字不改 | 语义归一失效 → 红 |
| **R8** | `link_strength` 真值：双向边/权重/归一/`suppressed`+`removed_at` 过滤/别名锚点 | 读数恒 0（M10）→ 红 |
| **R9** | `project_match` 三类不同源：mem 1/0.5/0、kb/corpus 恒 0.5 中性、项目未知 0.5 | 非记忆类恒 1.0（M16）→ 红；别名恒 None（M11）→ 红 |
| **R10** | `_infer_project`：`_ctx_project` 优先、只认 mem 且**只认唯一值**、都没有 → None | 恒 None（M14）→ 红；改回多数票 → 红 |
| **R11** | 断路器 `<=0` = 关闭（0 与负数都不切） | `<=` 改 `<`（M13）→ 红 |
| **R12** | prompt 池跨项目时 `project_match` 不把跨项目 mem 判成 1.0；`now` 不采信候选里的旧值 | 用池内众数当当前项目 → 红 |
| **R12d** | `project=None` + **3 跨 + 1 同** → 四条全 0.5；**单项目池** → 唯一值 1.0 | 回退成"池内多数票"（M14 第三段）→ 红 |
| **S3** | config 坏值（`rerank_top_k="abc"` / `rerank_weights` 坏 JSON / `breaker_seconds="x"` / `now` 非 ISO）→ **回落默认 + 一条诊断**，不抛、不穿到注入包 | 把坏值抛出去或静默吞掉 → 红 |
| **R16** | 权重里 `NaN`/`Infinity`/`"nan"`/`|w|>10` → 默认权重 + 诊断，**候选池不许被筛空**；总分非有限 → 该候选 `score=0`（不入选）并计数 | 让 NaN 流进 `score` → 整池 `score>0` 全灭（注入包静默变空）→ 红 |
| **RB** | **已登记**的键写坏值（int 抛 `ValueError` / string 原样交出去 / bool 静默成 False）→ 默认 + 诊断 | 把 `get_config` 的结果直接当值用 → 红 |
| **R13** | 生成器入参在 `rerank=True` 下不被吃光（QA F-1） | 改回 `_tag_kind_class(cands)` 不赋值 → 红 |
| **R14** | 真语料库（只读副本）能产 `conv:` 候选（判不到则 SKIP 并说明） | 语料链路断 → 红 |
| 兼容 | `rerank_enabled=False` 时 select **逐字节等价** | 关闭态改了行为/字段 → 红 |

三条纪律（与 T03a 的判据同一套）：

1. **不许恒绿**：每条断言都能说清"改坏了会怎样红"（对应的变异体在
   `mutation_push_rerank.py`，跑法见它的文件头）；
2. **判据要量到真正被递出去的东西**：R6a2 跑的是真 hook 进程（`fish_prompt_hook.py`），
   不是"调一下函数看看返回值"；
3. **不碰真数据**：所有库都在 `tempfile` 根下，`data/` 只读（R6b 复制一份到临时根，
   原目录一行不改）、`E:\\Obsidian` 只读。

R3 为什么还要**两条手算 oracle**：若 `score := combine_features(breakdown)`，"分项和
== 总分"就是恒真复述（`fish_corpus_rank.combine_features` 的 docstring 已警告过）。
首轮只有一个点（0.75，六项里钉住四项缺省值），**一致地**篡改 breakdown+score 仍绿
（QA F-2）。所以第二轮加一条"六项全非缺省"的组合 oracle（0.625 / 0.22，手算写在
`OracleCompositeTest` 的 docstring 里），它同时钉住 relevance 归一、freshness、access、
project、link、dup 六条读数——任何一条被"顺手改对"都会让这两个数对不上。
"""
import datetime
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import zstandard

P0 = Path(__file__).resolve().parent
if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))

import fish  # noqa: E402
import fish_common  # noqa: E402
import fish_corpus  # noqa: E402
import fish_corpus_rank  # noqa: E402
import fish_corpus_store as store  # noqa: E402
import fish_push  # noqa: E402
import fish_push_rerank  # noqa: E402

from fish_common import _utc_str  # noqa: E402

HOOK = P0 / "fish_prompt_hook.py"
CID = "01CITIZEN0000000RERANK1"
PROJECT = "TESTA"
OTHER = "TESTB"
MEM_KINDS = {"rule", "association", "serendipity", "situational", "prompt_related"}
QUERY = "撞车判据怎么做"
NEUTRAL = fish_push_rerank.PROJECT_NEUTRAL
# 生产树（只读）：真语料库/真 vault 的所在。worktree 与生产树在 `learn/` 下同级，
# 所以 `P0.parents[1]` 两边都指到 `learn`（worktree 缺 corpus.sqlite，正是 QA D-3）。
# 隔离变异副本（`mutation_push_rerank.py` 的 mkdtemp 树）用 `FISH_PROD_P0` 指回生产树：
# 副本只含 `.py`，`data/` 按纪律排除，不指回来 R14 会退化成 skip（判不到 ≠ 通过）。
PRODUCTION_P0 = Path(os.environ.get("FISH_PROD_P0")
                     or (P0.parents[1] / "大肥鱼生命系统" / "p0"))

MS = 1790918381967
SEG_TEXT = ("撞车判据的唯一真值是 overlap：先比目录前缀、再比文件后缀，同一份声明窗口内"
            "出现两条就算撞车。判定不看谁先签到，也不看谁嗓门大——这条结论在历史会话里"
            "已经反复确认过，长度足够过 min_chars。")


def _frame(payload) -> bytes:
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return zstandard.ZstdCompressor().compress(blob + b"\n")


def _ev_session(sid, cwd="I:/app/push-proj"):
    return {"type": "session", "version": 4, "id": sid, "createdAt": MS, "cwd": cwd}


def _ev_user(seq, text):
    return {"type": "user/message", "seq": seq, "time": MS + seq * 10,
            "data": {"content": [{"type": "text", "text": text}],
                     "source": {"kind": "user"}, "role": "user", "id": "u%d" % seq}}


def _ev_assistant(seq, text):
    return {"type": "assistant/message", "seq": seq, "time": MS + seq * 10,
            "data": {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [
                {"type": "reasoning", "text": "不该入库"},
                {"type": "text", "text": text}], "id": "a%d" % seq}}}


class RerankCase(unittest.TestCase):
    """主库在临时根里；语料库（R7 才用）是同一根下的另一个文件。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-rerank-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)", (CID, "重排判据居民", "resident", "owner", None,
                                      "active", fish.now_utc()))
        for prefix in (PROJECT, OTHER, "PJ"):
            self.conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,NULL,0,'active')", (prefix, prefix + " 项目"))
        self.conn.commit()
        self.vault_dir = self.tmp / "vault"
        self.vault_dir.mkdir()
        self.n = 0
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 夹具
    def _mem(self, project=PROJECT, title="判据条目", mem_type="fact"):
        self.n += 1
        code = "%s-261027-%02d" % (project, self.n)
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared','active','[]',NULL,?,?,0,0)",
            (code, title, project, CID, mem_type, fish.now_utc(), fish.now_utc()))
        self.conn.commit()
        return code

    def _cand(self, code, score, *, project=PROJECT, freshness=None):
        """一条 first-phase 候选（字段形态与 `_cand` 一致的最小集）。"""
        cand = {"code": code, "kind": "association", "title": code, "reason": "判据夹具",
                "project": project, "benefit": score, "cost": 0.0, "weight": 1.0,
                "key": False, "score": score}
        if freshness is not None:
            cand["freshness"] = {"factor": freshness, "band": "fresh", "age_days": 1.0}
        return cand

    def _access(self, code, times=1):
        for _ in range(int(times)):
            self.conn.execute(
                "INSERT INTO memory_access_log (code,citizen_id,session_key,project,via,"
                "accessed_at) VALUES (?,?,?,?,?,?)",
                (code, CID, "dsh:test", PROJECT, "recall", fish.now_utc()))
        self.conn.commit()

    def _push(self, code, times=1):
        for _ in range(int(times)):
            self.conn.execute(
                "INSERT INTO push_log (session_key,citizen_id,project,code,channel,"
                "budget_tokens,pushed_at) VALUES (?,?,?,?,?,?,?)",
                ("dsh:test", CID, PROJECT, code, "tool_after", 1000, fish.now_utc()))
        self.conn.commit()

    def _push_at(self, code, when):
        """在**指定时刻**写一条推送记录（dup 窗口的边界判据要它）。"""
        self.conn.execute(
            "INSERT INTO push_log (session_key,citizen_id,project,code,channel,"
            "budget_tokens,pushed_at) VALUES (?,?,?,?,?,?,?)",
            ("dsh:test", CID, PROJECT, code, "tool_after", 1000,
             _utc_str(when) if isinstance(when, datetime.datetime) else str(when)))
        self.conn.commit()

    def _index(self, code, project=PROJECT, *, status="active", dormant=0):
        """造一行 `code_index`（links 锚点查询要它：`project`/`status`/`dormant` 都被筛）。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO code_index (code,title,project,owner,mem_type,"
            "visibility,status,tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,'fact','shared',?,'[]',NULL,?,?,0,?)",
            (code, code, project, CID, status, fish.now_utc(), fish.now_utc(), int(dormant)))
        self.conn.commit()

    def _link(self, src, dst, weight=1.0, *, suppressed=0, removed_at=None):
        """造一条 links 边（`rel` 固定，UNIQUE(src,dst,rel) 靠 REPLACE 覆写）。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO links (src,dst,rel,source,weight,created_at,removed_at,"
            "suppressed) VALUES (?,?,?,?,?,?,?,?)",
            (src, dst, "rel", "test", float(weight), fish.now_utc(), removed_at,
             int(suppressed)))
        self.conn.commit()

    def _alias(self, alias, canonical, kind="project"):
        self.conn.execute(
            "INSERT OR REPLACE INTO name_aliases (kind,from_name,to_name,created_at,"
            "created_by,reason) VALUES (?,?,?,?,?,?)",
            (kind, alias, canonical, fish.now_utc(), CID, "T03b 判据"))
        self.conn.commit()

    def _cfg(self, key, value):
        """写 config 表（这些键可能就是**未登记**的：`_cfg_any` 的退路就是为它写的）。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO config (key,value,type,updated_at) VALUES (?,?,?,?)",
            (key, str(value), "string", fish.now_utc()))
        self.conn.commit()

    def _enable_push(self, *, rerank=True):
        self._cfg("push.enabled", "true")
        self._cfg("push.prompt_enabled", "true")
        self._cfg("push.prompt_max_items", "2")
        self._cfg("push.rerank_enabled", "true" if rerank else "false")

    def _register_vault(self, name="撞车判据.md", body="# 撞车判据\n\n撞车判据的唯一真值是 "
                                                  "overlap。\n"):
        (self.vault_dir / name).write_text(body, encoding="utf-8")
        return fish.kb_vault_add(self.conn, name="判据库", path=str(self.vault_dir), mode="ro")


class TopKTest(RerankCase):
    """R1：只对 top-K 打分（尾部原样，且入参不被改）。"""

    def test_R1_only_top_k_is_reranked(self):
        cands = [self._cand("%s-261027-%02d" % (PROJECT, i), round(1.0 - i / 100.0, 4))
                 for i in range(100)]
        snapshot = {c["code"]: c["score"] for c in cands}
        tail_codes = [c["code"] for c in cands[50:]]
        for code in tail_codes:
            self._push(code, times=5)          # 尾部若被偷偷重排，这 5 次会把它压下去
            self._access(code, times=9)
        out = fish_push.rerank_candidates(self.conn, cands, top_k=50, now=fish.now_utc())
        self.assertEqual(len(out), 100, "尾部必须原样保留（不是丢掉）")
        reranked = [c for c in out if c.get("score_stage") == "rerank"]
        self.assertEqual(len(reranked), 50, "只许 top-K 被重排")
        self.assertEqual({c["code"] for c in reranked},
                         {c["code"] for c in cands[:50]}, "重排的必须是首阶段分最高的那 50 条")
        tail_out = {c["code"]: c for c in out if c.get("score_stage") is None}
        self.assertEqual(set(tail_out), set(tail_codes))
        for code in tail_codes:
            self.assertEqual(tail_out[code]["score"], snapshot[code], "尾部分数不许动")
            self.assertNotIn("score_breakdown", tail_out[code])
        self.assertTrue(all("score_breakdown" not in c and "score_stage" not in c
                            for c in cands), "重排不许改入参字典（首阶段读数要留档）")

    def test_top_k_comes_from_config(self):
        self._cfg("push.rerank_top_k", "2")
        cands = [self._cand("%s-261027-%02d" % (PROJECT, i), 0.9 - i / 10.0) for i in range(5)]
        out = fish_push.select_push_candidates(self.conn, cands, limit=5, rerank=True)
        self.assertEqual(len([c for c in out if c.get("score_stage") == "rerank"]), 2,
                         "push.rerank_top_k 必须控制只算几条")


class WeightsTest(RerankCase):
    """R2：权重从 config 读（改配置 → 结果真的变）。"""

    def test_R2_weights_come_from_config(self):
        hot = self._cand("%s-261027-01" % PROJECT, 0.90, project=OTHER, freshness=0.05)
        fresh = self._cand("%s-261027-02" % PROJECT, 0.50, project=PROJECT, freshness=1.0)
        before = fish_push.rerank_candidates(self.conn, [dict(hot), dict(fresh)],
                                             now=fish.now_utc(), project=PROJECT)
        self.assertEqual(before[0]["code"], hot["code"], "默认权重重 relevance")
        self.assertAlmostEqual(before[0]["score"], 0.45 + 0.15 * 0.05, delta=1e-6)
        self._cfg("push.rerank_weights", json.dumps(
            {"relevance": 0.0, "freshness": 0.6, "access_ratio": 0.0,
             "project_match": 0.6, "link_strength": 0.0, "dup_penalty": 0.0}))
        after = fish_push.rerank_candidates(self.conn, [dict(hot), dict(fresh)],
                                           now=fish.now_utc(), project=PROJECT)
        self.assertEqual(after[0]["code"], fresh["code"], "权重改了排序必须跟着变")
        self.assertAlmostEqual(after[0]["score"], 1.2, delta=1e-6)


class BreakdownTest(RerankCase):
    """R3：分项和 == 总分（1e-6）+ 手算 oracle（防"恒真复述"）。"""

    def test_R3_breakdown_sums_to_score(self):
        self._mem(PROJECT, "唯一候选")
        out = fish_push.rerank_candidates(
            self.conn, [self._cand("%s-261027-01" % PROJECT, 0.5)],
            now=fish.now_utc(), project=PROJECT)
        got = out[0]
        breakdown = got["score_breakdown"]
        self.assertEqual(set(breakdown), set(fish_corpus_rank.FEATURE_NAMES))
        weights = fish_corpus_rank.FEATURE_WEIGHTS
        manual = sum(weights[name] * breakdown[name] for name in fish_corpus_rank.FEATURE_NAMES)
        self.assertAlmostEqual(manual, got["score"], delta=1e-6, msg="分项和必须等于总分")
        # 手算 oracle：单条候选 → relevance 退化 1.0；无 freshness → 1.0；同项目 → 1.0
        self.assertAlmostEqual(breakdown["relevance"], 1.0, delta=1e-9)
        self.assertAlmostEqual(breakdown["project_match"], 1.0, delta=1e-9)
        self.assertAlmostEqual(got["score"], 0.75, delta=1e-9)
        self.assertEqual(got["first_phase_score"], 0.5)
        self.assertEqual(got["score_stage"], fish_corpus_rank.SCORE_STAGE_RERANK)
        self.assertEqual(got["rerank"], "push_six_features")


class DupPenaltyTest(RerankCase):
    """R4：重复惩罚（负向特征是活的；权重 0 → 不罚）。"""

    def test_R4_dup_penalty_lowers_score(self):
        dup = self._cand("%s-261027-10" % PROJECT, 0.6)
        clean = self._cand("%s-261027-11" % PROJECT, 0.6)
        self._push(dup["code"], times=5)
        out = {c["code"]: c for c in fish_push.rerank_candidates(
            self.conn, [dict(dup), dict(clean)], now=fish.now_utc(), project=PROJECT)}
        self.assertAlmostEqual(out[dup["code"]]["score_breakdown"]["dup_penalty"], 1.0,
                               delta=1e-9)
        self.assertAlmostEqual(out[clean["code"]]["score_breakdown"]["dup_penalty"], 0.0,
                               delta=1e-9)
        self.assertAlmostEqual(out[clean["code"]]["score"] - out[dup["code"]]["score"], 0.30,
                               delta=1e-6, msg="5 次重复必须扣满 0.30")
        self._cfg("push.rerank_weights", json.dumps({"dup_penalty": 0.0}))
        zero = {c["code"]: c for c in fish_push.rerank_candidates(
            self.conn, [dict(dup), dict(clean)], now=fish.now_utc(), project=PROJECT)}
        self.assertAlmostEqual(zero[dup["code"]]["score"], zero[clean["code"]]["score"],
                               delta=1e-9, msg="权重 0 → 重复不再罚")


class OrderChangeTest(RerankCase):
    """R5：同一批候选，重排前后 top-3 有变化（说明它真的接进了选择器）。"""

    def test_R5_top3_changes_after_rerank(self):
        a = self._cand("%s-261027-21" % PROJECT, 0.90, project=OTHER, freshness=0.1)
        b = self._cand("%s-261027-22" % PROJECT, 0.80, project=OTHER, freshness=0.1)
        c = self._cand("%s-261027-23" % PROJECT, 0.70, project=PROJECT, freshness=1.0)
        d = self._cand("%s-261027-24" % PROJECT, 0.60, project=OTHER, freshness=0.1)
        self._access(c["code"], times=3)
        pool = [dict(a), dict(b), dict(c), dict(d)]
        before = [x["code"] for x in sorted(pool, key=lambda x: (-x["score"], x["code"]))]
        after = [x["code"] for x in fish_push.select_push_candidates(
            self.conn, [dict(x) for x in pool], limit=3, rerank=True, project=PROJECT)]
        self.assertEqual(after[0], c["code"], "同项目 + 新鲜 + 被读过的那条应升到第一")
        self.assertNotEqual(before[:3], after[:3], "重排前后 top-3 必须变化")
        self.assertGreater(len(set(before) & set(after)), 0, "变化要来自重排，不是丢候选")


class HookBudgetTest(RerankCase):
    """R6：hook 预算（决策路径 <2s + rerank 配对增量 ≤0.2s）+ 断路器切断慢组件（R6c）。

    ## 2s 这条线量的是"推送工作量"，不是解释器启动

    本机实测（2026-10-27，三次独立读数：实现者/评审/QA）：

      · `python -c pass`（一次解释器启动）    **0.104–0.174s**
      · `import fish`（748KB 门面 + 依赖）     **0.348–0.508s**
      · hook 侧 `import hook_io, fish_activity` **0.352s**
      · `connect + executescript(SCHEMA) + migrate`（真库）**0.011s**（不是 0.6–0.9s！）
      · 决策路径（真 vault，含 kb 检索）       **0.39–0.70s**

    ⚠️ 首轮交付文档把大头记成"`executescript+migrate` ≈ 0.6–0.9s"——**实测 0.011s**，
    该归因不成立（QA D-2 / 评审 W8 已订正）：真正的固定成本是**两次解释器启动 +
    `import fish` + hook 依赖导入**，与 T03b 无关（rerank 计算本身 0.5–0.95 ms）。

    因此判据分两段：

      · R6a/R6b 量**推送决策路径**（`build_prompt_candidates` + `select_push_candidates`）
        —— 这正是 hook 等 `fish prompt-push` 的那段工作，也是 P0-1 事故（vault 全遍历
        三次 ≈ 2.7s，顶到 10s 超时线）真正出问题的地方，硬线 **2s**；
      · R6d 量 **rerank on/off 的配对增量**（同 DB 同查询、N=3 取中位）——这才是 T03b
        能负责的部分，硬线 **0.2s**；
      · R6a2 把整条 hook 子进程跑 **N=10** 次，只**打印分布**（min/中位/max、<2s 计数），
        **不设墙钟硬线**：绝对值随缓存/负载/杀软漂移（本机 1.23–3.94s），拿它当判据会
        变成"跑一次绿一次"的假绿。

    R6b/R6c 为什么必须有：断路器是**后置**判定（跑完才知道超时），它省不了这次墙钟
    ——所以"R6a 跑得快"证明不了断路器存在。R6b/R6c 直接钉"超时 → 候选为 0 + 诊断
    落盘"，M9a（把断路器摘掉）会立刻红。
    """

    def test_R6a_push_path_under_2s(self):
        mem = self._mem(PROJECT, "撞车判据怎么做")
        self._enable_push()
        now = datetime.datetime.now(datetime.timezone.utc)
        started = time.perf_counter()
        cands = fish.build_prompt_candidates(
            self.conn, self.root, prompt="%s %s" % (mem, QUERY), citizen_id=CID,
            session_key="dsh:r6", project=PROJECT, now=now, limit=2)
        picked = fish.select_push_candidates(self.conn, cands, limit=2,
                                            prefer_kind="prompt_related", project=PROJECT,
                                            now=now)
        elapsed = time.perf_counter() - started
        print("[R6a] 推送决策路径 %.3fs（合成夹具）" % elapsed)
        self.assertIn(mem, [c["code"] for c in picked], "这条记忆必须真的被选中")
        self.assertLess(elapsed, 2.0, "推送决策路径预算 2s")

    def test_R6a2_hook_subprocess_distribution(self):
        """整条 hook（含两次解释器启动）跑 N=10 次：**只记分布，不设墙钟硬线**。

        断言只有两件事：每次 rc=0、每次真的递出了那条记忆（`assertIn`）。墙钟读数
        （min/中位/max、<2s 计数）打印出来交给报告——它是环境读数，不是 T03b 的判据。
        """
        mem = self._mem(PROJECT, "撞车判据怎么做")
        self._enable_push()
        payload = {"cwd": "I:/app/push-proj",
                   "prompt": "%s %s" % (mem, QUERY)}       # 带编码 → 记忆候选必然命中
        env = dict(os.environ, PYTHONIOENCODING="utf-8", FISH_DATA=str(self.root))
        samples = []
        for index in range(10):
            # 每次换一个 session_id：24h 去重是**按会话**的，同一个会话跑第二遍会
            # 因为"刚推过"而不递（那是正确行为，不是钩子坏了）。
            payload["session_id"] = "3931d23f-17c9-4de8-87b0-d7b782e70c%02d" % index
            started = time.perf_counter()
            proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", cwd=str(P0), env=env, timeout=120)
            samples.append(time.perf_counter() - started)
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
            self.assertIn(mem, proc.stdout,
                          "hook 必须真的递出那条记忆：rc=%d %r %r"
                          % (proc.returncode, proc.stdout[:400], proc.stderr[-400:]))
        print("[R6a2] hook 子进程墙钟 N=%d min %.3fs / 中位 %.3fs / max %.3fs；<2s %d/%d"
              % (len(samples), min(samples), statistics.median(samples), max(samples),
                 sum(1 for v in samples if v < 2.0), len(samples)))

    def test_R6d_rerank_pair_increment_within_budget(self):
        """R6 第二段：同一 DB、同一查询，`rerank on/off` 的**决策路径配对增量** ≤ 0.2s。

        为什么只守增量（评审 §3 的条件式接受）：绝对值含两次解释器启动 + `import fish`
        + hook 依赖导入，与 T03b 无关；T03b 真正能负责的是"打开重排多花多少"。
        N=3 对（off/on 交替，抵消顺序与缓存漂移），取中位差。
        """
        real = P0 / "data" / "index.sqlite"
        if not real.exists():
            self.skipTest("真库 data/index.sqlite 不存在：配对增量判不到，不是通过")
        tmp = Path(tempfile.mkdtemp(prefix="fish-rerank-pair-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "data"
        root.mkdir(parents=True)
        shutil.copy2(real, root / "index.sqlite")
        conn = fish.connect(root)
        self.addCleanup(conn.close)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.commit()
        deltas = []
        for index in range(3):
            spent = {}
            order = (False, True) if index % 2 == 0 else (True, False)
            for rerank in order:
                now = datetime.datetime.now(datetime.timezone.utc)
                started = time.perf_counter()
                cands = fish.build_prompt_candidates(
                    conn, root, prompt="海报排版怎么做 %d" % index, citizen_id=None,
                    session_key="dsh:r6d-%d-%s" % (index, rerank), project=None, now=now,
                    limit=2)
                fish.select_push_candidates(conn, cands, limit=2, rerank=rerank, now=now)
                spent[rerank] = time.perf_counter() - started
            deltas.append(spent[True] - spent[False])
        median = statistics.median(deltas)
        print("[R6d] rerank on/off 决策路径配对增量 中位 %+.3fs（样本 %r）"
              % (median, [round(d, 3) for d in deltas]))
        self.assertLessEqual(median, 0.2,
                             "打开重排的增量必须留在 0.2s 内（T03b 只负责这一段）")

    def test_R6b_real_vault_push_path_under_2s(self):
        """真 vault（`data/index.sqlite` 的副本 + `E:\\Obsidian` 只读）上的决策路径 <2s。

        判不到就 skip 并写明理由（不是通过）。**只读**：真库复制到临时根，原目录一行不改。
        """
        real = P0 / "data" / "index.sqlite"
        if not real.exists():
            self.skipTest("真库 data/index.sqlite 不存在：真 vault 判不到，不是通过")
        tmp = Path(tempfile.mkdtemp(prefix="fish-rerank-real-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "data"
        root.mkdir(parents=True)
        shutil.copy2(real, root / "index.sqlite")
        conn = fish.connect(root)
        self.addCleanup(conn.close)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.commit()
        had_vault = conn.execute(
            "SELECT COUNT(*) FROM kb_vault").fetchone()[0]
        if not had_vault:
            self.skipTest("真库没有登记 vault：真 vault 判不到，不是通过")
        rows = []
        for index, rerank in enumerate((True, False)):
            now = datetime.datetime.now(datetime.timezone.utc)
            started = time.perf_counter()
            cands = fish.build_prompt_candidates(
                conn, root, prompt="海报排版怎么做 %d" % index, citizen_id=None,
                session_key="dsh:r6-real-%d" % index, project=None, now=now, limit=2)
            fish.select_push_candidates(conn, cands, limit=2, rerank=rerank, now=now)
            rows.append((rerank, time.perf_counter() - started, len(cands)))
        for rerank, elapsed, count in rows:
            print("[R6b] 真 vault 决策路径 rerank=%s %.3fs（候选 %d）"
                  % (rerank, elapsed, count))
            self.assertLess(elapsed, 2.0, "真 vault 上的推送决策路径预算 2s")

    def test_R6c_breaker_cuts_slow_component(self):
        self._register_vault()
        self._cfg("push.breaker_seconds", "0.05")
        real = fish_push._kb_search_multi

        def slow(*args, **kwargs):
            time.sleep(0.2)
            return real(*args, **kwargs)

        with mock.patch.object(fish_push, "_kb_search_multi", slow):
            out = fish_push._kb_push_candidates(self.conn, self.root, query="撞车判据",
                                                project=PROJECT, limit=2)
        self.assertEqual(out, [], "超过 push.breaker_seconds 必须本次不推")
        log = self.root / "_audit" / "push_breaker.jsonl"
        self.assertTrue(log.exists(), "断路器必须落诊断")
        text = log.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"ts", "component", "seconds", "query_hash"})
        self.assertEqual(rows[0]["component"], "kb")
        self.assertGreaterEqual(rows[0]["seconds"], 0.05)
        self.assertNotIn("撞车判据", text, "诊断不许落原文")

    def test_R6c_breaker_also_covers_corpus(self):
        self._cfg("push.breaker_seconds", "0.05")
        real = fish_corpus.conv_push_for_session

        def slow(**kwargs):
            time.sleep(0.2)
            return real(**kwargs)

        with mock.patch.object(fish_corpus, "conv_push_for_session", slow):
            out = fish_push._conv_push_candidates(self.conn, self.root, query=QUERY,
                                                  project=PROJECT, session_key="dsh:test")
        self.assertEqual(out, [], "语料侧同样受断路器管")
        rows = [json.loads(line) for line in
                (self.root / "_audit" / "push_breaker.jsonl").read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([r["component"] for r in rows], ["conv"])


class SemanticUnionTest(RerankCase):
    """R7：三类候选同池，`kind_class` 齐全、`kind` 一字不改。"""

    def _ingest_corpus(self):
        sessions = self.tmp / "sessions"
        sid = "3931d23f-17c9-4de8-87b0-d7b782e70cf0"
        folder = sessions / "--I-app-push-proj--" / sid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "session.v4.jsonl.zstd").write_bytes(b"".join(
            _frame(ev) for ev in (_ev_session(sid), _ev_user(9, "撞车判据怎么做"),
                                  _ev_assistant(10, SEG_TEXT))))
        corpus_conn = store.open_db(self.root)
        self.addCleanup(corpus_conn.close)
        fish_corpus.ingest(("dsh",), root=self.root, conn=corpus_conn,
                           roots={"dsh": [sessions]}, use_lock=False,
                           project_of=lambda cwd: "PJ", citizen_of=lambda key: CID)
        return "dsh:" + sid

    def test_R7_three_classes_share_kind_class(self):
        self._enable_push()
        mem = self._mem("PJ", "撞车判据怎么做")
        fish.reindex_fts(self.conn, self.root)
        self._register_vault()
        session_key = self._ingest_corpus()
        cands = fish.build_prompt_candidates(
            self.conn, self.root, prompt=QUERY, citizen_id=CID, session_key=session_key,
            project="PJ", limit=3)
        classes = {fish_corpus_rank.semantic_kind(c) for c in cands}
        self.assertEqual(classes, {"mem", "kb", "corpus"},
                         "三类候选必须同池：%r" % [c["code"] for c in cands])
        picked = fish.select_push_candidates(self.conn, cands, limit=3, rerank=True,
                                             project="PJ")
        self.assertEqual({c["kind_class"] for c in picked}, {"mem", "kb", "corpus"})
        by_class = {c["kind_class"]: c for c in picked}
        self.assertEqual(by_class["mem"]["code"], mem)
        self.assertEqual(by_class["kb"]["kind"], "kb")
        self.assertEqual(by_class["corpus"]["kind"], "conv_related", "语料 kind 一字不改")
        self.assertIn(by_class["mem"]["kind"], MEM_KINDS)
        self.assertTrue(all(c["kind_class"] in fish_corpus_rank.KIND_CLASSES for c in picked))


class OracleCompositeTest(RerankCase):
    """R3b：六特征**全非缺省**的手算 oracle（防"一致地篡改 breakdown+score"，QA F-2）。

    夹具（两条候选，让 relevance 归一不退化）：

    | 候选 | 首阶段分 | 项目 | freshness | 被读 | links | 24h 推送 |
    |---|---|---|---|---|---|---|
    | `TESTB-261027-01` | 0.90 | TESTB（跨项目） | 0.50 | 0 次 | 0.80（连 TESTA 锚点） | 0 次 |
    | `TESTA-261027-01` | 0.50 | TESTA（同项目） | 1.00 | 4 次 | 无 | 3 次 |

    手算（权重 relevance .45 / freshness .15 / access .10 / project .15 / link .10 /
    dup −.30，`rank_norm` 对首阶段分取负后做 min-max → 0.90 那条 1.0、0.50 那条 0.0）：

      · 跨项目那条 = .45×1.0 + .15×0.5 + .10×0.0 + .15×0.0 + .10×1.0 − .30×0.0 = **0.625**
      · 同项目那条 = .45×0.0 + .15×1.0 + .10×1.0 + .15×1.0 + .10×0.0 − .30×0.6 = **0.22**

    这两个数只在**六条读数同时符合设计**时成立：任何一条被"顺手改对"（freshness 恒 1.0、
    link 恒 0、project 恒 1.0、dup 窗口算错、access 分母错）都会让它们对不上——而
    "分项和 == 总分"依然绿（同源恒等）。所以它是 R3 的**独立**判别力来源。
    """

    def test_R3b_composite_oracle(self):
        cross = self._cand("%s-261027-01" % OTHER, 0.90, project=OTHER, freshness=0.5)
        same = self._cand("%s-261027-01" % PROJECT, 0.50, project=PROJECT, freshness=1.0)
        anchor = "%s-261027-90" % PROJECT
        self._index(anchor)
        self._index(same["code"])
        self._link(cross["code"], anchor, 0.8)
        self._access(same["code"], times=4)
        self._push(same["code"], times=3)
        out = {c["code"]: c for c in fish_push_rerank.rerank_candidates(
            self.conn, [dict(cross), dict(same)], now=fish.now_utc(), project=PROJECT)}
        self.assertAlmostEqual(out[cross["code"]]["score"], 0.625, delta=1e-9,
                               msg="跨项目那条的手算期望是 0.625")
        self.assertAlmostEqual(out[same["code"]]["score"], 0.22, delta=1e-9,
                               msg="同项目那条的手算期望是 0.22（3 次推送 → dup 0.6）")
        self.assertEqual(out[cross["code"]]["score_breakdown"],
                         {"relevance": 1.0, "freshness": 0.5, "access_ratio": 0.0,
                          "project_match": 0.0, "link_strength": 1.0, "dup_penalty": 0.0})
        self.assertEqual(out[same["code"]]["score_breakdown"],
                         {"relevance": 0.0, "freshness": 1.0, "access_ratio": 1.0,
                          "project_match": 1.0, "link_strength": 0.0, "dup_penalty": 0.6})
        for got in out.values():
            manual = sum(fish_corpus_rank.FEATURE_WEIGHTS[name] * got["score_breakdown"][name]
                         for name in fish_corpus_rank.FEATURE_NAMES)
            self.assertAlmostEqual(manual, got["score"], delta=1e-9,
                                   msg="分项和必须等于总分（先 round 再求和）")


class TopKBoundsTest(RerankCase):
    """R1b：`top_k` 的上下界与解析失败（配置写坏时 K 仍要是 K；QA D-1 的口径统一）。

    代码口径（`fish_push_rerank.rerank_candidates` docstring）：`<1 → 1`、`>500 → 500`、
    解析失败 → 50。**文档按这个口径写**——首轮文档写"`<1` 退 50"是错的（D-1）。
    """

    def _reranked(self, pool, top_k):
        out = fish_push_rerank.rerank_candidates(
            self.conn, [dict(c) for c in pool], top_k=top_k, now=fish.now_utc(),
            project=PROJECT)
        return len([c for c in out if c.get("score_stage") == "rerank"])

    def test_R1b_top_k_bounds_and_fallback(self):
        big = [self._cand("%s-261027-%04d" % (PROJECT, i), round(1.0 - i / 1000.0, 6))
               for i in range(600)]
        small = [self._cand("%s-261027-%04d" % (PROJECT, i), round(1.0 - i / 10.0, 6))
                 for i in range(10)]
        self.assertEqual(self._reranked(big, 10 ** 9), 500,
                         "上界 500：配置写坏成 10**9 时不许退化成全量重排")
        self.assertEqual(self._reranked(small, 0), 1, "<1 夹到 1（重排 0 条无意义）")
        self.assertEqual(self._reranked(small, -5), 1, "负数同样夹到 1")
        self.assertEqual(self._reranked(small, None), 10, "解析失败退默认 50（池只有 10 条）")
        self.assertEqual(self._reranked(small, "abc"), 10, "坏字符串退默认 50")
        self.assertEqual(self._reranked(small, "7"), 7, "数字串照常按 7")


class LinkStrengthTest(RerankCase):
    """R8：`link_strength` 的真值（双向边 / 权重 / 归一 / 抑制与删除过滤 / 别名锚点）。

    首轮这条特征 **0 覆盖**：判据全文没有 `links` 字面量，把 `_link_strengths` 改成恒
    `{}` 仍全绿（评审 5 个静默变异之一）。这里把每条口径都钉住。
    """

    PROJECT_NAME = "LNK"

    def _link_fixture(self):
        anchors = ["LNK-261027-%02d" % i for i in (90, 91, 92)]
        for code in anchors:
            self._index(code, self.PROJECT_NAME)
        codes = {"strong": "LNK-261027-01", "weak": "LNK-261027-02",
                 "suppressed": "LNK-261027-03", "removed": "LNK-261027-04",
                 "none": "LNK-261027-05"}
        for code in codes.values():
            self._index(code, self.PROJECT_NAME)
        self._link(codes["strong"], anchors[0], 0.8)                  # 候选 → 锚点
        self._link(anchors[1], codes["weak"], 0.4)                    # 锚点 → 候选（反向）
        self._link(codes["suppressed"], anchors[2], 0.9, suppressed=1)
        self._link(codes["removed"], anchors[2], 0.9, removed_at=fish.now_utc())
        return anchors, codes

    def test_R8_link_strength_readings(self):
        _anchors, codes = self._link_fixture()
        batch = [codes["strong"], codes["weak"], codes["suppressed"], codes["removed"],
                 codes["none"]]
        raw = fish_push_rerank._link_strengths(self.conn, batch, self.PROJECT_NAME)
        self.assertEqual(set(raw), {codes["strong"], codes["weak"]},
                         "只有真的连到锚点的候选才有读数（抑制边/删除边/无边都不算）")
        self.assertAlmostEqual(raw[codes["strong"]], 0.8, delta=1e-9)
        self.assertAlmostEqual(raw[codes["weak"]], 0.4, delta=1e-9, msg="反向边必须算")
        out = {c["code"]: c["score_breakdown"]["link_strength"]
               for c in fish_push_rerank.rerank_candidates(
                   self.conn, [self._cand(code, 0.5, project=self.PROJECT_NAME)
                               for code in batch],
                   now=fish.now_utc(), project=self.PROJECT_NAME)}
        self.assertEqual(out[codes["strong"]], 1.0, "归一：本批最大权重和 → 1.0")
        self.assertEqual(out[codes["weak"]], 0.5, "0.4/0.8 → 0.5")
        for code in (codes["suppressed"], codes["removed"], codes["none"]):
            self.assertEqual(out[code], 0.0, "%s 不该有链接强度" % code)

    def test_R8c_alias_anchors(self):
        _anchors, codes = self._link_fixture()
        self._alias("LNK-alias", self.PROJECT_NAME)
        batch = [codes["strong"], codes["weak"]]
        by_name = fish_push_rerank._link_strengths(self.conn, batch, self.PROJECT_NAME)
        by_alias = fish_push_rerank._link_strengths(self.conn, batch, "LNK-alias")
        self.assertEqual(set(by_alias), set(by_name),
                         "别名项目的锚点必须展开（否则别名项目 link_strength 恒 0）")
        self.assertAlmostEqual(by_alias[codes["strong"]], 0.8, delta=1e-9)

    def test_R8b_real_index_links_read_only(self):
        """真库只读：`_link_strengths` 在真实 `links` 上不抛、只产正权重。

        判据不是"必须有值"（那会让它随真数据变化而假红），而是"读数合法"：键都在入参里、
        值都 > 0。真库不可用时 skip 并写明（判不到 ≠ 通过）。
        """
        real = P0 / "data" / "index.sqlite"
        if not real.exists():
            self.skipTest("真库 data/index.sqlite 不存在：真 links 判不到，不是通过")
        tmp = Path(tempfile.mkdtemp(prefix="fish-rerank-links-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "data"
        root.mkdir(parents=True)
        shutil.copy2(real, root / "index.sqlite")
        conn = fish.connect(root)
        self.addCleanup(conn.close)
        rows = conn.execute(
            "SELECT code, project FROM code_index WHERE status='active' AND dormant=0 "
            "AND project IS NOT NULL AND project<>'' LIMIT 200").fetchall()
        if not rows:
            self.skipTest("真库没有带项目的 active 记忆：判不到，不是通过")
        project = rows[0][1]
        codes = [code for code, name in rows if name == project][:50] or [rows[0][0]]
        got = fish_push_rerank._link_strengths(conn, codes, project)
        print("[R8b] 真库 links 读数：项目 %s，%d 条候选 → %d 条有边"
              % (project, len(codes), len(got)))
        self.assertIsInstance(got, dict)
        for code, value in got.items():
            self.assertIn(code, codes)
            self.assertGreater(value, 0.0, "权重和出现非正数：过滤或方向读错了")


class ProjectMatchTest(RerankCase):
    """R9：三类候选的 `project_match` **不同源**（评审 S2 + QA F-3）。

    记忆用真实归属项目（1 / 0.5 别名 / 0 跨项目）；kb / corpus 的 `project` 是**生成
    上下文**，固定 0.5 中性——恒 1.0 会让它们白拿 +0.15，再叠加 freshness 缺省 +0.15，
    相对同项目旧记忆有 ≈ +0.25 的结构性先手（假特征）。
    """

    def _pool(self):
        pool = [
            self._cand("%s-261027-01" % PROJECT, 0.95, project=PROJECT),
            self._cand("%s-261027-02" % PROJECT, 0.85, project=OTHER),
            self._cand("%s-261027-03" % PROJECT, 0.75, project="TESTA-alias"),
            self._cand("%s-261027-04" % PROJECT, 0.65, project=PROJECT),
            self._cand("%s-261027-05" % PROJECT, 0.55, project=PROJECT),
            self._cand("%s-261027-06" % PROJECT, 0.45, project=PROJECT),
            self._cand("%s-261027-07" % PROJECT, 0.35),
        ]
        pool[3].update({"kind": "kb"})
        pool[4].update({"kind": "conv_related"})
        pool[5].update({"kind": "kb", "project": OTHER})
        pool[6].update({"project": ""})
        return pool

    def test_R9_project_match_by_kind_class(self):
        self._alias("TESTA-alias", PROJECT)
        pool = self._pool()
        out = {c["code"]: c["score_breakdown"]["project_match"]
               for c in fish_push_rerank.rerank_candidates(
                   self.conn, pool, now=fish.now_utc(), project=PROJECT)}
        self.assertEqual(out[pool[0]["code"]], 1.0, "同项目记忆 → 1.0")
        self.assertEqual(out[pool[1]["code"]], 0.0, "跨项目记忆 → 0.0")
        self.assertEqual(out[pool[2]["code"]], 0.5, "同别名组 → 0.5")
        self.assertEqual(out[pool[3]["code"]], NEUTRAL,
                         "kb 的 project 是生成上下文，不许当 1.0 证据")
        self.assertEqual(out[pool[4]["code"]], NEUTRAL, "corpus 同理（0.5 中性，不是 1.0）")
        self.assertEqual(out[pool[5]["code"]], NEUTRAL, "kb 即便项目名不等于当前项目也是中性")
        self.assertEqual(out[pool[6]["code"]], 0.0, "记忆项目为空 → 0.0（rank_mod 口径）")

    def test_R9b_unknown_current_project_is_neutral(self):
        pool = [self._cand("%s-261027-08" % PROJECT, 0.5, project="")]
        out = fish_push_rerank.rerank_candidates(self.conn, pool, now=fish.now_utc())
        self.assertEqual(out[0]["score_breakdown"]["project_match"], NEUTRAL,
                         "当前项目未知 → 0.5 中性（不拿池内众数当证据）")


class InferProjectTest(RerankCase):
    """R10：`_infer_project` 的四条纪律（`_ctx_project` 优先 / 只认 mem / **只认唯一值** / 无 → None）。

    R3 收口（评审 S1 修法 a）：旧实现取"池内 mem 众数"，3 跨 + 1 同时会把**多数项目**推成
    当前项目（跨项目记忆白拿 1.0）。现在混合多项目一律 `None`（全池 0.5 中性），只有"池里
    真的只剩一个项目"才采用它——1.0 在那时是可比较的，不是猜出来的。
    """

    def test_R10_infer_project(self):
        mem_a = self._cand("%s-261027-01" % PROJECT, 0.9, project=PROJECT)
        mem_a2 = self._cand("%s-261027-02" % PROJECT, 0.8, project=PROJECT)
        mem_b = self._cand("%s-261027-03" % OTHER, 0.7, project=OTHER)
        infer = fish_push_rerank._infer_project
        self.assertEqual(infer([mem_a, mem_a2]), PROJECT, "单项目池 → 用那个唯一项目")
        self.assertIsNone(infer([mem_a, mem_a2, mem_b]),
                          "混合多项目 → None（2 票 vs 1 票也是猜，不许当当前项目）")
        kb = [dict(self._cand("%s-261027-04" % OTHER, 0.6), kind="kb", project=OTHER),
              dict(self._cand("%s-261027-05" % OTHER, 0.5), kind="kb", project=OTHER),
              dict(self._cand("%s-261027-06" % OTHER, 0.4), kind="kb", project=OTHER)]
        self.assertEqual(infer(kb + [mem_b]), OTHER,
                         "kb 的 project 不进候选集（mem 只剩 OTHER 一个 → 唯一值）")
        self.assertIsNone(infer(kb + [mem_a, mem_b]), "kb 再多也不许把混合池变成单项目池")
        tagged = [dict(cand) for cand in (mem_a, mem_a2, mem_b)]
        for cand in tagged:
            cand[fish_push_rerank.CTX_PROJECT] = OTHER
        self.assertEqual(infer(tagged), OTHER, "_ctx_project 优先于任何事情")
        self.assertIsNone(infer([]), "空池 → None")
        self.assertIsNone(infer(kb), "只有非记忆类 → None（不许拿生成上下文当项目）")

    def test_R10b_inferred_project_used_by_rerank(self):
        mem_a = self._cand("%s-261027-11" % PROJECT, 0.9, project=PROJECT)
        mem_b = self._cand("%s-261027-12" % OTHER, 0.8, project=OTHER)
        out = {c["code"]: c["score_breakdown"]["project_match"]
               for c in fish_push_rerank.rerank_candidates(
                   self.conn, [mem_a, mem_b], now=fish.now_utc())}
        self.assertEqual(set(out.values()), {NEUTRAL},
                         "混合池推不出当前项目 → 两条都 0.5（不许一边 1.0 一边 0.0）")
        only_cross = [self._cand("%s-261027-13" % OTHER, 0.7, project=OTHER)]
        out2 = fish_push_rerank.rerank_candidates(self.conn, only_cross,
                                                  now=fish.now_utc())
        self.assertEqual(out2[0]["score_breakdown"]["project_match"], 1.0,
                         "单项目池（唯一值）→ 1.0，不是 0.5")


class PromptContextTest(RerankCase):
    """R12：prompt 路径的项目/时间接线（评审 🟠1 + QA F-3/F-4 + R3 的 S1 收口）。

    评审 S1 的问题：`fish.py:9405` 的 `cmd_prompt_push` 不传 `project`（`cwd` 解析不出项目时
    更常是 `None`），而 prompt 池是**跨项目**的（`search_memories_any(project=None)`）。生成
    侧能给的是"重排开启时贴 `_ctx_project`"；贴不上时**只许中性**（R12d），不许拿池内多数票
    冒充当前项目。
    """

    def test_R12a_prompt_candidates_carry_ctx_project(self):
        mem = self._mem(PROJECT, "撞车判据怎么做")
        fish.reindex_fts(self.conn, self.root)
        self._enable_push(rerank=True)
        prompt = "%s %s" % (mem, QUERY)
        on = fish.build_prompt_candidates(
            self.conn, self.root, prompt=prompt, citizen_id=CID, session_key="dsh:ctx-on",
            project=PROJECT, limit=3)
        self.assertTrue(on, "prompt 池必须非空（否则这条判据判不到）")
        self.assertEqual({c.get(fish_push_rerank.CTX_PROJECT) for c in on}, {PROJECT},
                         "重排开启时全池都要贴当前项目")
        self._cfg("push.rerank_enabled", "false")
        off = fish.build_prompt_candidates(
            self.conn, self.root, prompt=prompt, citizen_id=CID, session_key="dsh:ctx-off",
            project=PROJECT, limit=3)
        self.assertTrue(off)
        self.assertEqual([c for c in off if fish_push_rerank.CTX_PROJECT in c], [],
                         "关闭态不许贴任何新字段（off 逐字节等价的前提）")

    def test_R12b_cross_project_mem_not_scored_as_same(self):
        others = [self._cand("%s-261027-%02d" % (OTHER, i), round(0.9 - i / 10.0, 4),
                             project=OTHER) for i in range(3)]
        own = self._cand("%s-261027-09" % PROJECT, 0.5, project=PROJECT)
        out = {c["code"]: c["score_breakdown"]["project_match"]
               for c in fish_push.select_push_candidates(
                   self.conn, others + [own], limit=4, rerank=True, project=PROJECT)}
        self.assertEqual(out[own["code"]], 1.0)
        for cand in others:
            self.assertEqual(out[cand["code"]], 0.0,
                             "跨项目 mem 不许判成 1.0（池内多数票不是当前项目）")

    def test_R12c_now_is_not_taken_from_candidates(self):
        """`now` 必须由 select 当场解析成当前 UTC，不采信候选里残留的旧 `now`。"""
        cand = self._cand("%s-261027-07" % PROJECT, 0.5)
        cand["now"] = _utc_str(datetime.datetime.now(datetime.timezone.utc)
                               - datetime.timedelta(days=2))
        self._push_at(cand["code"],
                      datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(hours=30))
        picked = fish_push.select_push_candidates(self.conn, [cand], limit=1, rerank=True,
                                                  project=PROJECT)
        self.assertEqual(len(picked), 1, "这条候选必须真的被选出来")
        self.assertEqual(picked[0]["score_breakdown"]["dup_penalty"], 0.0,
                         "30 小时前的推送不在 24h 窗口内；若采信 cand['now']（2 天前）"
                         "会把它误算成一次重复（0.2）")

    def test_R12d_unknown_project_mixed_pool_is_neutral(self):
        """R12d（R3 收口 · 评审 S1 修法 a+b）：`project=None` 时**混合池全 0.5**，唯一池 1.0。

        这是评审给的原始反例：3 条跨项目（TESTB）+ 1 条同项目（TESTA），真实 hook 在
        `cwd=learn`（未映射）时 `project=None`。旧实现用"池内 mem 众数"→ TESTB，于是
        三条跨项目记忆各拿 1.0、真正同项目的那条拿 0.0（正好反过来）。现在：推不出 →
        四条都 0.5（中性），且**任何一条都不许出现 1.0**。
        """
        others = [self._cand("%s-261027-%02d" % (OTHER, i), round(0.9 - i / 10.0, 4),
                             project=OTHER) for i in range(3)]
        own = self._cand("%s-261027-19" % PROJECT, 0.5, project=PROJECT)
        pool = others + [own]
        self.assertIsNone(fish_push_rerank._infer_project(pool),
                          "3 跨 + 1 同：混合池不许用多数票推当前项目")
        selected = fish_push.select_push_candidates(self.conn, [dict(c) for c in pool],
                                                    limit=4, rerank=True)
        self.assertEqual(len(selected), 4, "四条都必须被选出来（中性分仍 > 0）")
        readings = {c["code"]: c["score_breakdown"]["project_match"] for c in selected}
        self.assertEqual(set(readings.values()), {NEUTRAL},
                         "project=None + 混合池 → 四条全 0.5（出现了 1.0 就是又猜了）")
        only = [self._cand("%s-261027-2%d" % (OTHER, i), round(0.8 - i / 10.0, 4),
                           project=OTHER) for i in range(2)]
        single = fish_push.select_push_candidates(self.conn, [dict(c) for c in only],
                                                  limit=2, rerank=True)
        self.assertEqual({c["score_breakdown"]["project_match"] for c in single}, {1.0},
                         "单项目池仍用唯一项目（1.0）——中性不等于永远 0.5")


class BreakerOffTest(RerankCase):
    """R11：断路器 `<=0` = **关闭**（0 与负数都不切；首轮这条只写在 docstring 里）。

    变异 M13（`<=` 改 `<`）会让 0 这个取值失效——本判据就是那个探针。
    """

    def _slow_kb(self):
        real = fish_push._kb_search_multi

        def slow(*args, **kwargs):
            time.sleep(0.2)
            return real(*args, **kwargs)

        return slow

    def test_R11_breaker_zero_means_off(self):
        self._register_vault()
        for value in ("0", "-1"):
            self._cfg("push.breaker_seconds", value)
            with mock.patch.object(fish_push, "_kb_search_multi", self._slow_kb()):
                out = fish_push._kb_push_candidates(self.conn, self.root, query="撞车判据",
                                                    project=PROJECT, limit=2)
            self.assertTrue(out, "阈值 %s = 关闭：慢也要推（不是切）" % value)
            self.assertFalse((self.root / "_audit" / "push_breaker.jsonl").exists(),
                             "关闭态不许写诊断")

    def test_R11b_positive_threshold_still_cuts(self):
        self._register_vault()
        self._cfg("push.breaker_seconds", "0.05")
        with mock.patch.object(fish_push, "_kb_search_multi", self._slow_kb()):
            out = fish_push._kb_push_candidates(self.conn, self.root, query="撞车判据",
                                                project=PROJECT, limit=2)
        self.assertEqual(out, [], "正阈值照旧切断（对照组：证明 R11 不是恒绿）")


class ConfigFallbackTest(RerankCase):
    """S3（R3 收口 · 评审 🟡 按 🟠 防）：config 坏值**回落默认 + 一条诊断**，不抛、不穿到注入包。

    四种坏值各一例：`push.rerank_top_k="abc"`、`push.rerank_weights` 坏 JSON、
    `push.breaker_seconds="x"`、`now` 非 ISO 串。判据只断言"不抛 + 真的用了默认值 + 诊断里
    查得到这一条"，不断言诊断行数/顺序（`_diag` 同进程按 (键, 原值) 去重，行数随调用顺序变）。

    为什么这条按 🟠 防而不是 🟡（评审 S3）：坏值抛出去的后果不是"少一个特征"，而是
    `cmd_prompt_push` 的 `except (sqlite3.Error, ValueError)` 都不一定兜得住——
    最坏是整包注入消失，而注入包是 hook 的全部产出。
    """

    def setUp(self):
        super().setUp()
        fish_push_rerank._DIAG_SEEN.clear()      # 同进程去重：每个用例从头开始数
        self.diag_path = self.root / "_audit" / "push_config.jsonl"

    def _diag_keys(self):
        if not self.diag_path.exists():
            return []
        text = self.diag_path.read_text(encoding="utf-8")
        return [json.loads(line)["key"] for line in text.splitlines() if line.strip()]

    def _pool(self, size=5):
        return [self._cand("%s-261027-%02d" % (PROJECT, i), round(0.9 - i / 10.0, 4))
                for i in range(size)]

    def test_S3_bad_top_k_falls_back_to_default(self):
        self._cfg("push.rerank_top_k", "abc")
        self.assertEqual(fish_push_rerank._cfg_any(self.conn, "push.rerank_top_k", 50), 50,
                         "坏 top_k → 默认 50（不是抛，也不是 0 条）")
        selected = fish_push.select_push_candidates(
            self.conn, self._pool(), limit=5, rerank=True, project=PROJECT)
        self.assertEqual(len(selected), 5, "坏 top_k 不许让选择器少选/多选")
        reranked = [c for c in selected if c.get("score_stage") == "rerank"]
        self.assertEqual(len(reranked), 5, "回落默认 50：5 条全在 K 内")
        self.assertIn("push.rerank_top_k", self._diag_keys(), "坏值必须留一条诊断")

    def test_S3_bad_weights_fall_back_to_default(self):
        pool = self._pool(2)
        before = fish_push_rerank.rerank_candidates(self.conn, [dict(c) for c in pool],
                                                    now=fish.now_utc(), project=PROJECT)
        self._cfg("push.rerank_weights", "{这不是 JSON")
        after = fish_push_rerank.rerank_candidates(self.conn, [dict(c) for c in pool],
                                                   now=fish.now_utc(), project=PROJECT)
        self.assertEqual([c["score"] for c in after], [c["score"] for c in before],
                         "坏 JSON → 与默认权重逐条同分")
        self.assertIn("push.rerank_weights", self._diag_keys(), "坏 JSON 必须留一条诊断")

    def test_S3_bad_breaker_seconds_falls_back(self):
        self._cfg("push.breaker_seconds", "x")
        self.assertEqual(fish_push_rerank._cfg_any(self.conn, "push.breaker_seconds", 3.0),
                         3.0, "坏阈值 → 默认 3.0s（不是 0=关闭，也不是抛）")
        self._register_vault()
        out = fish_push._kb_push_candidates(self.conn, self.root, query="撞车判据",
                                            project=PROJECT, limit=2)
        self.assertTrue(out, "回落 3.0s：快路径照推（坏值不许把推送整段切掉）")
        self.assertFalse((self.root / "_audit" / "push_breaker.jsonl").exists(),
                         "没超阈值就不该写断路器诊断（两种诊断不许混）")
        self.assertIn("push.breaker_seconds", self._diag_keys(), "坏阈值必须留一条诊断")

    def test_S3_bad_now_falls_back_to_utc_and_keeps_dup(self):
        cand = self._cand("%s-261027-07" % PROJECT, 0.5)
        self._push(cand["code"], times=1)        # 刚推过 1 次（在 24h 窗口内）
        out = fish_push_rerank.rerank_candidates(self.conn, [cand], now="昨天下午",
                                                 project=PROJECT)
        self.assertEqual(out[0]["score_breakdown"]["dup_penalty"], 0.2,
                         "坏 now → 当前 UTC：刚推过的仍要计 1 次（0.2）；静默关 dup 会是 0.0")
        self.assertIn("now", self._diag_keys(), "坏 now 必须留一条诊断（不许静默关窗口）")


class WeightGuardTest(RerankCase):
    """R16（R4 · 评审 🟡1 按 🟠 防）：`NaN`/`Infinity`/`"nan"`/`|w|>10` 一律**回落默认权重**，
    非有限的总分**不入选**并计数——它们共同的后果是同一条：注入包**静默变空**。

    为什么按 🟠 防：坏权重不报错、`rerank` 照跑、`_diag` 之外没有任何症状；而
    `{"relevance": NaN}` 是**合法 JSON**（`json.loads` 默认收 `NaN`/`Infinity` 字面量），
    手写配置与配置面板都写得出来。NaN 一旦进 `score`，`select` 的 `score > 0` 会把**整池**
    筛掉——不是"少推一条"，是"推送整段消失且没人知道"。
    """

    def setUp(self):
        super().setUp()
        fish_push_rerank._DIAG_SEEN.clear()
        self.diag_path = self.root / "_audit" / "push_config.jsonl"

    def _diag_keys(self):
        if not self.diag_path.exists():
            return []
        return [json.loads(line)["key"] for line in
                self.diag_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _pool(self, size=3):
        return [self._cand("%s-261027-%02d" % (PROJECT, i), round(0.9 - i / 10.0, 4))
                for i in range(size)]

    def _scores(self, pool):
        out = fish_push_rerank.rerank_candidates(self.conn, [dict(c) for c in pool],
                                                 now=fish.now_utc(), project=PROJECT)
        return [c["score"] for c in out]

    def test_R16_nan_and_inf_weights_fall_back_to_default(self):
        pool = self._pool()
        before = self._scores(pool)
        self._cfg("push.rerank_weights",
                  '{"relevance": NaN, "freshness": Infinity, "access_ratio": "nan", '
                  '"link_strength": -Infinity}')
        self.assertEqual(self._scores(pool), before,
                         "四个坏权重 → 逐个落回默认（与没配权重逐条同分）")
        for name in ("relevance", "freshness", "access_ratio", "link_strength"):
            self.assertIn("push.rerank_weights.%s" % name, self._diag_keys(),
                          "坏权重必须留一条诊断（%s）" % name)
        picked = fish_push.select_push_candidates(self.conn, self._pool(), limit=3,
                                                  rerank=True, project=PROJECT)
        self.assertEqual(len(picked), 3, "坏权重不许把候选池筛空（这就是本条的全部意义）")

    def test_R16b_out_of_range_weight_falls_back(self):
        pool = self._pool()
        before = self._scores(pool)
        self._cfg("push.rerank_weights", '{"dup_penalty": -1e9, "relevance": 1e9}')
        self.assertEqual(self._scores(pool), before,
                         "|w|>10 与 NaN 同罪：1e9 会把每条候选都压成 0 分（也是空池）")
        self.assertIn("push.rerank_weights.dup_penalty", self._diag_keys())
        self._cfg("push.rerank_weights", '{"relevance": 1' + "0" * 400 + "}")
        self.assertEqual(self._scores(pool), before,
                         "超大整数字面量（**合法 JSON**）→ float() 抛 OverflowError，同样回默认")
        self.assertIn("push.rerank_weights.relevance", self._diag_keys())

    def test_R16c_nan_first_phase_score_does_not_poison_batch(self):
        pool = self._pool(2) + [self._cand("%s-261027-77" % PROJECT, float("nan"))]
        out = fish_push_rerank.rerank_candidates(self.conn, pool, now=fish.now_utc(),
                                                 project=PROJECT)
        self.assertTrue(all(c["score"] == c["score"] for c in out),
                        "一个 NaN 首阶段分不许把整批 relevance/score 一起毒成 NaN")
        bad = [c for c in out if c["code"] == "%s-261027-77" % PROJECT][0]
        self.assertEqual(bad["score_breakdown"]["relevance"], 0.0,
                         "NaN 首阶段分当 0（没有信息，不是最高分）")
        self.assertEqual(len(fish_push.select_push_candidates(
            self.conn, self._pool(2), limit=3, rerank=True, project=PROJECT)), 2, "池子不空")

    def test_R16d_non_finite_score_is_dropped_and_counted(self):
        pool = self._pool(2)
        with mock.patch.object(fish_push_rerank.rank_mod, "combine_features",
                               return_value=float("nan")):
            out = fish_push_rerank.rerank_candidates(self.conn, [dict(c) for c in pool],
                                                     now=fish.now_utc(), project=PROJECT)
            picked = fish_push.select_push_candidates(self.conn, pool, limit=2,
                                                      rerank=True, project=PROJECT)
        self.assertEqual([c["score"] for c in out], [0.0, 0.0], "NaN 总分 → 0（= 不入选）")
        self.assertIn("push.rerank_score", self._diag_keys(),
                      "非法总分必须**计数**并留一条诊断（不许静默丢）")
        self.assertEqual(picked, [],
                         "全部候选都非法 → 一条都不入选（有合法候选时不许空池，见 R16）")


class ConfigRegisteredTest(RerankCase):
    """RB（R4 · QA B）：**已登记**的键写坏值 → 默认 + 诊断，绝不穿到注入包。

    "登记" = `fish_common.CONFIG_DEFAULTS` 里有这条键（A 合并时会做）。`get_config` 按登记
    类型只转一次，**不看范围也不看字面量**，于是三种症状各来一份：`int` 键遇 `"abc"` 抛
    `ValueError`（抛到 hook 就是整包消失）；`string` 键把 `"abc"` 原样交出去（炸点在
    `float()` 那一行，更远）；`bool` 键把 `"abc"` **静默**读成 `False`（连异常都没有）。
    所以 `_cfg_any` 不把 `get_config` 的返回值直接当值用，一律"按默认类型 cast + 兜底"。

    每个用例先**证明陷阱是真的**（`get_config` 那一行），再断言 `_cfg_any` 的结果——否则
    这条判据只是在复述实现。
    """

    def setUp(self):
        super().setUp()
        fish_push_rerank._DIAG_SEEN.clear()
        self.diag_path = self.root / "_audit" / "push_config.jsonl"

    def _register(self, key, default, typ):
        """把键**真的登记**进注册表（A 合并时会做的事），用例结束还原。"""
        self.addCleanup(fish_common.CONFIG_DEFAULTS.pop, key, None)
        fish_common.CONFIG_DEFAULTS[key] = (default, typ, None)

    def _diag_keys(self):
        if not self.diag_path.exists():
            return []
        return [json.loads(line)["key"] for line in
                self.diag_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _pool(self, size=5):
        return [self._cand("%s-261027-%02d" % (PROJECT, i), round(0.9 - i / 10.0, 4))
                for i in range(size)]

    def test_RB1_registered_int_key_with_text(self):
        self._register("push.rerank_top_k", 50, "int")
        self._cfg("push.rerank_top_k", "abc")
        with self.assertRaises(ValueError):         # 陷阱 ①：登记成 int 后 get_config 直接抛
            fish_common.get_config(self.conn, "push.rerank_top_k")
        self.assertEqual(fish_push_rerank._cfg_any(self.conn, "push.rerank_top_k", 50), 50,
                         "已登记的坏值同样回落默认（50），不是抛")
        self.assertIn("push.rerank_top_k", self._diag_keys(), "已登记坏值也要留诊断")
        self.assertEqual(len(fish_push.select_push_candidates(
            self.conn, self._pool(), limit=5, rerank=True, project=PROJECT)), 5,
            "默认 K=50：5 条全在 K 内（坏值不影响选择器）")

    def test_RB2_registered_bool_key_with_garbage(self):
        self._register("push.rerank_enabled", False, "bool")
        self._cfg("push.rerank_enabled", "abc")
        self.assertFalse(fish_common.get_config(self.conn, "push.rerank_enabled"),
                         "陷阱 ②：bool 键遇 'abc' 被静默读成 False（连异常都没有）")
        self.assertFalse(fish_push_rerank._cfg_any(self.conn, "push.rerank_enabled", False))
        self.assertIn("push.rerank_enabled", self._diag_keys(),
                      "静默成 False 是最难查的一种坏值——必须留诊断")

    def test_RB3_registered_string_key_breaks_breaker(self):
        self._register("push.breaker_seconds", 3.0, "string")
        self._cfg("push.breaker_seconds", "abc")
        self.assertEqual(fish_common.get_config(self.conn, "push.breaker_seconds"), "abc",
                         "陷阱 ③：登记成 string 后原样交出去（下一行 float() 才是炸点）")
        self.assertEqual(fish_push_rerank._cfg_any(
            self.conn, "push.breaker_seconds", 3.0), 3.0, "回落默认 3.0s（不是 0=关闭）")
        out = [{"code": "X"}]
        self.assertEqual(fish_push_rerank._breaker(self.conn, self.root, "kb",
                                                   time.monotonic(), "q", out), out,
                         "阈值坏值回落 3.0：快路径照常返回，不许抛")
        self.assertIn("push.breaker_seconds", self._diag_keys())

    def test_RB4_registered_weights_key_with_bad_json(self):
        pool = self._pool(2)
        before = [c["score"] for c in fish_push_rerank.rerank_candidates(
            self.conn, [dict(c) for c in pool], now=fish.now_utc(), project=PROJECT)]
        self._register("push.rerank_weights", "{}", "string")
        self._cfg("push.rerank_weights", "{这不是 JSON")
        after = [c["score"] for c in fish_push_rerank.rerank_candidates(
            self.conn, [dict(c) for c in pool], now=fish.now_utc(), project=PROJECT)]
        self.assertEqual(after, before, "登记后坏 JSON 仍走默认权重（不是整段重排消失）")
        self.assertIn("push.rerank_weights", self._diag_keys())


class GeneratorInputTest(RerankCase):
    """R13：生成器/一次性迭代入参在 `rerank=True` 下不被吃光（QA F-1）。

    `_tag_kind_class(cands)` 会先遍历一遍池子；旧实现没接返回值，生成器到手第二遍
    已经是空的 → 返回 0 条。修法是入口 `cands = list(cands)`。
    """

    def test_R13_generator_input_survives_rerank(self):
        pool = [self._cand("%s-261027-%02d" % (PROJECT, i), 0.5) for i in range(2)]
        got = fish_push.select_push_candidates(self.conn, (cand for cand in pool), limit=2,
                                               rerank=True, project=PROJECT)
        self.assertEqual(len(got), 2, "生成器入参必须和 list 入参同结果（不许 0 条）")
        self.assertTrue(all("score_stage" in c for c in got), "而且真的重排过")


class RealCorpusTest(RerankCase):
    """R14：真语料库（生产树只读副本）上产 `conv:` 候选（QA D-3）。

    首轮交付文档写"corpus 真库判不到（data/ 下没有 corpus.sqlite）"——**生产树有**
    （153MB，`corpus_fts` 5 万行）。worktree 缺它，所以这里只读复制生产库到临时根，
    判不到就 skip 并写明（判不到 ≠ 通过）。
    """

    def test_R14_real_corpus_conv_candidate(self):
        src = PRODUCTION_P0 / "data" / "corpus.sqlite"
        if not src.exists():
            self.skipTest("生产树 corpus.sqlite 不存在：真语料判不到，不是通过")
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(src) + suffix)
            if path.exists():
                shutil.copy2(path, self.root / (src.name + suffix))
        corpus_conn = sqlite3.connect(
            "file:%s?mode=ro" % (self.root / src.name).as_posix(), uri=True)
        try:
            rows = corpus_conn.execute(
                "SELECT g.session_id, g.text, s.project FROM corpus_segment g "
                "JOIN corpus_session s ON s.session_id=g.session_id "
                "WHERE s.project IS NOT NULL AND s.project<>'' "
                "AND length(g.text) BETWEEN 80 AND 400 "
                "ORDER BY g.occurred_at DESC LIMIT 5").fetchall()
        except sqlite3.Error as exc:
            corpus_conn.close()
            self.skipTest("真语料库结构不符（%s）：判不到，不是通过" % exc)
        corpus_conn.close()
        if not rows:
            self.skipTest("真语料没有可用段：判不到，不是通过")
        found = None
        for sid, text, project in rows:
            got = fish_push._conv_push_candidates(
                self.conn, self.root, query=str(text)[:24], project=project,
                session_key="dsh:" + sid, limit=2)
            convs = [c for c in got if str(c.get("code", "")).startswith("conv:")]
            if convs:
                found = (convs[0], project)
                break
        if found is None:
            self.skipTest("这 %d 个真段都没命中语料检索：判不到，不是通过" % len(rows))
        cand, project = found
        self.assertTrue(str(cand["code"]).startswith("conv:"))
        self.assertEqual(fish_corpus_rank.semantic_kind(cand), "corpus",
                         "真语料候选的 kind_class 必须是 corpus")
        print("[R14] 真语料候选 %s（项目 %s）" % (cand["code"], project))


class ModuleSplitTest(RerankCase):
    """R15：新模块的依赖方向与导出面（J2 类断言 + J7 双向）。

    T03b-R2 把重排整段搬到 `fish_push_rerank`（评审 S4：`fish_push.py` 只剩 1 行余量）。
    搬迁必须同时钉两件事：

      · **J2 类**：干净子进程 `import fish_push_rerank` 不许把 `fish` / `fish_push` 带进来
        （反向依赖的症状不是报错，而是某一侧拿到半初始化的模块）；
      · **J7 双向**：新模块**不进** `fish_push.__all__`（`fish` 的 re-export 清单不变），
        但 `rerank_candidates` 仍能通过 `fish_push.rerank_candidates` 取到（52 §转交点 1
        把它列为冻结接口）。
    """

    def test_R15_clean_import_does_not_pull_fish(self):
        code = ("import sys\n"
                "sys.path.insert(0, r'%s')\n"
                "import fish_push_rerank\n"
                "assert 'fish' not in sys.modules, 'fish_push_rerank 把 fish 带进来了'\n"
                "assert 'fish_push' not in sys.modules, 'fish_push_rerank 把 fish_push 带进来了'\n"
                "assert 'fish_common' in sys.modules, '重排应当依赖 fish_common'\n"
                "assert 'fish_corpus_rank' in sys.modules, '特征刻度来自 fish_corpus_rank'\n"
                "print('ok')\n" % P0)
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(P0), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertIn("ok", proc.stdout)

    def test_R15b_export_surface(self):
        self.assertIs(fish_push.rerank_candidates, fish_push_rerank.rerank_candidates,
                      "rerank_candidates 必须仍可从 fish_push 取到（模块属性）")
        self.assertNotIn("rerank_candidates", fish_push.__all__,
                         "新模块不许加进 __all__（J7：fish 侧 re-export 清单不变）")
        self.assertNotIn("rerank_candidates", getattr(fish, "__dict__", {}),
                         "fish 侧不许意外多出这个名字")
        self.assertGreater(len(fish_push.__all__), 25, "fish_push.__all__ 不许缩水")
        lines = len((P0 / "fish_push_rerank.py").read_text(
            encoding="utf-8").split("\n"))
        self.assertLessEqual(lines, 400, "新模块体量上限 400 行（收到 %d）" % lines)


class OffEquivalenceTest(RerankCase):
    """默认关：`push.rerank_enabled=False` 时 select 与 T03b 之前**逐字节等价**。"""

    def _pool(self):
        return [self._cand("%s-261027-31" % PROJECT, 0.50, freshness=1.0),
                self._cand("%s-261027-32" % PROJECT, 0.70, project=OTHER, freshness=0.1),
                self._cand("%s-261027-33" % PROJECT, 0.60)]

    def test_default_is_off(self):
        self.assertFalse(fish_push._rerank_enabled(self.conn), "默认必须关（零回归的前提）")
        self.assertFalse(fish_push._cfg_any(self.conn, "push.rerank_enabled", False))
        self.assertEqual(fish_push._cfg_any(self.conn, "push.rerank_top_k", 50), 50)
        self.assertEqual(fish_push._cfg_any(self.conn, "push.breaker_seconds", 3.0), 3.0)

    def test_off_selection_is_byte_identical(self):
        pool = self._pool()
        snapshot = [json.dumps(c, sort_keys=True, ensure_ascii=False) for c in pool]
        implicit = fish.select_push_candidates(self.conn, list(pool), limit=2)
        self._cfg("push.rerank_enabled", "false")
        explicit = fish.select_push_candidates(self.conn, list(pool), limit=2)
        self.assertEqual([id(c) for c in implicit], [id(c) for c in explicit],
                         "关闭态返回的必须是同一批对象（不是重排后的副本）")
        self.assertEqual([c["code"] for c in implicit], ["TESTA-261027-32",
                                                         "TESTA-261027-31"],
                         "关闭态排序仍按首阶段分（填充按入池顺序，最后统一按分截断）")
        self.assertEqual([json.dumps(c, sort_keys=True, ensure_ascii=False) for c in pool],
                         snapshot, "关闭态不许给候选加任何字段")
        self.assertTrue(all("kind_class" not in c and "score_stage" not in c for c in pool))

    def test_config_off_wins_over_rerank_flag_only_when_none(self):
        self._cfg("push.rerank_enabled", "false")
        pool = self._pool()
        forced = fish.select_push_candidates(self.conn, pool, limit=2, rerank=True)
        self.assertTrue(all("score_stage" in c for c in forced), "显式 rerank=True 覆盖配置")


if __name__ == "__main__":
    unittest.main(verbosity=2)
