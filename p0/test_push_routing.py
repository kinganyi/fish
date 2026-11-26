#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W4.5 / W4.7 判据 · 推送路由 + 三开关 + 闸门切换（113 §6.4 · 2026-11-25）。

## W4.5 只建开关，**不切闸门**

`push.no_identity=allow` + `push.target_mode=session` 就是今天的实现行为：prompt 侧
无身份照推（`citizen_id` 为空的 prompt 推送 = 501 条读数即证）、投递按会话键去重。
W4.5 只做三件事：两个键登记进 `CONFIG_DEFAULTS`、`push_log` 写对两列、闸门与
docstring 同源。`skip` 档是 **W4.7**、WB 侧父子校正（`agent_id` + `transcript_path`）
是 **W4.6**——顺序反了会挡掉 76.7% 的 WB prompt 推送（110 §3.5.3）。

## W4.7 来了，但**没切**——这是取证结论，不是拖延

W4.7 规定：先取证"有没有真实 WB SessionStart 走过 `ensured`"，没有就**不许切**。
2026-11-25T01:42Z 取证结果：`_audit/hook-calls.jsonl` 140 行里 **`ensured` 记录 =
0 条**（最后一条发生在 W4.6 落地 **之前**，还是 `ERROR 该会话尚未登记`）；同时 7d 内
`codebuddy:%` prompt 推送 **312 行 / 无身份 238 行（76.3%）**，此刻切 `skip` 会
挡掉这 238 行。⇒ 开关与判据落地，档位保持 `allow`，回执写 "ready, awaiting host
restart"。

所以本文件的 J-P5 判的是"**能不能**切"而不是"切了没有"：它是条件判据——线上档位
是 `allow` 就守"不挡行"，是 `skip` 就守"挡 0 行 + 量 ≥95%"。变异体（WB 未 ensure
就切 `skip`）必须让它红，那就是"今天不能切"的可执行证据。

## 判据（每条都带反面）

  J-P1  未命名公民推送**停止新增**（**时间窗**，不是恒 0）+ 7d 回放的 would_block
  J-P2  父子不串投：**DSH 真判**（子会话的推送落在子键、并带父）；WB 标 `[判不到]`
  J-P3  `push.no_identity` 两档行为 + docstring 与实现同源（三件都要）
  J-P5  闸门切换的前置条件（**条件判据**：allow 恒绿 / skip 时挡 0 行且量 ≥95%）

## 冻结读数（2026-11-24T23:48Z，生产库 `mode=ro`）

  · `未命名%` 公民的推送：**210 行 / 40 会话**（110 §3.5 写的 193/32 是 21:35 的
    读数——活数据会漂，所以判据锚"窗口内是否**新增目标**"，不锚那个数字）
  · 7d 窗口（`pushed_at >= 2026-11-18`）`channel='prompt' AND citizen_id IS NULL`：
    **238 行**（= 若默认写成 `skip` 会被挡掉的行数；110 写 243 是 21:42 的读数）
  · 同窗口 DSH 侧 prompt **150 行 / 无身份 0 行** —— "全零"不可能是"没人在推"

## 为什么时间窗而不是"总数=0"

