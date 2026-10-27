#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反馈修复 C 批（2026-10-26）：FB-03 身份来源声明与"双视图"告警。

## 病灶（`EVAL-261026-06` 原文）

同一个会话里：
  · `register` 回执说「role / 公民 A」；
  · 带 token 的 `inject` / `whoami` 说「visitor / 公民 B」；
  · **两边都不报错、不提醒**，而写入实际记在 B 名下。

也就是说，系统里同时存在"两套视图"，但它们各自看起来都完全正常。修的不是"哪一个错"，
而是**让分歧在回执里当场可见**：谁来源、谁归属、还有谁跟你同名/同会话/同账号。

## 四条判据（C1–C5）

C1 带 token 的 whoami 含 `身份来源：token（session_tokens）`；
   无 token 的 whoami 含 `身份来源：未归属`；inject 回执同口径两档。
   | 变异：把来源行写死为 token → 无 token 那档红（与 C4 共用同一处变异）
C2 register 回执含「不绑定会话」与「写入归属由 token 决定」两句，且带 token 时写明
   `当前 token → 公民 X`；无 token 时写 `unattributed`。
   | 变异：删掉边界句 → 红（本文件 test_C2 直接断言两句原文）
C3 造 1 个同名 role 公民 + 1 个带 token 的 visitor → whoami 出现
   `⚠ 检测到同名公民 <role id>（role，最近活跃 <ts>）——系统不合并；本次写入记在 <visitor id> 名下`
   | 变异：去掉同名告警 → 红（真源改 `_same_name_warnings` 一行，见交付报告）
C4 `whoami(all=true)` 列出 session / name / account 三种关联，按 id 去重，各带关联方式；
   不相关公民不出现；**无 token 时**给出「未列出」而不是"硬说 token"。
   | 变异：来源行写死为 token → 本判据的无凭证那一半红
C5 注入包/回执的既有格式不因新增行破坏（包尾仍是 `<!-- N/M token -->`、as-of 在它之前、
   铁律与根目录行仍在）；C1–C4 的既有行（登记身份/凭证/凭证状态/主人）也都在。

