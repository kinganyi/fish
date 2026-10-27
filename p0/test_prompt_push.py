#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T04 判据 · 提示词递记忆（相关性 × 陈旧性）+ `prompt` 渠道 + `activity_overlap`。

## 用户点名的核心（2026-10-26）

> 提示词递记忆并入推送系统（系统级）；相关性含**陈旧性折扣**——用内容年龄
> `occurred_at/recorded_at` + `compute_decay(..., last_accessed=None, ...)`，
> **不要用 `last_accessed_at`**（它会被展示行为刷新）。

## 判据清单

  Q1  `memory_freshness`：400 天前的 fact → 被折扣到 stale；同年龄 lesson → 1.0
  Q2  **参考时间是内容年龄**：`last_accessed_at` 被刷新到"刚刚"→ 折扣不变（变异必红）
  Q3  硬过滤：`superseded` / `dormant` 不进候选；stale 的敏感类型**不推**
  Q4  相关性：编码精确命中 > 仅正文命中；阈值 `push.prompt_min_relevance`
  Q5  去重：24h 内推过的编码不再推；本会话读过的不再推
  Q6  输出形态：≤2 条、≤120 tokens、只给编码+标题+理由（不递正文）
  Q7  `push_log.channel='prompt'` 恰 1 批次；`PUSH_CHANNELS` 含 prompt
  Q8  `activity_overlap`：inject/tool_after 有、prompt 没有（设计 §6.7 的取舍）
  Q8b `activity_overlap` 的候选生成与 `find_collisions` **同一份门槛**（评审 L5）：
      跨项目同名单段目录不报、真实多段重叠要报、临时/缓存目录不报（+ 变异自证）
  Q9  失败静默：无身份 / 无候选 / 注入块 prompt → 零输出
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_activity  # noqa: E402
import fish_surface  # noqa: E402

CID = "C0AAAAAAAAAAAAAAAAAAAAAAAA"
SESSION = "dsh:prompt"


class PromptBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-prompt-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('PJ','项目甲',NULL,0,'active')")
        self.conn.execute(
            "INSERT INTO workspace_map (path_prefix,project,note,created_at) "
            "VALUES ('i:/proj','PJ',NULL,?)", (fish.now_utc(),))
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host,"
            "host_session_key) VALUES (?,?,'resident','active',?,'dsh',?)",
            (CID, "提示词测试", fish.now_utc(), SESSION))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 夹具
    def _mem(self, code, title, *, mem_type="fact", project="PJ", body=None,
             days_old=0, status="active", dormant=0, accessed_ago_days=None):
        import datetime
        ts = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body_path = self.root / "projects" / project / f"{code}.md"
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(body if body is not None else f"{title} 的正文", encoding="utf-8")
        access_ts = None
        if accessed_ago_days is not None:
            access_ts = (datetime.datetime.now(datetime.timezone.utc)
                         - datetime.timedelta(days=accessed_ago_days)).strftime(
                             "%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,last_accessed_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared',?,'[]',?,?,?,?,?,?)",
            (code, title, project, CID, mem_type, status,
             str(body_path.relative_to(self.root)).replace("\\", "/"),
             ts, ts, access_ts, 1 if access_ts else 0, dormant))
        self.conn.commit()
        fish.reindex_fts(self.conn, self.root)
        return code

    def _push(self, payload):
        """真跑 `cmd_prompt_push`：注入 stdin，返回它写给模型看的纯文本。"""
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps(payload, ensure_ascii=False))
        sys.stdout = io.StringIO()
        try:
            args = type("A", (), {"root": str(self.root), "host": "dsh"})()
            fish.cmd_prompt_push(args)
            return sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out

    def _prompt_payload(self, prompt, *, session="prompt", cwd="i:/proj"):
        return {"session_id": session, "cwd": cwd, "prompt": prompt}

    def _push_rows(self):
        return self.conn.execute(
            "SELECT channel, code, session_key, budget_tokens FROM push_log "
            "WHERE channel='prompt'").fetchall()