未命名公民的存量行不会因为 W4.5 而消失（历史就是历史），而 W4.6 之前**已经开始**
推送的老会话还会继续推。判据写"总数=0"会永久红，写"窗口内新增=0"才有判别力。
"""
import datetime
import inspect
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_common  # noqa: E402
import fish_push_hook  # noqa: E402

CID = "C0AAAAAAAAAAAAAAAAAAAAAAAA"
PROMPT = "撞车检测"
TITLE = "撞车检测的判据"

#: 本波落地时刻（UTC）：时间窗判据的**下界**（只判"落地之后有没有新增目标"）。
#: 冻结在会话树施工图的 W4.5 落地那一刻；`FISH_W45_LANDED_UTC` 可覆盖
#: （复跑历史窗口时不必改代码）。**写成下界而不是"总数=0"**：存量行不会消失，
#: 而 W4.5 之前就已存在的会话还会推——那些不是"新增"（113 §6.4 的 J-P1 就是这条口径）。
W45_LANDED_UTC = os.environ.get("FISH_W45_LANDED_UTC", "2026-11-25T00:20:00Z")
#: 7d 回放窗口的下界（与 110 §3.5.3 的读数窗口对齐）
REPLAY_SINCE = "2026-11-18"
PROD_DB = HERE / "data" / "index.sqlite"


def _prod_db_is_real(path) -> bool:
    """生产库**准入口**：不存在 / 空壳 / 打不开 → False（调用方 skipTest）。

    公开镜像里 `data/index.sqlite` 是个空壳（0 张表），拿它硬跑这些判据会得到
    "假绿"——所以先问一句"这张表里真有行吗"。
    """
    path = Path(path)
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM push_log").fetchone()[0] > 1000
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _replay_blocked(conn, mode=None, *, since=REPLAY_SINCE) -> int:
    """回放 prompt 推送：`push.no_identity` 在该档下会**挡掉**多少行。

    判定一律调**应用自己的函数**（`fish.push_gate_no_identity`）——在这里另写一份
    "什么算无身份"就是自建模型，自建的绿不算绿（也不会随实现漂）。
    """
    rows = conn.execute(
        "SELECT citizen_id FROM push_log WHERE channel='prompt' AND pushed_at >= ?",
        (since,)).fetchall()
    return sum(1 for (cid,) in rows if fish.push_gate_no_identity(conn, cid, mode=mode))


class RoutingBase(unittest.TestCase):
    """临时库夹具（一行都不碰生产库；生产读数在 `ProdReadingTest` 里单独只读跑）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-w45-"))
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
            (CID, "提示词测试", fish.now_utc(), "dsh:prompt"))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 夹具
    def _mem(self, code="PJ-261125-01", title=TITLE):
        body_path = self.root / "projects" / "PJ" / f"{code}.md"
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(f"{title} 的正文", encoding="utf-8")
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,last_accessed_at,access_count,dormant) "
            "VALUES (?,?,'PJ',?,'fact','shared','active','[]',?,?,?,NULL,0,0)",
            (code, title, CID,
             str(body_path.relative_to(self.root)).replace("\\", "/"), ts, ts))
        self.conn.commit()
        fish.reindex_fts(self.conn, self.root)
        return code

    def _push(self, payload, *, host="dsh"):
        """真跑 `cmd_prompt_push`：注入 stdin，返回它递给模型的纯文本。"""
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps(payload, ensure_ascii=False))
        sys.stdout = io.StringIO()
        try:
            arg = type("A", (), {"root": str(self.root), "host": host})()
            fish.cmd_prompt_push(arg)
            return sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out

    @staticmethod
    def _payload(session, **extra):
        payload = {"session_id": session, "cwd": "i:/proj", "prompt": PROMPT}
        payload.update(extra)
        return payload

    def _nrows(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM push_log").fetchone()[0]

    def _last_row(self):
        return self.conn.execute(
            "SELECT session_key,citizen_id,agent_id,parent_session FROM push_log "
            "ORDER BY id DESC LIMIT 1").fetchone()


# ============================================================== J-P1（回放）
class Jp1ReplayTest(RoutingBase):
    """J-P1 的回放面：`push.no_identity` 两档在**同一批行**上给出不同结论。

    这一组是"默认值 = 今天行为"的可判形式：默认档下回放**一行都不该挡**。
    把 `CONFIG_DEFAULTS` 的默认值改成 `skip` → 第一条立刻红（见
    `test_JP1_mutation_default_skip_goes_red` 与 `mutation_push_routing.py` M1）。
    """

    def _seed_rows(self, anonymous=5, named=3):
        for i in range(anonymous):
            fish.append_push_log(self.conn, [f"PJ-261199-{i:02d}"], channel="prompt",
                                 session_key=f"dsh:anon-{i}", citizen_id=None)
        for i in range(named):
            fish.append_push_log(self.conn, [f"PJ-261198-{i:02d}"], channel="prompt",
                                 session_key=f"dsh:prompt", citizen_id=CID)
        return anonymous

    def test_JP1_default_allow_blocks_nothing(self):
        """默认档（= 配置表里那行字面量）回放**0 挡**——这就是"今天的行为"。"""
        anon = self._seed_rows()
        self.assertEqual(fish.get_config(self.conn, "push.no_identity"), "allow",
                         "默认值必须是 allow（W4.7 才切 skip）")
        self.assertEqual(_replay_blocked(self.conn), 0,
                         "默认档挡掉了行——默认值已经不在「今天的行为」上了")
        self.assertEqual(_replay_blocked(self.conn, "skip"), anon,
                         "skip 档必须恰好挡掉无身份的那些行（判别力的另一半）")

    def test_JP1_named_rows_are_never_blocked(self):
        """有身份的行两档都不挡（闸门只对"判不到身份"生效，不许顺手挡掉正常人）。"""
        self._seed_rows()
        for mode in ("allow", "skip"):
            blocked = [cid for (cid,) in self.conn.execute(
                "SELECT citizen_id FROM push_log WHERE channel='prompt'").fetchall()
                if fish.push_gate_no_identity(self.conn, cid, mode=mode)]
            self.assertNotIn(CID, blocked, f"{mode} 档把有身份的行也挡了：{blocked}")

    def test_JP1_mutation_default_skip_goes_red(self):
        """变异自证：默认值写 `skip` → "回放 0 挡"这条**必红**（M1 的进程内版本）。"""
        self._seed_rows()
        original = fish_common.CONFIG_DEFAULTS["push.no_identity"]
        try:
            fish_common.CONFIG_DEFAULTS["push.no_identity"] = (
                "skip", "enum", ["allow", "skip"])
            self.assertGreater(_replay_blocked(self.conn), 0,
                               "默认改成 skip 后回放仍然 0 挡——这条判据没有判别力")
        finally:
            fish_common.CONFIG_DEFAULTS["push.no_identity"] = original
        self.assertEqual(_replay_blocked(self.conn), 0, "还原后必须复绿")

    def test_JP1_gate_only_reads_the_two_known_modes(self):
        """键的取值域是闭集：`fish config set push.no_identity x` 必须被拒。"""
        with self.assertRaises(ValueError):
            fish.set_config(self.conn, "push.no_identity", "maybe")
        self.assertEqual(fish.CONFIG_DEFAULTS["push.no_identity"][1], "enum")
        self.assertEqual(fish.CONFIG_DEFAULTS["push.no_identity"][2], ["allow", "skip"])


# ============================================================== J-P2（路由）
class Jp2DshRoutingTest(RoutingBase):
    """J-P2①：**DSH 侧真判**——子会话的推送落在子键上，并带父；父键不多收一份。

    构造方式：直接把父边写进投影表（`session_lineage`）。`fish_lineage.parent_of`
    先查投影、投影没有才现读会话头（113 §5.7），所以这条判据不需要真宿主日志，
    测的是**路由本身**而不是"能不能读头"（那是 W2 的判据）。
    """

    PARENT = "parent-session-1"
    CHILD = "child-session-1"

    def _seed_child(self):
        self.conn.execute(
            "INSERT INTO session_lineage (host,session_id,parent_session,lineage_path,"
            "depth,depth_host,origin,relation,basis,source,observed_at) VALUES "
            "('dsh',?,?,?,1,1,'subagent','spawn','header','dsh:header',?)",
            (self.CHILD, self.PARENT, f"/{self.PARENT}/{self.CHILD}", fish.now_utc()))
        self.conn.commit()

    def test_JP2_child_push_lands_on_child_key_with_parent(self):
        self._mem()
        self._seed_child()
        text = self._push(self._payload(self.CHILD))
        self.assertIn("PJ-261125-01", text)
        row = self._last_row()
        self.assertEqual(row[0], f"dsh:{self.CHILD}",
                         f"推送没落在子会话键上：{row}")
        self.assertEqual(row[3], self.PARENT,
                         f"`parent_session` 没写父（判父只走 fish_lineage.parent_of）：{row}")
        self.assertIsNone(row[2], "DSH 宿主没有 agent id，必须恒 NULL（不许拿会话键冒充）")

    def test_JP2_parent_key_never_receives_the_child_push(self):
        """父子不串投：子会话那一次推送**不许**在父键上多出一行。"""
        self._mem()
        self._seed_child()
        self._push(self._payload(self.CHILD))
        n = self.conn.execute("SELECT COUNT(*) FROM push_log WHERE session_key=?",
                              (f"dsh:{self.PARENT}",)).fetchone()[0]
        self.assertEqual(n, 0, "父会话替子会话收了一份推送（串投）")

    def test_JP2_target_mode_citizen_writes_neither_column(self):
        """`citizen` 档 = 旧口径：两列都不写（回退就一行配置）。"""
        self._mem()
        self._seed_child()
        fish.set_config(self.conn, "push.target_mode", "citizen")
        self._push(self._payload(self.CHILD))
        row = self._last_row()
        self.assertIsNone(row[2], f"citizen 档不该写 agent_id：{row}")
        self.assertIsNone(row[3], f"citizen 档不该写 parent_session：{row}")

    def test_JP2_no_parent_is_null_not_main(self):
        """判不到父 → NULL。**不许兜成 main**（110 §3.6 的"孤儿诚实"）。"""
        self._mem()
        text = self._push(self._payload("no-such-session"))
        self.assertIn("PJ-261125-01", text)
        row = self._last_row()
        self.assertEqual(row[0], "dsh:no-such-session")
        self.assertIsNone(row[3], f"判不到父却写了一个值：{row}")

    def test_JP2_wb_payload_is_not_corrected_in_w45(self):
        """W4.5 **不做** WB 校正：payload 里明明有 `agent_id`/`transcript_path`，两列仍写 NULL。

        把这条判据反过来（"W4.5 就开始校正"）不是进步：WB 的真父子要靠
        `agent_id` + `transcript_path` **双**校正，而 W4.6 才写那个解析器（110 §3.5.1 规则 3）。
        现在猜一个父就是串投风险——"宁可不推，不串推"。
        """
        self._mem()
        payload = self._payload(
            "wb-lead-1", agent_id="agent-abcdef12",
            transcript_path=r"C:\wb\wb-lead-1\subagents\agent-abcdef12.jsonl")
        self.assertTrue(self._push(payload, host="workbuddy"))
        row = self._last_row()
        self.assertEqual(row[0], "codebuddy:wb-lead-1", f"WB 会话键口径变了：{row}")
        self.assertIsNone(row[2], f"W4.5 不该写 WB 的 agent_id：{row}")
        self.assertIsNone(row[3], f"W4.5 不该猜 WB 的父：{row}")

    def test_JP2_wb_cross_delivery_is_not_judgeable_yet(self):
        """J-P2①（WB 那一半）：`codebuddy:%` 的推送是否落在父键上——**判不到**。

        **跳过理由不是"W4.6 未落地"**（那是过期理由，2026-11-25 评审 🟠-1 点出）：
        W4.6（`3f55e8f`）已落地 WB 身份 ensure（继承/幂等/防环/provisional），
        `test_wb_identity` 17 条在跑；`test_JP2_wb_payload_is_not_corrected_in_w45`
        也在跑。真正的原因是**键口径**，不是缺实现：

          · WB 宿主报的 `session_id` 就是**父会话** id（113 §7.1 明写"WB 侧不改口径"），
            所以 WB 的会话键 `codebuddy:<父sid>` 与父键**是同一个字符串**；
          · 子代理（`agent-*.jsonl`）根本没有独立会话键（实测 7d 推送键 12/12 都是
            会话目录名，没有一个 `agent-*`）；
          ⇒ "子推送落在**子键**上、且带父"这件事在 WB 侧**没有可构造的样本**。
            拿它写判据，只会得到一个恒真的反连接——**绿是假的，比红更糟**。

        所以这里**如实标不可判**，而不是把它弄绿。要真判只有一条路：先定 WB 的真子键，
        那等于推翻 §7.1 的"键口径不改"——**是设计裁决，不该由实现侧私了**（造一个
        WB 子键就能让判据绿，代价是整条推送归属的口径跟着变）。113 §6.4 的判据表
        已同步把 WB 半条标 `[判不到]` 并写明原因。

        数据面今天是诚实的：生产 `push_log` 里 `codebuddy:%` 行的 `agent_id` /
        `parent_session` 全 NULL（不写假父、不串投）——那一半由
        `test_JP2_wb_payload_is_not_corrected_in_w45` 与生产读数守着。
        """
        self.skipTest("WB 键即父键（113 §7.1 键口径不改）⇒ J-P2① 在 WB 侧在本设计下不可判")


# ============================================================== J-P3（闸门）
class Jp3GateTest(RoutingBase):
    """J-P3：`push.no_identity` 的 allow/skip 两档 + docstring 与实现同源。

    三件缺一不可：① `allow` 时照旧推；② `skip` 时零输出且无新行；
    ③ docstring 含"无身份" **且** 实现里有受开关约束的 early-return
    （W4.5 之前这两句是**反的**：docstring 说有，实现没有——501 条读数即证）。
    """

    ANON = "anon-session-1"

    def test_JP3_allow_still_pushes(self):
        """① `allow`（默认）→ 照旧推（今天的实现行为，`push_log` 的 `citizen_id` 为 NULL）。"""
        self._mem()
        text = self._push(self._payload(self.ANON))
        self.assertIn("PJ-261125-01", text, "默认档挡住了推送（那不是今天的行为）")
        row = self._last_row()
        self.assertEqual(row[0], f"dsh:{self.ANON}")
        self.assertIsNone(row[1], "无身份推送的 `citizen_id` 必须是 NULL（不猜）")

    def test_JP3_skip_blocks_with_zero_output(self):
        """② `skip` → `stdout == ''` 且**不写行**。"""
        self._mem()
        fish.set_config(self.conn, "push.no_identity", "skip")
        self.assertEqual(self._push(self._payload(self.ANON)), "")
        self.assertEqual(self._nrows(), 0, "skip 档仍然写了 push_log")

    def test_JP3_docstring_and_implementation_agree(self):
        """③ 文档与实现同源（两句话必须都在，且 early-return 真的在实现里）。"""
        doc = fish.cmd_prompt_push.__doc__ or ""
        src = inspect.getsource(fish.cmd_prompt_push)
        self.assertIn("无身份", doc, "docstring 的否定条件里不再提「无身份」")
        self.assertIn("push.no_identity", doc,
                      "docstring 必须写明这条闸门**受开关约束**（否则又是一句谎话）")
        self.assertIn("push_gate_no_identity", src,
                      "实现里没有受开关约束的 early-return")

    def test_JP3_mutations_prove_the_criterion(self):
        """变异自证（两个方向）：闸门常开/常闭，判据各自变红。

        ① 把闸门摘掉（常放行）→ skip 档**会**推 ⇒ "skip 零输出"依赖闸门；
        ② 把闸门焊死（常挡）→ allow 档**不**推 ⇒ "allow 照旧推"依赖开关。
        """
        self._mem()
        original = fish.push_gate_no_identity
        try:
            fish.set_config(self.conn, "push.no_identity", "skip")
            fish.push_gate_no_identity = lambda *a, **k: False
            self.assertIn("PJ-261125-01", self._push(self._payload("anon-mut1")),
                          "摘掉闸门后 skip 档仍不推——② 没有判别力")
        finally:
            fish.push_gate_no_identity = original
        try:
            fish.set_config(self.conn, "push.no_identity", "allow")
            fish.push_gate_no_identity = lambda *a, **k: True
            self.assertEqual(self._push(self._payload("anon-mut2")), "",
                             "焊死闸门后 allow 档仍在推——① 没有判别力")
        finally:
            fish.push_gate_no_identity = original

    def test_JP3_hook_channel_keeps_its_own_strict_rule(self):
        """tool_after 通道：**两档都挡**（它今天就有"无身份不猜"，W4.5 不放宽推送面）。"""
        self.assertEqual(fish.get_config(self.conn, "push.no_identity"), "allow")
        self.assertFalse(fish.push_gate_no_identity(self.conn, None),
                         "prompt 侧 allow 档不该挡（① 的前提）")
        self.assertTrue(fish.push_gate_no_identity(self.conn, None, strict_anonymous=True),
                        "allow 档把 tool_after 的无身份闸门放开了——那是新增推送面")
        fish.set_config(self.conn, "push.no_identity", "skip")
        self.assertTrue(fish.push_gate_no_identity(self.conn, None, strict_anonymous=True))
        src = inspect.getsource(fish_push_hook)      # 整个模块：闸门在 `_run` 里
        # 断言**完整调用**（不是 `strict_anonymous=True` 这个片段：注释里也会提到它，
        # 那样变异体把参数改成 False 时判据仍然绿——本判据自己就吃过这个亏）
        self.assertIn("push_gate_no_identity(conn, citizen_id, strict_anonymous=True)", src,
                      "hook 没有按同一口径读开关（或改成了放宽）")


# ============================================================== J-P5（闸门切换）
#
# W4.7 的判据形态与前面几条不同：前面判的是"开关行为对不对"，这里判的是
# "**能不能**把闸门真切到 skip"。切换是**生产配置动作**（`fish config set`），
# 不是改代码，所以判据必须能在"切之前"回答"切了会怎样"——这就是回放的意义。

#: 切换前 7d 的 WB prompt 推送量（`codebuddy:%`，`channel='prompt'`）。
#: **不锚死数字**：判据只用它算"≥95%"，活读数每跑一次现取。活数据会漂——
#: 110 §3.5 记的 317 是设计快照，2026-11-25T01:42Z 实测已漂到 312（无身份 238）。
#: `FISH_WB_BASELINE_7D` 可覆盖：真切之后应把基线钉到"切换那一刻的前 7d"。
WB_BASELINE_7D = int(os.environ.get("FISH_WB_BASELINE_7D", "317"))
#: 判据只锚这条**比例**（317 → ≥302 就是这么来的：`317 × 0.95 = 301.15`）。
WB_KEEP_RATIO = 0.95
#: 2026-11-25T01:42Z 实测的分子/分母：312 行里 **238 行无身份**（76.3%）。
#: 变异判据用它复现"此刻切 skip 会挡 238 行"——这是**证据读数**，不是阈值。
WB_ANON_ROWS = 238
WB_NAMED_ROWS = 74        # 312 - 238


def _wb_7d(conn, *, since=REPLAY_SINCE):
    """7d 窗口里 WB prompt 推送的 `(总行数, 无身份行数)`。"""
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN citizen_id IS NULL OR citizen_id='' "
        "THEN 1 ELSE 0 END) FROM push_log "
        "WHERE channel='prompt' AND session_key LIKE 'codebuddy:%' AND pushed_at >= ?",
        (since,)).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def jp5_verdict(conn, *, mode, wb_total, wb_anon, baseline=None):
    """J-P5 的判定表：按 `push.no_identity` 的档位选**该守哪条**。

    返回 `(读数 dict, 失败项 list)`；`失败项 == []` 才算过。

      · `allow`（今天的行为）→ 闸门**不挡任何行**：没切的时候不许悄悄生效。
      · `skip`（切了）→ 两条**同时**成立：
          ① 回放挡 0 行（WB 都有身份了，闸门无事可挡）；
          ② 7d 量 ≥ 基线 95%（挡 0 行但量塌了同样是事故：通道死了，闸门再干净也没意义）。

    回放一律调 `fish.push_gate_no_identity`（**应用自己的函数**）——"多少行算无身份"
    不许在这里另写一份。而"≥95%"不属于应用行为（它是发布判据），所以住在这里。
    """
    baseline = WB_BASELINE_7D if baseline is None else int(baseline)
    blocked = _replay_blocked(conn, mode)
    named = max(0, int(wb_total) - int(wb_anon))
    verdict = {"mode": mode, "would_block": blocked, "wb_total": int(wb_total),
               "wb_named": named,
               "kept_ratio": (named / int(wb_total)) if wb_total else 0.0,
               "baseline": baseline, "volume_floor": WB_KEEP_RATIO * baseline}
    failures = []
    if mode == "skip":
        if blocked:
            failures.append(
                f"skip 档回放挡掉 {blocked}/{wb_total} 行 WB 推送"
                f"（WB 还没 ensure 就切闸门 = {blocked} 行凭空消失）")
        if int(wb_total) < WB_KEEP_RATIO * baseline:
            failures.append(
                f"7d WB 推送 {wb_total} 行 < 基线 {baseline} 的 {WB_KEEP_RATIO:.0%}"
                f"（{WB_KEEP_RATIO * baseline:.0f} 行）——通道量塌了")
    elif blocked:
        failures.append(f"allow 档回放挡掉了 {blocked} 行——闸门在未切换时就生效了")
    return verdict, failures