## 最小运行（**不要**跑全量：全量归 QA）

    & "C:\\Users\\15596\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" `
      -m unittest test_feedback_261026_c -v

必须在 `fish-fix/p0` 目录下跑（`fish.py` 按相对路径加载，与其他测试一致）。

受影响面的回归（同一轮里逐条跑）：
`test_identity_p1a` / `test_identity_p1b` / `test_identity_p1c` / `test_mcp` / `test_onboarding`。
"""
import importlib.util
import re
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server        # noqa: E402

SOURCE_RE = re.compile(r"^身份来源：(.+)$")
TOKEN_COMMENT_RE = re.compile(r"^<!-- (\d+)/(\d+) token -->$")
AS_OF_RE = re.compile(r"as-of (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")

TOKEN_SOURCE = "token（session_tokens）"
REGISTER_SOURCE = "register 登记（不参与归属）"
UNATTRIBUTED_SOURCE = "未归属"
RELATED_HEAD = "相关公民（**不含你自己**"


class _Case(unittest.TestCase):
    """共用夹具：一个 tempfile 根 + 一台真 FishServer（产物只在临时目录里）。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "data"
        self.srv = mcp_server.FishServer(self.root)
        self.conn = self.srv.conn

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    # ------------------------------------------------------------ 造数据
    def _citizen(self, name, *, kind="visitor", status="active", host="dsh",
                 session_key=None, account=None, last_seen=None, role=None):
        """直接插一行公民：这些判据要的是**库里的形态**，不是某条业务路径的副产物。"""
        cid = fish.ulid()
        stamp = last_seen or fish.now_utc()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at,"
            "is_owner,account_id,device_id,host_session_key,last_seen_at,archived_at) "
            "VALUES (?,?,?,?,?,?,?,0,?,NULL,?,?,NULL)",
            (cid, name, kind, role, host, status, stamp, account, session_key, stamp))
        self.conn.commit()
        return cid

    def _token(self, citizen_id, *, session_key="fish-test:sess"):
        res = fish.issue_session_token(
            self.conn, citizen_id=citizen_id, session_key=session_key,
            evidence="self-reported", channel="cli")
        self.conn.commit()
        return res["token"]

    # ------------------------------------------------------------ 读回执
    @staticmethod
    def _source_of(text):
        """取「身份来源」那一行（返回整行，便于逐字比对口径）。"""
        for line in text.splitlines():
            m = SOURCE_RE.match(line.strip())
            if m:
                return m.group(1)
        return None

    def _related_rows(self, text):
        """从 `all=true` 的回执里切出「相关公民」的条目前（没有段则返回 None）。"""
        lines = text.splitlines()
        start = next((i for i, ln in enumerate(lines) if ln.startswith(RELATED_HEAD)), None)
        if start is None:
            return None
        rows = []
        for line in lines[start + 1:]:
            if line.startswith("- ") and "｜" in line:
                rows.append(line)
            elif rows:
                break
        return rows

    @staticmethod
    def _row_id(row):
        return row[2:].split("｜", 1)[0].strip()


# ==================================================== C1 身份来源声明
class C1IdentitySourceTest(_Case):
    """C1：whoami / inject 回执必须写明本次身份来自哪里。"""

    def test_C1_whoami_declares_token_vs_unattributed(self):
        anon = self.srv.call_tool("whoami", {})
        self.assertEqual(self._source_of(anon), UNATTRIBUTED_SOURCE,
                         f"无凭证的 whoami 必须写「身份来源：未归属」；实际回执：\n{anon}")
        self.assertNotIn("身份来源：token", anon,
                         "没带凭证时说成 token 就是在编造归属来源")

        cid = self._citizen("C1-本人")
        token = self._token(cid)
        out = self.srv.call_tool("whoami", {"token": token})
        self.assertEqual(self._source_of(out), TOKEN_SOURCE,
                         f"带有效 token 的 whoami 必须写「身份来源：token」；实际：\n{out}")
        # 来源行与身份行必须**同时**在：只报名字不报来源，正是本次要治的病。
        self.assertIn(f"公民 {cid}", out)
        self.assertIn("身份来源：", out)

    def test_C1_inject_receipt_declares_source_too(self):
        cid = self._citizen("C1-注入")
        token = self._token(cid)
        with_token = self.srv.call_tool("inject", {"token": token})
        self.assertEqual(self._source_of(with_token), TOKEN_SOURCE,
                         f"inject 回执也要写来源；实际尾部：\n{with_token[-400:]}")
        without = self.srv.call_tool("inject", {})
        self.assertEqual(self._source_of(without), UNATTRIBUTED_SOURCE,
                         f"无 token 的 inject 必须写未归属；实际尾部：\n{without[-400:]}")


# ==================================================== C2 register 的边界
class C2RegisterBoundaryTest(_Case):
    """C2：register 回执把"只登记公民、不绑定会话"写在明面上。"""

    def test_C2_receipt_states_boundary_without_token(self):
        out = self.srv.call_tool("register", {"display_name": "C2-无凭证公民"})
        self.assertEqual(self._source_of(out), REGISTER_SOURCE,
                         f"register 回执的来源必须是「register 登记（不参与归属）」；"
                         f"实际：\n{out}")
        self.assertIn("不绑定会话", out, "边界句缺失：读者会以为 register 绑定了本会话")
        self.assertIn("写入归属由 token 决定", out,
                      "必须把'归属由谁决定'写出来（否则误解原封不动）")
        self.assertIn("unattributed", out, "没凭证时要写明归属会落到 unattributed")

    def test_C2_receipt_names_current_token_citizen(self):
        mine = self._citizen("C2-持证人")
        token = self._token(mine)
        out = self.srv.call_tool("register", {"display_name": "C2-另一个人", "token": token})
        self.assertEqual(self._source_of(out), TOKEN_SOURCE,
                         f"带有效 token 时来源是 token；实际：\n{out}")
        self.assertIn(f"当前 token → 公民 {mine}", out,
                      "必须写出**当前 token 指向谁**（它常常不是刚登记的那个）")
        self.assertIn("不绑定会话", out, "边界句是无条件的（名同名不同都要有）")


# ==================================================== C3 同名告警
class C3SameNameWarningTest(_Case):
    """C3：带 token 的 whoami 发现同名活公民 → 告警 + 说清写入落点。"""

    def test_C3_warns_and_names_write_target(self):
        sibling = self._citizen("双视图-同名", kind="role",
                                last_seen="2026-10-25T01:02:03Z")
        mine = self._citizen("双视图-同名", kind="visitor")
        token = self._token(mine)

        out = self.srv.call_tool("whoami", {"token": token})
        self.assertIn(f"⚠ 检测到同名公民 {sibling}", out,
                      f"同名活公民必须被报出来；实际回执：\n{out}")
        self.assertIn("（role，最近活跃 2026-10-25T01:02:03Z）", out,
                      f"告警要带形态与最近活跃时刻；实际：\n{out}")
        self.assertIn("——系统不合并；", out, "必须说清系统**不会**自动合并")
        self.assertIn(f"本次写入记在 {mine} 名下", out,
                      f"告警必须给落点（否则读者仍不知道写进去算谁的）：\n{out}")
        self.assertNotIn(f"本次写入记在 {sibling} 名下", out,
                         "落点写错人了：写入归属只由 token 决定")

    def test_C3_unique_name_stays_quiet(self):
        cid = self._citizen("C3-独名")
        token = self._token(cid)
        out = self.srv.call_tool("whoami", {"token": token})
        self.assertNotIn("检测到同名公民", out,
                         f"没有同名公民时不许无中生有（噪声会让人忽略真告警）：\n{out}")
        # 同名但**已退役**的：不构成"另一套在用的身份"（告警只认活跃公民）
        self._citizen("C3-独名", status="retired", kind="role")
        out2 = self.srv.call_tool("whoami", {"token": token})
        self.assertNotIn("检测到同名公民", out2,
                         f"退役的同名公民不该触发告警；实际：\n{out2}")


# ==================================================== C4 相关公民清单
class C4WhoamiAllTest(_Case):
    """C4：`all=true` 按 会话/同名/账号 三锚列相关公民，去重且不全库 dump。"""

    def setUp(self):
        super().setUp()
        self.me = self._citizen("C4-self", session_key="sess-c4", account="acct-c4")
        self.token = self._token(self.me)
        self.mate = self._citizen("C4-session-mate", session_key="sess-c4",
                                  account="acct-other")
        self.named = self._citizen("C4-self", session_key="sess-x", account="acct-y")
        self.both = self._citizen("C4-self", session_key="sess-x2", account="acct-c4")
        self.acct = self._citizen("C4-account-mate", session_key="sess-z",
                                  account="acct-c4")
        self.stranger = self._citizen("C4-stranger", session_key="sess-q",
                                      account="acct-q")

    def test_C4_lists_three_relations_deduped(self):
        out = self.srv.call_tool("whoami", {"token": self.token, "all": True})
        rows = self._related_rows(out)
        self.assertIsNotNone(rows, f"all=true 必须有「相关公民」段；实际回执：\n{out}")
        ids = [self._row_id(r) for r in rows]
        self.assertEqual(sorted(ids), sorted([self.mate, self.named, self.both, self.acct]),
                         f"应恰好列出 4 个相关公民（不含自己）；实际 {ids}")
        self.assertEqual(len(ids), len(set(ids)), f"同一个公民被列了两次：{ids}")
        self.assertNotIn(self.me, ids, "自己不该出现在「相关公民」里（上面已经报过了）")
        self.assertNotIn(self.stranger, out, "不相关公民不许出现——这不是全库 dump")

        by_id = {self._row_id(r): r for r in rows}
        self.assertIn("关联：session", by_id[self.mate],
                      f"同会话键 → 关联方式应为 session；实际：{by_id[self.mate]}")
        self.assertIn("关联：name", by_id[self.named],
                      f"同名 → 关联方式应为 name；实际：{by_id[self.named]}")
        self.assertIn("关联：name/account", by_id[self.both],
                      f"同名+同账号应合并成一条并标双关联；实际：{by_id[self.both]}")
        self.assertIn("关联：account", by_id[self.acct],
                      f"同账号 → 关联方式应为 account；实际：{by_id[self.acct]}")
        for row in rows:
            self.assertIn("｜", row, f"行格式应为 分栏；实际：{row}")
            self.assertIn("宿主 dsh", row, f"每行要给 host；实际：{row}")
            self.assertIn("最近活跃 ", row, f"每行要给 last_seen；实际：{row}")
            self.assertIn("token ", row, f"每行要给 token 数；实际：{row}")

    def test_C4_token_count_ignores_revoked(self):
        """token 数只数**未吊销**凭证：一条吊销的 token 说明不了"它还活着"。"""
        killed = fish.issue_session_token(
            self.conn, citizen_id=self.mate, session_key="sess-c4-dead",
            evidence="self-reported", channel="cli")
        self.conn.commit()
        fish.revoke_session_tokens(self.conn, token_hash=killed["token_hash"])
        self.conn.commit()
        alive = self._token(self.mate, session_key="sess-c4-alive")
        self.assertTrue(alive)
        out = self.srv.call_tool("whoami", {"token": self.token, "all": True})
        rows = {self._row_id(r): r for r in self._related_rows(out)}
        self.assertIn("token 1｜", rows[self.mate],
                      f"两条凭证吊销一条 → 应报 1；实际：{rows[self.mate]}")

    def test_C4_without_token_says_not_listed(self):
        """无凭证时不仅不列，还要说清"不是库里没有人"——这就是变异②的红点。"""
        out = self.srv.call_tool("whoami", {"all": True})
        self.assertEqual(self._source_of(out), UNATTRIBUTED_SOURCE,
                         f"无凭证时来源必须是未归属；实际：\n{out}")
        rows = self._related_rows(out)
        self.assertEqual(rows, [], f"没有锚就一条都不许列；实际 {rows}")
        self.assertIn("未列出", out, f"要显式说「未列出」并给原因；实际：\n{out}")
        self.assertNotIn(self.mate, out, "没有锚时不许把别人列出来")

    def test_C4_all_false_keeps_old_receipt(self):
        """不传 all = 一个字都不多（旧调用与旧判据不受影响）。"""
        out = self.srv.call_tool("whoami", {"token": self.token})
        self.assertIsNone(self._related_rows(out), f"默认不该出现相关公民段：\n{out}")


# ==================================================== C5 格式不回归
class C5FormatPreservedTest(_Case):
    """C5：新增行不许破坏注入包/回执的既有格式。"""

    def test_C5_inject_tail_format_is_intact(self):
        cid = self._citizen("C5-本人")
        token = self._token(cid)
        out = self.srv.call_tool("inject", {"token": token})
        last = out.rstrip("\n").splitlines()[-1]
        self.assertRegex(last, TOKEN_COMMENT_RE,
                         f"包尾仍是 `<!-- N/M token -->`（下游按它解析预算）；实际：{last!r}")
        self.assertIn("三条铁律", out, "L0 铁律行不许被新行挤掉")
        self.assertIn("记忆根目录", out, "L0 根目录行不许被新行挤掉")
        self.assertRegex(out, r"(本次截断：|本次无截断)", "FB-02 的截断声明仍在")
        m = AS_OF_RE.search(out)
        self.assertIsNotNone(m, "FB-16 的 as-of 行仍在")
        i_source = out.find("身份来源：")
        i_comment = out.find("<!--")
        self.assertGreater(i_source, 0, "来源行必须在")
        self.assertLess(i_source, i_comment,
                        "来源行属于正文，必须排在 tokens/budget 元信息注释之前")

    def test_C5_whoami_legacy_lines_still_present(self):
        cid = self._citizen("C5-whoami")
        token = self._token(cid)
        out = self.srv.call_tool("whoami", {"token": token})
        self.assertTrue(out.startswith("登记身份："),
                        f"whoami 首行仍是登记身份；实际：{out.splitlines()[0]!r}")
        for needle in ("凭证：会话 ", "凭证状态：", "身份来源：", "主人：", "与你的宿主身份不冲突"):
            self.assertIn(needle, out, f"whoami 的既有行被弄丢了：{needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
