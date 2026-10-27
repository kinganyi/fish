#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约修复 A1（2026-10-26）：FB-04 / FB-07 / FB-09 / FB-10 的判据。

四条都是**文案与状态显示**的契约缺口。库里早就有真相（`expires_at` / `revoked_at` /
`citizens.is_owner` / `dormant`），缺的是把它如实说给读的人。所以每一条的判据都是
「造出那种状态 → 读输出文本」，而不是读库：

  FB-04  session_start 回执：永久 token 不再印出 `有效期至 None`
  FB-07  whoami 凭证状态：未提供 / 已吊销 / 永久 / 剩余 N 小时（向上取整、至少 1）
  FB-09  init：空库给两行下一步；已有主人时不吭声
  FB-10  recall 状态列：`[type/status+dormant]`，MCP 与 CLI 同口径 + 维度说明

## 最小运行（**不要**跑全量：603 条归 QA）

    & "C:\\Users\\15596\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" `
      -m unittest test_feedback_261026_contract -v

必须在 `fish-fix/p0` 目录下跑（`fish.py` 按相对路径加载，与其他测试一致）。
"""
import argparse
import contextlib
import importlib.util
import io
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server  # noqa: E402


class _ServerCase(unittest.TestCase):
    """共用夹具：一个 tempfile 根 + 一台 FishServer（测试产物只在临时目录里）。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.srv = mcp_server.FishServer(self.root)
        self.anon = self.srv._resolve_identity(None)

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def _issue(self, session_name, ttl_hours=None):
        """签一条凭证，返回 (明文 token, 回执文本)。

        两件事必须在这里做，否则夹具会骗人：

        ① **清掉宿主会话 env**。真实 MCP 进程里宿主把 `DSH_SESSION_ID` 之类 scrub 掉了，
           而测试进程带着它们；不清的话不同 `session_name` 会落到同一个
           `host_session_key` 上，"两条凭证 = 两个会话"这个前提就没了。
        ② `ttl_hours` 走 config（`session.token_ttl_hours`），不走签发参数——
           `start_session` 拿到的就是 None，即"按配置签发"这条真实路径。
        """
        drop = {v for _h, v in fish.HOST_SESSION_ENVS} | set(fish.SESSION_ALIAS_ENVS)
        clean = {k: v for k, v in os.environ.items() if k not in drop}
        if ttl_hours is not None:
            fish.set_config(self.srv.conn, "session.token_ttl_hours", int(ttl_hours))
        with mock.patch.dict(os.environ, clean, clear=True):
            out = self.srv.tool_session_start({"session_name": session_name}, self.anon)
        matched = re.search(r"token:\s*(fsh_\S+)", out)
        self.assertIsNotNone(matched, f"回执里应有 token：\n{out}")
        return matched.group(1), out


# ---------------------------------------------------------------- FB-04 回执
class FB04SessionStartReceiptTest(_ServerCase):
    """FB-04：`expires_at` 为空时，回执必须说人话，不许出现 `None`。"""

    def test_FB04_permanent_token_says_never_expires(self):
        """永久 token（默认 TTL=0）→ 含「永不过期」，且全文没有 `None`。"""
        token, out = self._issue("fb04-permanent")
        info = fish.lookup_session_token(self.srv.conn, token)
        self.assertIsNone(info["expires_at"], "默认签发的 token 本就不该有到期日")
        self.assertIn("永不过期（随会话 end 吊销）", out,
                      f"永久 token 要说清「永不到期 + 吊销出口」：\n{out}")
        self.assertNotIn("None", out, f"回执里出现 None = 把空值当日期印了：\n{out}")

    def test_FB04_expiring_token_keeps_raw_iso(self):
        """有到期时间 → 原样给 ISO（脚本要能解析，不做人话换算）。"""
        token, out = self._issue("fb04-expiring", ttl_hours=24)
        info = fish.lookup_session_token(self.srv.conn, token)
        self.assertIsNotNone(info["expires_at"], "TTL=24 应算出到期时刻")
        self.assertIn(f"有效期至 {info['expires_at']}", out,
                      f"到期时间要原样出现在回执里：\n{out}")
        self.assertNotIn("永不过期", out, "有到期日时不能说永不过期")
        self.assertNotIn("None", out)


# ------------------------------------------------------------ FB-07 凭证状态
class FB07WhoamiCredentialStateTest(_ServerCase):
    """FB-07：whoami 多一行「凭证状态」，四种状态各说各的。"""

    def test_FB07_no_token(self):
        """没带 token → 未提供凭证（不是"未核"两个字就打发）。"""
        out = self.srv.call_tool("whoami", {})
        self.assertIn("凭证状态：未提供凭证", out, out)

    def test_FB07_revoked(self):
        """已吊销 → 给出吊销时刻（AI 需要它来判断"是不是我自己刚轮换掉了"）。"""
        token, _out = self._issue("fb07-revoked")
        info = fish.lookup_session_token(self.srv.conn, token)
        fish.revoke_session_tokens(self.srv.conn, session_key=info["session_key"])
        self.srv.conn.commit()
        revoked_at = fish.lookup_session_token(self.srv.conn, token)["revoked_at"]
        self.assertIsNotNone(revoked_at)
        out = self.srv.call_tool("whoami", {"token": token})
        self.assertIn(f"凭证状态：已吊销（{revoked_at}）", out, out)
        self.assertIn("session_start", out, f"吊销后要给出换凭证的出口：\n{out}")

    def test_FB07_permanent(self):
        """永久有效 → 「永久（随会话 end 吊销）」。"""
        token, _out = self._issue("fb07-permanent")
        out = self.srv.call_tool("whoami", {"token": token})
        self.assertIn("凭证状态：永久（随会话 end 吊销）", out, out)

    def test_FB07_remaining_hours(self):
        """有到期日 → 「剩余 N 小时」，N 在 1..TTL 之间。"""
        token, _out = self._issue("fb07-hours", ttl_hours=24)
        out = self.srv.call_tool("whoami", {"token": token})
        matched = re.search(r"凭证状态：剩余 (\d+) 小时", out)
        self.assertIsNotNone(matched, f"应给出剩余小时数：\n{out}")
        hours = int(matched.group(1))
        self.assertGreaterEqual(hours, 1, "向上取整、至少 1（不许出现剩余 0 小时）")
        self.assertLessEqual(hours, 24, f"刚签的 24 小时凭证不该报 {hours} 小时")

    def test_FB07_ceil_math(self):
        """取整方向是向上：20 分钟 → 1 小时；1 小时整 → 1 小时；1 小时 1 秒 → 2 小时。

        2026-10-26（批 G · 评审 🟠1）：**已过期返回 0**。
        这条原来钉的是「已过期的时刻也至少报 1」——那正是被评审认定为 bug 的那一半：
        `max(1, …)` 把"过期 3 小时"念成「剩余 1 小时」，而同一回执的身份来源行写着
        「未归属」。判据跟着改，**强度不减**（仍是精确等值断言），只是把期望值
        从"伪造的 1"改成"如实的 0"；未过期那三条一个字没动。
        """
        now = "2026-10-26T04:29:11Z"
        self.assertEqual(fish.remaining_token_hours("2026-10-26T04:49:11Z", at=now), 1)
        self.assertEqual(fish.remaining_token_hours("2026-10-26T05:29:11Z", at=now), 1)
        self.assertEqual(fish.remaining_token_hours("2026-10-26T05:29:12Z", at=now), 2)
        # 已过期的时刻如实读成 0（"已过期"有单独一档提示，见 credential_state_line）
        self.assertEqual(fish.remaining_token_hours("2026-10-26T04:00:00Z", at=now), 0)


# ---------------------------------------------------------------- FB-09 init
class FB09InitNextStepTest(unittest.TestCase):
    """FB-09：`init` 结束时看 owner——没有就教下一步，有就闭嘴。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "data"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_init(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_init(argparse.Namespace(root=str(self.root)))
        return buf.getvalue()

    def _add_owner(self):
        conn = fish.connect(self.root)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,host,status,is_owner,"
            "created_at) VALUES ('CIT-OWNER-FB09','主人','resident','cli','active',1,?)",
            (fish.now_utc(),))
        conn.commit()
        conn.close()

    def test_FB09_empty_db_guides_next_steps(self):
        """空库：两行引导都要出现（主人怎么设、凭证怎么签）。"""
        out = self._run_init()
        self.assertIn("已初始化", out, out)
        self.assertIn("下一步：设置主人 → fish owner <citizen_id>", out, out)
        self.assertIn("签发凭证 → fish session start --project <项目>", out, out)

    def test_FB09_with_owner_stays_quiet(self):
        """已有主人：一个字都不多说（重复 init 是幂等补装，不是首次开箱）。"""
        self._run_init()          # 先建库
        self._add_owner()         # 再让库里出现主人
        out = self._run_init()    # 第二次 init
        self.assertIn("已初始化", out, out)
        self.assertNotIn("下一步：设置主人", out, f"已有主人不该再教设主人：\n{out}")
        self.assertNotIn("fish session start --project", out,
                         f"已有主人不该再教签凭证：\n{out}")


# --------------------------------------------------------------- FB-10 标签
class FB10RecallStatusTagTest(_ServerCase):
    """FB-10：状态列合并成 `[type/status+dormant]`，MCP 与 CLI 同一口径。"""

    def setUp(self):
        super().setUp()
        self.srv.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('FBX','反馈验证','',0,'active')")
        self.srv.conn.commit()
        self.code, _rel = fish.record_memory(
            self.srv.conn, self.root, project="FBX", title="休眠条目的状态列",
            body="用来验证 MCP 与 CLI 的状态列口径是否一致。",
            mem_type="fact", tags=[], visibility="shared", owner="unittest")
        self._mark_dormant()

    def _mark_dormant(self):
        """把条目置为休眠。**每次取值前都要重做**：`record_access` 会唤醒它
        （`dormant=0`），这正是"被召回唤醒"的语义——但判据要的是休眠态的那一眼。"""
        self.srv.conn.execute("UPDATE code_index SET dormant=1 WHERE code=?", (self.code,))
        self.srv.conn.commit()

    def _run_cli_recall(self):
        ns = argparse.Namespace(root=str(self.root), query=None, code=self.code,
                                project=None, tags=None, scope="all", limit=5)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_recall(ns)
        return buf.getvalue()

    def _run_mcp_recall(self):
        return self.srv.call_tool("recall", {"code": self.code, "limit": 5})

    def test_FB10_cli_merges_dormant_into_tag(self):
        """CLI：`[fact/active+dormant]`，不再有裸 ` (休眠)`，第一行是维度说明。"""
        self._mark_dormant()
        out = self._run_cli_recall()
        self.assertIn(f"[fact/active+dormant]", out, f"CLI 状态列应合并：\n{out}")
        self.assertNotIn("(休眠)", out, f"CLI 不许再有裸尾巴：\n{out}")
        self.assertEqual(out.splitlines()[0], fish.RECALL_DIMENSION_NOTE,
                         f"CLI 第一行应是维度说明：\n{out}")

    def test_FB10_mcp_merges_dormant_into_tag(self):
        """MCP：同口径——标签、说明、以及"第一行就是说明"。"""
        self._mark_dormant()
        out = self._run_mcp_recall()
        self.assertIn(f"[fact/active+dormant]", out, f"MCP 状态列应合并：\n{out}")
        self.assertNotIn("(休眠)", out, f"MCP 不许再有裸尾巴：\n{out}")
        self.assertEqual(out.splitlines()[0], fish.RECALL_DIMENSION_NOTE,
                         f"MCP 回包第一行应是维度说明：\n{out}")

    def test_FB10_cli_and_mcp_never_drift(self):
        """两处不许漂：同一状态渲染出的标签与说明逐字相同。"""
        self._mark_dormant()
        cli = self._run_cli_recall()
        self._mark_dormant()      # CLI 那次已把条目唤醒，取 MCP 口径前再置一次
        mcp = self._run_mcp_recall()
        cli_tag = re.search(r"\[fact/[^\]]+\]", cli)
        mcp_tag = re.search(r"\[fact/[^\]]+\]", mcp)
        self.assertIsNotNone(cli_tag, cli)
        self.assertIsNotNone(mcp_tag, mcp)
        self.assertEqual(cli_tag.group(0), mcp_tag.group(0),
                         "CLI 与 MCP 的状态列必须逐字一致")
        self.assertEqual(cli.splitlines()[0], mcp.splitlines()[0],
                         "CLI 与 MCP 的维度说明必须逐字一致")
        self.assertIn("两个维度", mcp.splitlines()[0])

    def test_FB10_active_entry_has_no_dormant_suffix(self):
        """活跃条目不许多挂一个 `+dormant`（合并不能变成"永远带上"）。"""
        self.srv.conn.execute("UPDATE code_index SET dormant=0 WHERE code=?", (self.code,))
        self.srv.conn.commit()
        out = self._run_mcp_recall()
        self.assertIn("[fact/active]", out, out)
        self.assertNotIn("+dormant", out, f"活跃条目不该带休眠标记：\n{out}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