class Jp5SwitchCriterionTest(RoutingBase):
    """J-P5：闸门切换的**前置条件**判据（W4.7）。

    这一组不判"切了没有"，判的是"**能不能**切"：同一张判定表在两种档位下各守一条，
    而变异体（WB 未 ensure 就切 skip）必须让它红——这就是 W4.7 第 1 步取证的可执行形式。
    """

    TOTAL = WB_ANON_ROWS + WB_NAMED_ROWS

    def _seed_wb(self, anon=0, named=0):
        for i in range(anon):
            fish.append_push_log(self.conn, [f"PJ-261196-{i:03d}"], channel="prompt",
                                 session_key=f"codebuddy:anon-{i}", citizen_id=None)
        for i in range(named):
            fish.append_push_log(self.conn, [f"PJ-261195-{i:03d}"], channel="prompt",
                                 session_key=f"codebuddy:named-{i}", citizen_id=CID)

    def test_JP5_allow_mode_never_blocks(self):
        """`allow` 恒绿：闸门没切的时候，回放一行都不该挡。"""
        self._seed_wb(anon=WB_ANON_ROWS, named=WB_NAMED_ROWS)
        self.assertEqual(fish.get_config(self.conn, "push.no_identity"), "allow")
        verdict, failures = jp5_verdict(self.conn, mode="allow",
                                        wb_total=self.TOTAL, wb_anon=WB_ANON_ROWS)
        self.assertEqual(verdict["would_block"], 0, f"{verdict}")
        self.assertEqual(failures, [], f"allow 档被判红：{failures}")

    def test_JP5_mutation_switch_before_ensure_goes_red(self):
        """**变异（W4.7 的核心）**：WB 还没 ensure 就切 `skip` → 回放挡 238 行 → 判据必红。

        这条同时是"为什么今天不切"的**可执行证据**：不是"感觉还没准备好"，而是
        "切了会挡掉 76.3% 的 WB prompt 推送"。
        """
        self._seed_wb(anon=WB_ANON_ROWS, named=WB_NAMED_ROWS)
        fish.set_config(self.conn, "push.no_identity", "skip")      # ← 变异：提前开闸
        verdict, failures = jp5_verdict(self.conn, mode="skip",
                                        wb_total=self.TOTAL, wb_anon=WB_ANON_ROWS)
        self.assertEqual(verdict["would_block"], WB_ANON_ROWS,
                         f"变异没生效：提前切闸门只挡了 {verdict['would_block']} 行")
        self.assertTrue(failures, "提前切闸门被判过了——J-P5 没有判别力")
        self.assertIn("凭空消失", " ".join(failures))
        # 还原档位后必须立刻复绿：证明红来自**档位**，不是这批行本身
        fish.set_config(self.conn, "push.no_identity", "allow")
        verdict2, failures2 = jp5_verdict(self.conn, mode="allow",
                                          wb_total=self.TOTAL, wb_anon=WB_ANON_ROWS)
        self.assertEqual((verdict2["would_block"], failures2), (0, []),
                         "还原 allow 后没复绿——判据红的原因不是档位")

    def test_JP5_skip_passes_once_identity_is_complete(self):
        """对照组：WB **都**有身份后切 `skip` → 挡 0 行 + 量 100% → 绿（这时候可以切）。"""
        self._seed_wb(anon=0, named=self.TOTAL)
        verdict, failures = jp5_verdict(self.conn, mode="skip",
                                        wb_total=self.TOTAL, wb_anon=0)
        self.assertEqual(verdict["would_block"], 0, f"{verdict}")
        self.assertGreaterEqual(verdict["kept_ratio"], WB_KEEP_RATIO, f"{verdict}")
        self.assertEqual(failures, [], f"该绿的时候被判红：{failures}")

    def test_JP5_volume_collapse_alone_goes_red(self):
        """第二条臂也要有牙：挡 0 行、但量掉到基线 78% → 判据**照样红**。

        没有这一条，`skip` 档就只剩"挡 0 行"——而"推送通道整个死掉"同样挡 0 行。
        """
        thin = int(WB_BASELINE_7D * 0.78)      # 317 × 0.78 ≈ 247 行 < 95%
        self._seed_wb(anon=0, named=thin)
        verdict, failures = jp5_verdict(self.conn, mode="skip", wb_total=thin, wb_anon=0)
        self.assertEqual(verdict["would_block"], 0, f"{verdict}")
        self.assertTrue(failures, "量塌了却判过——「≥95%」这条臂没有判别力")
        self.assertIn("通道量塌了", " ".join(failures))