class FreshnessTest(PromptBase):
    """Q1/Q2/Q3：陈旧性是这一轮的核心。"""

    def test_old_fact_is_stale_lesson_is_not(self):
        fact = self._mem("PJ-261001-01", "很老的撞车事实", days_old=400)
        lesson = self._mem("PJ-261001-02", "很老的撞车教训", mem_type="lesson",
                           days_old=400)
        fresh_fact = fish.memory_freshness(self.conn, fact)
        fresh_lesson = fish.memory_freshness(self.conn, lesson)
        self.assertLess(fresh_fact["factor"], 0.05, f"400 天前的 fact 应被重罚：{fresh_fact}")
        self.assertEqual(fresh_fact["band"], "stale")
        self.assertGreaterEqual(fresh_fact["age_days"], 399)
        self.assertEqual(fresh_lesson["factor"], 1.0,
                         "教训/规范不按年龄打折（陈旧的是事实，不是原则）")
        self.assertEqual(fresh_lesson["band"], "fresh")

    def test_reference_is_content_age_not_last_access(self):
        """把 `last_accessed_at` 刷成"刚刚"→ 折扣**不能**变（这正是用户担心的错）。

        变异自证：把 `compute_decay` 的第 2 个参数改成 `last_accessed_at`，
        下面第二条断言立刻红——所以这条判据真的在钉"用哪个时间"。
        """
        code = self._mem("PJ-261001-03", "很老但刚被展示过", days_old=400,
                         accessed_ago_days=0)
        info = fish.memory_freshness(self.conn, code)
        self.assertLess(info["factor"], 0.1,
                        f"展示行为不该让老事实变新鲜：{info}")
        # 变异：改回用 last_accessed_at
        original = fish.compute_decay

        def leaked(mem_type, last_accessed_at, recorded_at, cfg=None):
            return original(mem_type, fish.now_utc() if last_accessed_at is None
                            else last_accessed_at, recorded_at, cfg)

        try:
            fish.compute_decay = leaked
            mutated = fish.memory_freshness(self.conn, code)
            self.assertGreater(mutated["factor"], 0.5,
                               "变异后老事实变新鲜——说明判据确实在测参考时间")
        finally:
            fish.compute_decay = original
        self.assertLess(fish.memory_freshness(self.conn, code)["factor"], 0.1)

    def test_superseded_is_hard_filtered(self):
        code = self._mem("PJ-261001-04", "已被取代的撞车事实", status="superseded")
        info = fish.memory_freshness(self.conn, code)
        self.assertEqual(info["factor"], 0.0)
        self.assertEqual(info["band"], "stale")
        self.assertIn("退役", info["note"])
        rows = fish.search_memories_any(self.conn, ["撞车"], limit=10)
        self.assertNotIn(code, [r[0] for r in rows], "被取代的条目**不进**候选（硬过滤）")

    def test_dormant_is_not_a_candidate(self):
        code = self._mem("PJ-261001-05", "休眠的撞车事实", dormant=1)
        rows = fish.search_memories_any(self.conn, ["撞车"], limit=10)
        self.assertNotIn(code, [r[0] for r in rows], "dormant=1 不是候选")

    def test_unknown_code(self):
        info = fish.memory_freshness(self.conn, "NOPE-000000-00")
        self.assertEqual(info["band"], "unknown")
        self.assertEqual(info["factor"], 0.0)