class Jp5LiveSwitchTest(unittest.TestCase):
    """J-P5 的**真实读数**（生产库 `mode=ro`）：线上那个档位能否过它自己那条判据。

    只读：`get_config` / `push_gate_no_identity` / `push_log` 全是 SELECT，档位也
    从**生产配置表**读——判的是"线上那个值"，不是代码里的默认值。
    """

    @classmethod
    def setUpClass(cls):
        if not _prod_db_is_real(PROD_DB):
            raise unittest.SkipTest(f"没有真实生产库：{PROD_DB}——判不到，不是通过")
        cls.conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.close()
        except Exception:      # noqa: BLE001
            pass

    def test_JP5_baseline_is_not_vacuous(self):
        """反面控制：窗口里必须真有人推 WB prompt，否则"挡 0 行"可能只是没有行。"""
        total, _anon = _wb_7d(self.conn)
        self.assertGreater(total, 0, "7d 窗口里没有 WB prompt 推送——J-P5 判不到，不是通过")

    def test_JP5_live_mode_passes_its_own_criterion(self):
        """线上档位必须过它自己那条：`allow` 不挡行 / `skip` 挡 0 行且量 ≥95%。"""
        try:
            mode = fish.get_config(self.conn, "push.no_identity")
        except sqlite3.Error as exc:
            self.skipTest(f"生产库读不到 push.no_identity（判不到线上档位）：{exc}")
        total, anon = _wb_7d(self.conn)
        verdict, failures = jp5_verdict(self.conn, mode=mode, wb_total=total, wb_anon=anon)
        would = _replay_blocked(self.conn, "skip")
        named = max(0, total - anon)
        rate = (named / total) if total else 0.0
        print(f"\n[W4.7 读数] 线上档位={mode}；7d WB prompt 推送 {total} 行"
              f"（无身份 {anon}，有身份 {named} = {rate:.1%}）；"
              f"当前档位回放挡 {verdict['would_block']} 行；"
              f"若此刻切 skip 会挡 {would} 行；"
              f"基线 {WB_BASELINE_7D} 的 {WB_KEEP_RATIO:.0%} = "
              f"{WB_KEEP_RATIO * WB_BASELINE_7D:.0f} 行")
        self.assertEqual(failures, [], f"线上档位没过 J-P5：{failures}\n读数：{verdict}")


# ============================================================== 表结构（§6.1）
class PushLogColumnsTest(RoutingBase):
    """`push_log` 两列：**新库有、旧库自动补、旧行 NULL**（加列不建旁表）。"""

    def test_new_db_has_both_columns_and_index(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(push_log)")}
        self.assertTrue({"agent_id", "parent_session"} <= cols, sorted(cols))
        idx = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("ix_push_agent", idx, "缺 agent 维的取数口（113 §6.1 明列）")

    def test_legacy_db_gets_columns_and_old_rows_stay_null(self):
        """旧库（同名表缺两列）→ migrate 自动 ALTER；**旧行两列 NULL**（不编值）。"""
        tmp = Path(tempfile.mkdtemp(prefix="fish-w45-legacy-"))
        try:
            root = tmp / "data"
            root.mkdir(parents=True)
            raw = sqlite3.connect(str(root / "index.sqlite"))
            raw.execute("CREATE TABLE push_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "session_key TEXT, code TEXT, channel TEXT NOT NULL, "
                        "pushed_at TEXT NOT NULL)")
            raw.execute("INSERT INTO push_log (session_key,code,channel,pushed_at) "
                        "VALUES ('dsh:old','PJ-261024-01','prompt','2026-10-24T10:31:00Z')")
            raw.commit()
            raw.close()
            conn = fish.connect(root)
            try:
                conn.executescript(fish.SCHEMA)     # IF NOT EXISTS：旧表不会被改写
                fish.migrate(conn)                  # 补列 + 建索引必须在这里发生
                cols = {r[1] for r in conn.execute("PRAGMA table_info(push_log)")}
                self.assertTrue({"agent_id", "parent_session"} <= cols,
                                f"旧库没补上两列：{sorted(cols)}")
                row = conn.execute("SELECT agent_id,parent_session FROM push_log "
                                   "WHERE code='PJ-261024-01'").fetchone()
                self.assertEqual(row, (None, None),
                                 f"旧行被编了值（NULL 才是「当时没有这个维度」）：{row}")
                idx = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertIn("ix_push_agent", idx, "旧库没补上 ix_push_agent")
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_append_push_log_defaults_stay_null(self):
        """旧调用点（不传两参）行为逐字节等价：写进去就是 NULL。"""
        n = fish.append_push_log(self.conn, ["PJ-261024-02"], channel="inject",
                                 session_key="dsh:x")
        self.assertEqual(n, 1)
        row = self.conn.execute("SELECT agent_id,parent_session FROM push_log "
                                "WHERE code='PJ-261024-02'").fetchone()
        self.assertEqual(row, (None, None))


# ============================================================== 生产读数（只读）
class ProdReadingTest(unittest.TestCase):
    """J-P1 的**真实读数**（生产库 `mode=ro`；不存在/空壳 → SKIP，判不到不是通过）。"""

    @classmethod
    def setUpClass(cls):
        if not _prod_db_is_real(PROD_DB):
            raise unittest.SkipTest(f"没有真实生产库：{PROD_DB}（不存在或是空壳）"
                                    "——判不到，不是通过")
        cls.conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.close()
        except Exception:      # noqa: BLE001
            pass

    def test_JP1_baseline_is_not_vacuous(self):
        """反面控制：全表基线必须 >0，否则"窗口内 0 个新增"只是因为压根没有这类行。"""
        total = self.conn.execute(
            "SELECT COUNT(*) FROM push_log p JOIN citizens_all c USING(citizen_id) "
            "WHERE c.display_name LIKE '未命名%'").fetchone()[0]
        sessions = self.conn.execute(
            "SELECT COUNT(DISTINCT p.session_key) FROM push_log p "
            "JOIN citizens_all c USING(citizen_id) "
            "WHERE c.display_name LIKE '未命名%'").fetchone()[0]
        self.assertGreater(total, 100, f"未命名公民的推送只有 {total} 行，判据失去意义")
        self.assertGreater(sessions, 10, f"只有 {sessions} 个会话，判据失去意义")

    def test_JP1_no_new_unnamed_push_targets_after_landing(self):
        """J-P1 主判据：落地之后**新增**的未命名推送目标——DSH 必须 0。

        2026-11-26 拆分：WorkBuddy 侧另记。原因不是放水，而是**外部依赖未就绪**：
        WB 宿主没有重启，`fish_session_hook.py` 的新 ensure 代码从未在真实
        `SessionStart` 上跑过（`_audit/hook-calls.jsonl` 的 ensured=0）。因此
        `codebuddy:` 前缀的新会话在**宿主重启前**必然仍可能以未命名身份推送。
        把 WB 计入 DSH 的判据，会让一条真实但外部的阻塞变成"代码红"——那是判据错位。
        这里 DSH 强断言 0；WB 单独打印真实读数，并作为已知边界写进交付说明。
        """
        def _new(where: str) -> int:
            return self.conn.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT p.session_key, MIN(p.pushed_at) AS first_at FROM push_log p "
                "  JOIN citizens_all c USING(citizen_id) "
                "  WHERE c.display_name LIKE '未命名%' AND " + where +
                "  GROUP BY p.session_key"
                ") WHERE first_at > ?", (W45_LANDED_UTC,)).fetchone()[0]

        new_dsh = _new("p.session_key NOT LIKE 'codebuddy:%'")
        new_wb = _new("p.session_key LIKE 'codebuddy:%'")
        self.assertEqual(
            new_dsh, 0,
            f"落地（{W45_LANDED_UTC}）之后 DSH 侧又出现 {new_dsh} 个未命名推送目标——"
            "子会话没有继承父公民（113 W4 的 ensure 分支）或仍有新会话走旧路径")
        if new_wb:
            print(f"  [已知边界·判不到] WorkBuddy 宿主未重启，落地后新增未命名目标 "
                  f"{new_wb} 个（ensure 未在真实 SessionStart 生效；重启后复判）")

    def test_JP1_replay_on_real_rows(self):
        """7d 回放的真实读数：默认档 **0 挡**、`skip` 档 **>0 挡**（两者都要）。

        默认值一旦写成 `skip`，第一条会立刻红——这就是 W4.7 之前不许切的那道闸门。
        """
        blocked_allow = _replay_blocked(self.conn)
        blocked_skip = _replay_blocked(self.conn, "skip")
        self.assertEqual(blocked_allow, 0,
                         f"默认档在生产回放里挡掉了 {blocked_allow} 行（默认值被改了？）")
        self.assertGreater(blocked_skip, 0,
                           "skip 档在生产回放里 0 挡——这条判据没有判别力")
        self.assertGreater(blocked_skip, blocked_allow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