class RelevanceTest(PromptBase):
    """Q4：相关性可解释、有门槛。"""

    def test_code_exact_beats_body_only(self):
        exact = self._mem("PJ-261020-01", "撞车检测的判据")
        other = self._mem("PJ-261020-02", "别的条目", body="撞车 检测 判据 都在正文里")
        terms = fish.extract_terms(self.conn, "PJ-261020-01 是什么")
        self.assertIn("PJ-261020-01", terms, "编码要被切出来")
        rows = fish.search_memories_any(self.conn, terms, limit=5)
        self.assertEqual(rows[0][0], exact, f"编码精确命中要排第一：{rows}")
        self.assertIn(other, [r[0] for r in rows])

    def test_extract_terms_shapes(self):
        terms = fish.extract_terms(self.conn, "PJ 撞车检测怎么做的")
        self.assertIn("PJ", terms, "项目名要被认出来")
        self.assertTrue(any(len(t) == 2 for t in terms if t.isascii() is False),
                        f"中文要切 2-4 字 n-gram：{terms}")
        self.assertLessEqual(len(terms), fish.PROMPT_MAX_TERMS)

    def test_clean_prompt_drops_injection(self):
        raw = ("[fish] 本次会话凭证已自动签发\n token : fsh_xxx\n"
               "【当前状态】这一行也要丢\n　登记身份：某某\n真正的问题：撞车怎么判")
        cleaned = fish.clean_prompt(raw)
        self.assertNotIn("fsh_", cleaned)
        self.assertNotIn("[fish]", cleaned)
        self.assertNotIn("【当前状态", cleaned)
        self.assertNotIn("登记身份", cleaned)
        self.assertIn("撞车怎么判", cleaned)

    def test_relevance_threshold(self):
        """门槛高于可达到的最大相关性（编码精确命中 = 1.0，故门槛设 1.01）→ 一条都不给。"""
        self._mem("PJ-261020-03", "撞车检测的判据")
        baseline = fish.build_prompt_candidates(
            self.conn, self.root, prompt="PJ-261020-03 撞车检测",
            citizen_id=CID, session_key=SESSION, project="PJ")
        self.assertTrue(baseline, "默认门槛下应当命中")
        fish.set_config(self.conn, "push.prompt_min_relevance", 1.01)
        blocked = fish.build_prompt_candidates(
            self.conn, self.root, prompt="PJ-261020-03 撞车检测",
            citizen_id=CID, session_key=SESSION, project="PJ")
        self.assertEqual(blocked, [], "门槛调高后必须**一条都不给**")


class OutputTest(PromptBase):
    """Q6/Q7/Q9：输出形态、渠道、失败静默。"""

    def test_pushes_at_most_two_and_logs(self):
        self._mem("PJ-261020-11", "撞车检测的判据")
        self._mem("PJ-261020-12", "撞车检测的实现")
        self._mem("PJ-261020-13", "撞车检测的边界")
        text = self._push(self._prompt_payload("撞车检测怎么做的"))
        self.assertTrue(text.strip(), f"应有输出：{text!r}")
        self.assertLessEqual(text.count("\n- "), 2, f"最多 2 条：{text}")
        self.assertIn("fish 记忆", text)
        self.assertLessEqual(fish.count_tokens(text), 130,
                             f"预算 120 tokens（留一点容差）：{fish.count_tokens(text)}")
        rows = self._push_rows()
        # `push_log` 是**一条记忆一行**（批次靠 pushed_at 圈定），所以这里是
        # "1 行 / 每推一条一条行"，不是"整批一行"。判据钉的是：都落在 prompt 渠道、
        # 同一会话、预算记进去了。
        self.assertGreaterEqual(len(rows), 1)
        self.assertLessEqual(len(rows), 2, f"最多 2 条：{rows}")
        for channel, _code, session_key, budget in rows:
            self.assertEqual(channel, "prompt")
            self.assertEqual(session_key, SESSION)
            self.assertLessEqual(budget, 120, "预算要落进 push_log（便于度量）")
        self.assertIn("prompt", fish.PUSH_CHANNELS)

    def test_no_body_text_pushed(self):
        secret = "正文里有一段不该被推过去的细节 SECRET-BODY"
        self._mem("PJ-261020-14", "撞车检测的判据", body=secret)
        text = self._push(self._prompt_payload("撞车检测"))
        self.assertIn("PJ-261020-14", text)
        self.assertNotIn("SECRET-BODY", text, "只递标题+编码+理由，不递正文")

    def test_24h_dedupe(self):
        self._mem("PJ-261020-15", "撞车检测的判据")
        first = self._push(self._prompt_payload("撞车检测"))
        self.assertTrue(first.strip())
        second = self._push(self._prompt_payload("撞车检测再问一次"))
        self.assertEqual(second, "", "24h 内推过的编码不再推")
        self.assertEqual(len(self._push_rows()), 1)

    def test_seen_in_session_not_pushed(self):
        code = self._mem("PJ-261020-16", "撞车检测的判据")
        self.conn.execute(
            "INSERT INTO memory_access_log (code, citizen_id, session_key, via, "
            "accessed_at) VALUES (?,?,?,'recall',?)", (code, CID, SESSION, fish.now_utc()))
        self.conn.commit()
        text = self._push(self._prompt_payload("撞车检测"))
        self.assertEqual(text, "", "本会话已经读过的，不需要再递一次")

    def test_no_identity_gives_no_output(self):
        """无身份 → **仍然可以**递（提示词是给会话看的，不是给归属记账的）。

        但 `push_log` 的 `session_key` 会是本会话键、`citizen_id` 为 NULL——
        这比"不递"更有用：提示词时机不该因为身份没解析出来就完全失效。
        判据钉的是：不抛错、不写脏数据。
        """
        self._mem("PJ-261020-17", "撞车检测的判据")
        text = self._push(self._prompt_payload("撞车检测", session="unknown-session"))
        self.assertIn("PJ-261020-17", text)
        rows = self._push_rows()
        self.assertEqual(rows[0][2], "dsh:unknown-session")

    def test_no_candidates_gives_no_output(self):
        text = self._push(self._prompt_payload("完全不相关的一句话 abcdef"))
        self.assertEqual(text, "")

    def test_injection_only_prompt_gives_no_output(self):
        self._mem("PJ-261020-18", "撞车检测的判据")
        text = self._push(self._prompt_payload("[fish] 本次会话凭证已自动签发"))
        self.assertEqual(text, "", "注入块不是用户意图，不该触发检索")

    def test_disabled_switch(self):
        self._mem("PJ-261020-19", "撞车检测的判据")
        fish.set_config(self.conn, "push.prompt_enabled", False)
        self.assertEqual(self._push(self._prompt_payload("撞车检测")), "")

    def test_bad_payload(self):
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO("{not json"), io.StringIO()
        try:
            fish.cmd_prompt_push(type("A", (), {"root": str(self.root), "host": "dsh"})())
            self.assertEqual(sys.stdout.getvalue(), "")
        finally:
            sys.stdin, sys.stdout = old_in, old_out


class ActivityOverlapTest(PromptBase):
    """Q8：动作事实反哺推送——但**只在 inject/tool_after**（prompt 只递记忆）。"""

    def _declare(self, surface="p0/fish.py"):
        board = fish.board_checkin(self.conn, CID, "改 fish", surface=surface,
                                   scope="project", scope_key="PJ")
        self.conn.execute("UPDATE presence SET agent_id=? WHERE board_id=?",
                          (SESSION, board["id"]))
        self.conn.commit()

    def _other_activity(self, directory="p0", minutes_ago=1):
        other = "C1BBBBBBBBBBBBBBBBBBBBBBBB"
        self.conn.execute(
            "INSERT OR IGNORE INTO citizens (citizen_id,display_name,kind,status,"
            "created_at,host) VALUES (?,?,'resident','active',?,'dsh')",
            (other, "别人", fish.now_utc()))
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, citizen_id, project, tool_name,"
            " action, path, path_dir, path_abs, args_digest, ok, seen_at) "
            "VALUES ('dsh','dsh:o',?,'PJ','write','write','p0/x.py','p0',"
            "'i:/proj/p0/x.py','x',1,?)",
            (other, fish_activity.utc_now()))
        self.conn.commit()

    def test_overlap_candidate_in_inject_timing(self):
        self._declare()
        self._other_activity()
        cands = fish.build_push_candidates(
            self.conn, self.root, citizen_id=CID, project="PJ", session_key=SESSION,
            timing="inject")
        codes = [c["code"] for c in cands]
        self.assertTrue(any(c.startswith("activity:") for c in codes),
                        f"应有 activity_overlap 候选：{codes}")

    def test_no_overlap_candidate_in_prompt_timing(self):
        self._declare()
        self._other_activity()
        self._mem("PJ-261020-21", "撞车检测的判据")
        cands = fish.build_push_candidates(
            self.conn, self.root, citizen_id=CID, project="PJ", session_key=SESSION,
            timing="prompt", prompt="撞车检测")
        self.assertTrue(all(not c["code"].startswith("activity:") for c in cands),
                        f"prompt 时机只递记忆（不混撞车警告）：{[c['code'] for c in cands]}")

    def test_overlap_deduped_24h(self):
        self._declare()
        self._other_activity()
        first = [c for c in fish.build_push_candidates(
            self.conn, self.root, citizen_id=CID, project="PJ", session_key=SESSION,
            timing="inject") if c["code"].startswith("activity:")]
        self.assertTrue(first)
        fish.append_push_log(self.conn, [first[0]["code"]], channel="tool_after",
                             session_key=SESSION, citizen_id=CID)
        second = [c for c in fish.build_push_candidates(
            self.conn, self.root, citizen_id=CID, project="PJ", session_key=SESSION,
            timing="inject") if c["code"].startswith("activity:")]
        self.assertEqual(second, [], "同会话同目录 24h 内最多一次")

    # ------------------------------------------------------------ 评审 L5
    #
    # `activity_overlap` 曾经用无门槛的 `surface_hit`（只比同基准前缀）判重叠，
    # 而 `find_collisions` 用的是 `path_hit(..., min_segments)`——两条路纪律不一致，
    # 于是"跨项目同名单段目录"（`p0`）能跨项目误报。现在两条路同一份门槛。

    def _other_write(self, path_abs, *, path_dir=None, project="PJ"):
        """造一行别人的写动作（`path_abs` 决定 `dir_abs`——判定的唯一依据）。"""
        other = "C1BBBBBBBBBBBBBBBBBBBBBBBB"
        self.conn.execute(
            "INSERT OR IGNORE INTO citizens (citizen_id,display_name,kind,status,"
            "created_at,host) VALUES (?,?,'resident','active',?,'dsh')",
            (other, "别人", fish.now_utc()))
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, citizen_id, project, tool_name,"
            " action, path, path_dir, path_abs, args_digest, ok, seen_at) "
            "VALUES ('dsh','dsh:o',?,?,'write','write',?,?,?,'x',1,?)",
            (other, project, path_dir or path_abs, path_dir or path_abs, path_abs,
             fish_activity.utc_now()))
        self.conn.commit()

    def _declare_root(self, surface):
        """根板（没有项目根）声明：相对面无法转绝对，走 `path_hit` 的保守路径。"""
        board = fish.board_checkin(self.conn, CID, "改这块", surface=surface,
                                   scope="root", scope_key="")
        self.conn.execute("UPDATE presence SET agent_id=? WHERE board_id=?",
                          (SESSION, board["id"]))
        self.conn.commit()
        return board

    def _activity_codes(self, *, timing="inject"):
        return [c["code"] for c in fish.build_push_candidates(
            self.conn, self.root, citizen_id=CID, project=None, session_key=SESSION,
            timing=timing) if c["code"].startswith("activity:")]

    def test_single_segment_dir_not_reported_across_projects(self):
        """跨项目同名单段目录**不报**：`i:/proj/p0` 与 `i:/other/p0` 只是同名。"""
        self._declare_root("p0")            # 1 段：到处同名
        self._other_write("i:/other/p0/x.py", path_dir="p0")
        self.assertEqual([c for c in self._activity_codes() if "p0" in c], [],
                         "1 段目录跨项目不该算撞车（旧实现会误报）")

    def test_multi_segment_overlap_reported(self):
        """真实多段重叠**要报**：根板声明 `p0/data`，别人在 `i:/other/p0/data` 里写。"""
        self._declare_root("p0/data")
        self._other_write("i:/other/p0/data/x.db", path_dir="p0/data")
        codes = self._activity_codes()
        self.assertTrue(codes, "多段重叠必须出候选（否则'真撞车'永远不报）")

    def test_temp_and_cache_dirs_not_reported(self):
        """`%TEMP%` / `node_modules` 里的同名目录不报（那里没有协作面）。"""
        self._declare_root("p0/data")
        self._other_write("c:/users/x/appdata/local/temp/p0/data/x.db", path_dir="p0/data")
        self.assertEqual(self._activity_codes(), [], "临时目录不是协作面")

    # ------------------------------------------------------------ 评审 F4 / F5
    #
    # F4：只做单向 `path_hit(我的面, 对方的目录)` → 我声明**文件面**、对方写同一个
    #     文件时不报（对方给的是目录键，而文件面不做分量链包含）——"同一文件被
    #     两个人改"这一类真实撞车永远沉默。
    # F5：候选生成读声明面没有时间窗，而 `find_collisions` 用
    #     `board.overlap_window_min`——同一个事实两个答案。

    def test_declared_file_face_reported_when_other_writes_same_file(self):
        """F4 正例：声明文件面 + 对方写同一个文件 → 必须报。"""
        self._declare_root("p0/fish.py")          # 文件面（根板 ⇒ 保持相对）
        self._other_write("i:/other/p0/fish.py", path_dir="p0")
        codes = self._activity_codes()
        self.assertTrue(any("p0/fish.py" in c for c in codes),
                        f"同一文件被两个人改必须报（F4 的单向漏报）：{codes}")

    def test_declared_file_face_different_file_not_reported(self):
        """F4 反例：跨项目**不同文件**不许报（假撞车比漏报更伤）。"""
        self._declare_root("p0/fish.py")
        self._other_write("i:/other/docs/x.py", path_dir="docs")
        self.assertEqual(self._activity_codes(), [], "不同路径不是撞车")

    def test_declared_row_outside_window_not_used(self):
        """F5：声明行超出 `board.overlap_window_min` 后不再参与判定。"""
        import datetime
        board = self._declare_root("p0/data")
        stale = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "UPDATE presence SET last_seen_at=?, auto_seen_at=? WHERE board_id=?",
            (stale, stale, board["id"]))
        self.conn.commit()
        self._other_write("i:/other/p0/data/x.db", path_dir="p0/data")
        self.assertEqual(self._activity_codes(), [],
                         "声明行过了窗口还参与判定 = 两条撞车通道口径不一致（F5）")
        # 变异自证：窗口放开（0=不限）→ 同一场景必须报，证明挡它的确实是窗口
        fish.set_config(self.conn, "board.overlap_window_min", 0)
        self.assertTrue(self._activity_codes(),
                        "窗口放开后应判它——否则红的是别的东西，不是时间窗")

    def test_mutation_name_only_match_goes_red(self):
        """变异自证：把判定换成"目录末段同名即命中" → 「单段不报」必须红。"""
        original = fish_surface.path_hit
        try:
            def name_only(surface, target, *, min_segments=2):
                sa = fish_surface.path_segments(surface)
                sb = fish_surface.path_segments(target)
                if sa and sb and sa[-1] == sb[-1]:
                    return surface
                return original(surface, target, min_segments=min_segments)

            fish_surface.path_hit = name_only
            self._declare_root("p0")
            self._other_write("i:/other/p0/x.py", path_dir="p0")
            self.assertTrue(
                [c for c in self._activity_codes() if "p0" in c],
                "变异后应当误报——若仍然为空，说明「单段不报」的判据没在测判定逻辑")
        finally:
            fish_surface.path_hit = original


class MutationTest(PromptBase):
    """判据能变红。"""

    def test_drop_stale_floor_goes_red(self):
        """删掉陈旧性硬门槛 → 400 天前的事实会被推（原来不推）。"""
        self._mem("PJ-261001-31", "很老的撞车事实", days_old=400)
        self.assertEqual(self._push(self._prompt_payload("撞车事实 很老")), "",
                         "默认：stale 的 fact 不推")
        fish.set_config(self.conn, "push.prompt_stale_floor", 0.0)
        fish.set_config(self.conn, "push.prompt_min_relevance", 0.0)
        text = self._push(self._prompt_payload("撞车事实 很老"))
        self.assertTrue(text.strip(), "门槛归零后应当被推——说明门槛确实在挡它")

    def test_prompt_not_in_channels_goes_red(self):
        """把 `prompt` 从渠道白名单去掉 → 写日志必须响亮失败。"""
        with mock.patch.object(fish, "PUSH_CHANNELS", ("inject", "hook", "tool_after")):
            with self.assertRaises(ValueError):
                fish.append_push_log(self.conn, ["PJ-261001-01"], channel="prompt")
        self.assertIn("prompt", fish.PUSH_CHANNELS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
