#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批 G：按独立评审报告（对 `c236401` 的 IS_PASS: NO）逐条闭合的判据（2026-10-26）。

每个类对应评审报告里的**一条编号**，判据名带同一编号，便于回查：

  | 编号 | 现象 | 本文件的判据 |
  |---|---|---|
  | 🟠1 | 过期未吊销凭证被念成「剩余 1 小时」，与同回执的「未归属」互相矛盾 | `G1CredentialExpiredTest` |
  | 🟠2 | 包尾自解释两处说假话（L2 被裁仍称「无截断」；`sections=["status"]` 仍称含 L2/候选） | `G2InjectionTailTest` |
  | 🟠3 | CLI 面缺 `whoami --all`、`inject --sections/--no-board`，三处漂移 | `G3CliParityTest` |
  | 🟡5 | ④注意 / ⑤错题本 固定条数却不声明 N/M | `G5SectionHeadTest` |
  | 🟡6 | FB-12 归档接线没有能失败的判据（删掉 `main` 那行仍全绿） | `G6DailyReportWiringTest` |
  | 🟡7 | FB-14 缺端到端判据（D 批直接给 classification 塞 project，绕过 cwd/路径解析） | `G7ActivityToBoardTest` |
  | 🟡8 | `fish distill --days -1` 退出码 0（`return 2` 没被 `main()` 传播） | `G8DistillExitTest` |
  | 🟡9 | 标签过滤 `LIKE '%"t"%'`：`a_b` 会命中 `axb` | `G9TagsLikeTest` |
  | ⚪11 | 没给 tags 也提示「去掉 --tags」 | `G11TagsHintTest` |
  | ⚪12 | 非标准 `expires_at` 让 whoami 抛 ValueError | `G12BadExpiresAtTest` |
  | ⚪13 | `p0/_audit/` 等未 ignore | `G13GitignoreTest` |
  | ⚪15 | 节标题「在动但未签到」语义偏一档（实为"未在本板登记"） | `G15UncheckedHeadingTest` |

## 每条判据为什么这么读

- **读印出来的文本**，不读实现里的中间变量：回执上写什么，读者就信什么。凡"文案类"
  反馈（🟠1/🟠2/⚪11/⚪15）都以 stdout / MCP 回包为唯一真值。
- **不拿实现自证实现**：🟡9 造 `a_b` / `axb` / `a%b` 三条真实记忆再查；🟡7 造真实
  payload 走 `handle_post_tool_use` 再读看板文件，都不直接给分类结果塞 project。
- **两端一致**（🟠1/🟠3）：CLI 与 MCP 的同一行要**逐字相等**，比"各自含关键词"硬。

## 最小运行（**不要**跑全量）

    & "C:\\Users\\15596\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" `
      -m unittest test_feedback_261026_g -v

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

import fish_naming      # noqa: E402 —— 会话短标识（人类面唯一取法，F2 契约）
import fish_activity     # noqa: E402
import fish_surface      # noqa: E402
import mcp_server        # noqa: E402

CODE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*-\d{6}-\d+\b")
SHOW_RE = re.compile(r"显示 (\d+)/(\d+) 条")


def _z_ago(days=0, minutes=0) -> str:
    """`days` 天 / `minutes` 分钟前的 UTC ISO（与库里 `recorded_at` 同一口径）。"""
    moment = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days, minutes=minutes))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _line_with(text, prefix):
    """取第一条以 `prefix` 开头的行（找不到返回 ""）。"""
    for line in (text or "").splitlines():
        if line.strip().startswith(prefix):
            return line.strip()
    return ""


def _line_containing(text, needle):
    """取第一条**含** `needle` 的行（日报的回执行带时间戳前缀，不能用 startswith）。"""
    for line in (text or "").splitlines():
        if needle in line:
            return line.strip()
    return ""


class _TempRoot(unittest.TestCase):
    """一个临时根 + 一个 `mcp_server.FishServer`（临时库，绝不碰生产）。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="fish-g-"))
        self.root = self.base / "data"
        self.srv = mcp_server.FishServer(self.root)
        self.conn = self.srv.conn

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:               # noqa: BLE001 —— 收尾不该掩盖断言失败
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    # ------------------------------------------------------------------ 夹具
    def _citizen(self, name, *, host="cli", is_owner=0):
        cid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
            "created_at,is_owner) VALUES (?,?,?,?,?,?,?,?)",
            (cid, name, "resident" if is_owner else "visitor", None, host,
             "active", fish.now_utc(), is_owner))
        self.conn.commit()
        return cid

    def _project(self, prefix):
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
            "status) VALUES (?,?,NULL,0,'active')", (prefix, prefix))
        self.conn.commit()
        return prefix


# ============================================================ 🟠1 凭证过期档
class G1CredentialExpiredTest(_TempRoot):
    """🟠1：过期**未吊销**的凭证必须被说成「已过期」，且不许再出现「剩余 N 小时」。

    现场（评审原文）：`expires_at` 是过去时、`revoked_at` 为 NULL 的 token，whoami 说
    「剩余 1 小时」——而同一份回执的「身份来源」行写着「未归属」，写入实际被记成
    unattributed。两句话互相矛盾，读的人会相信更具体的那句（"还能用一小时"）。
    """

    def _token(self, citizen_id, *, expires_at=None, session_key="fish-g:g1"):
        res = fish.issue_session_token(
            self.conn, citizen_id=citizen_id, session_key=session_key,
            evidence="machine", channel="cli", ttl_hours=24)
        self.conn.commit()
        if expires_at is not None:
            self.conn.execute(
                "UPDATE session_tokens SET expires_at=? WHERE token_hash=?",
                (expires_at, res["token_hash"]))
            self.conn.commit()
            res["expires_at"] = expires_at
        return res

    def test_G1_expired_token_says_expired_and_never_remaining(self):
        """过期 → 出现「已过期」，且**同一行**里不再出现「剩余」。"""
        cid = self._citizen("G1-过期持证人")
        past = "2026-10-01T00:00:00Z"
        res = self._token(cid, expires_at=past)

        out = self.srv.call_tool("whoami", {"token": res["token"]})
        cred = _line_with(out, "凭证状态：")
        self.assertIn("已过期", cred, f"过期凭证必须单列一档：\n{out}")
        self.assertIn(past, cred, f"到期时刻要印出来（可核对，不用猜）：\n{out}")
        self.assertIn("session_start", cred, "过期后唯一出口是重签，要把出口写上")
        self.assertNotIn("剩余", cred, f"已过期**不许**再报「剩余 N 小时」（伪造 1 小时）：\n{out}")

        # 同一回执不许自相矛盾：凭证已过期 → 身份来源必须是「未归属」。
        self.assertIn("身份来源：未归属", out,
                      f"凭证无效时身份来源不能还声称是 token：\n{out}")
        # 写入侧的口径（真实后果）：这条 token 解不出公民。
        self.assertIsNone(fish.validate_session_token(self.conn, res["token"]))

    def test_G1_remaining_hours_returns_zero_for_past(self):
        """底层读数：已过期返回 0（旧实现 `max(1, …)` 会伪造 1 小时）。"""
        now = "2026-10-26T04:29:11Z"
        self.assertEqual(fish.remaining_token_hours("2026-10-26T04:00:00Z", at=now), 0,
                         "已过期的时刻必须读成 0，不许再被 max(1, …) 兜成 1")
        # 未过期的取整方向不变（这条是既有契约，改档位不许连它一起动）
        self.assertEqual(fish.remaining_token_hours("2026-10-26T04:49:11Z", at=now), 1)
        self.assertEqual(fish.remaining_token_hours("2026-10-26T05:29:11Z", at=now), 1)
        self.assertEqual(fish.remaining_token_hours("2026-10-26T05:29:12Z", at=now), 2)

    def test_G1_credential_line_and_cli_match_mcp_word_for_word(self):
        """CLI 与 MCP 的「凭证状态」「身份来源」两行**逐字相等**（过期档也一样）。"""
        cid = self._citizen("G1-双入口")
        res = self._token(cid, expires_at="2026-10-01T00:00:00Z")

        mcp = self.srv.call_tool("whoami", {"token": res["token"]})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_whoami(argparse.Namespace(
                root=str(self.root), citizen=None, token=res["token"], all=False))
        cli = buf.getvalue()

        for prefix in ("凭证状态：", "身份来源："):
            mcp_line = _line_with(mcp, prefix)
            cli_line = _line_with(cli, prefix)
            self.assertTrue(mcp_line, f"MCP 缺「{prefix}」行")
            self.assertEqual(cli_line, mcp_line,
                             f"CLI 与 MCP 的「{prefix}」行必须逐字相同（F 批既有口径）")
        self.assertIn("已过期", _line_with(cli, "凭证状态："))


# ====================================================== ⚪12 坏 expires_at
class G12BadExpiresAtTest(_TempRoot):
    """⚪12：非标准 `expires_at` 不许把 whoami 弄崩（探针崩掉的代价最高）。"""

    def test_G12_bad_expires_does_not_raise_and_prints_raw_value(self):
        cid = self._citizen("G12-坏数据")
        bad = "not-a-timestamp"
        res = fish.issue_session_token(
            self.conn, citizen_id=cid, session_key="fish-g:g12",
            evidence="machine", channel="cli", ttl_hours=24)
        self.conn.execute("UPDATE session_tokens SET expires_at=? WHERE token_hash=?",
                          (bad, res["token_hash"]))
        self.conn.commit()

        # ① 不抛：措辞层必须兜住（`lookup_session_token` 会把坏值原样带出来）
        line = fish.credential_state_line(self.conn, res["token"])
        self.assertIn("无法解析", line, f"坏数据要如实说出来，而不是抛：{line!r}")
        self.assertIn(bad, line, "原始值要印出来（否则无从排查）")

        # ② 整条 whoami 也不崩（MCP 与 CLI 两端）
        out = self.srv.call_tool("whoami", {"token": res["token"]})
        self.assertIn("无法解析", _line_with(out, "凭证状态："), out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_whoami(argparse.Namespace(
                root=str(self.root), citizen=None, token=res["token"], all=False))
        self.assertIn("无法解析", buf.getvalue(), "CLI 同样不许崩")

        # ③ 底层仍要**响亮**：坏数据不能连读数函数一起吞掉（判据要能看见它）
        with self.assertRaises(ValueError):
            fish.remaining_token_hours(bad)


# ================================================= 🟠2 注入包尾的自解释
class G2InjectionTailTest(_TempRoot):
    """🟠2：包尾两行必须与包体一致——裁了就报，没要的段就不许声称含它。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("G2")
        self.cid = self._citizen("G2-行动者")
        fish.record_memory(self.conn, self.root, project=self.prefix,
                           title="G2 注入包测试记忆", body="正文占位。", mem_type="fact")

    def _pack(self, **kw):
        kw.setdefault("project", self.prefix)
        return fish.build_injection_pack(self.conn, self.root, self.cid, **kw)

    @staticmethod
    def _tail(pack):
        """包尾两行（最后两行：截断声明 + as-of）。"""
        lines = [ln for ln in pack.strip().splitlines() if ln.strip()]
        return lines[-2], lines[-1]

    def test_G2_l2_trim_reports_truncation(self):
        """L2 被预算裁掉 → 包尾**必须**说截断，且与 `meta["truncated"]` 同源同值。"""
        hit = None
        for budget in (1200, 900, 700, 600, 500, 450, 400, 350, 300, 250, 200, 150):
            pack, meta = self._pack(budget=budget)
            if meta["truncated"]["l2"]:
                hit = (budget, pack, meta)
                break
        self.assertIsNotNone(hit, "找不到把 L2 整段裁掉的预算，判据失去靶点（请检查 l2_built）")
        budget, pack, meta = hit
        first, _second = self._tail(pack)
        self.assertIn("本次截断", first, f"budget={budget} 时 L2 已被裁，尾行却说没截断：\n{first}")
        self.assertIn("L2", first, f"被裁的是 L2，尾行要点名：\n{first}")
        self.assertEqual(first, fish.announce_truncation(meta["truncated"]),
                         "尾行与 meta 必须同一份计算（两处各算一份 = 读者不知道信谁）")

    def test_G2_sections_status_does_not_claim_l2_or_candidates(self):
        """`sections=["status"]` → 尾行不许声称含 L2 / 候选 / 看板。"""
        pack, meta = self._pack(sections=["status"])
        _first, second = self._tail(pack)
        self.assertEqual(meta["sections"], ["status"])
        self.assertIn("本包含 L0 身份", second, second)
        for forbidden in ("L0–L2", "L2", "候选", "看板"):
            self.assertNotIn(forbidden, second,
                             f"包里没有「{forbidden}」，尾行不许声称有：\n{second}")
        # 包体也要真的没有这些段（尾行说的是实话，不是话术）
        self.assertNotIn("可查项目：", pack, "L2 段不该在包里")
        self.assertNotIn("看板（", pack, "看板段不该在包里")
        self.assertNotIn("规矩段：", pack, "规矩段不该在包里")

    def test_G2_full_pack_keeps_legacy_phrase(self):
        """全段（什么都没丢）时仍是旧措辞——不把健康包说得比实际更残缺。"""
        pack, meta = self._pack()
        self.assertEqual(meta["sections"], sorted(fish.INJECTION_SECTIONS))
        _first, second = self._tail(pack)
        self.assertIn("本包含 L0–L2 与候选", second, second)

    def test_G2_reserve_covers_actual_tail(self):
        """预算预留的长度必须 ≥ 实际包尾（否则"裁到 N"之后静默超预算）。"""
        worst = fish.count_tokens(fish.injection_tail_worst())
        cases = [self._pack(), self._pack(sections=["status"]),
                 self._pack(sections=["push", "index"]),
                 self._pack(budget=400), self._pack(budget=150)]
        for pack, meta in cases:
            _first, second = self._tail(pack)
            actual = fish.count_tokens(_first + "\n" + second)
            self.assertLessEqual(actual, worst,
                                 f"实际包尾 {actual} > 预留 {worst}（sections={meta['sections']}）")


# ==================================================== 🟠3 CLI 面与 MCP 一致
class G3CliParityTest(_TempRoot):
    """🟠3：CLI 补上 MCP 已有的三个旋钮，且渲染同源。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("G3")
        self.me = self._citizen("G3-同名者", host="dsh")
        self.other = self._citizen("G3-同名者", host="zcode")
        fish.record_memory(self.conn, self.root, project=self.prefix,
                           title="G3 参考记忆", body="正文占位。", mem_type="fact")
        res = fish.issue_session_token(
            self.conn, citizen_id=self.me, session_key="fish-g:g3",
            evidence="machine", channel="cli", ttl_hours=24)
        self.conn.commit()
        self.token = res["token"]

    # ------------------------------------------------------------------ 工具
    def _cli_whoami(self, **kw):
        args = argparse.Namespace(root=str(self.root), citizen=None,
                                  token=self.token, all=False)
        for key, value in kw.items():
            setattr(args, key, value)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_whoami(args)
        return buf.getvalue()

    def _cli_inject(self, **kw):
        args = argparse.Namespace(root=str(self.root), citizen=self.me, budget=None,
                                  project=self.prefix, cwd=None, sections=None,
                                  no_board=False, auto_presence=False, json=True)
        for key, value in kw.items():
            setattr(args, key, value)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_inject(args)
        return json.loads(buf.getvalue())

    @staticmethod
    def _related_block(text):
        """从回执里切出「相关公民」段的原始行（含段头；用于两端逐字比对）。"""
        out, cur = [], False
        for line in (text or "").splitlines():
            if line.startswith("相关公民（"):
                cur = True
                out.append(line)
                continue
            if cur:
                if line.startswith("- ") or line.startswith("（"):
                    out.append(line)
                else:
                    break
        return out

    # ------------------------------------------------------------------ 判据
    def test_G3_cli_whoami_all_lists_related_like_mcp(self):
        """`whoami --all`：列相关公民、标关联方式，且与 MCP 的段落**逐字一致**。"""
        cli = self._cli_whoami(all=True)
        mcp = self.srv.call_tool("whoami", {"token": self.token, "all": True})

        cli_block = self._related_block(cli)
        mcp_block = self._related_block(mcp)
        self.assertTrue(cli_block, f"CLI 必须有「相关公民」段：\n{cli}")
        self.assertEqual(cli_block, mcp_block,
                         "CLI 与 MCP 的相关公民清单必须逐字相同（同一份渲染）")
        joined = "\n".join(cli_block)
        self.assertIn(self.other, joined, "同名兄弟要被列出来")
        self.assertIn("关联：", joined, "每条要标关联方式（session/name/account）")
        self.assertNotIn(self.me, joined, "自己不该出现在「相关公民」里")

        # 不给 --all 时一个字都不多（默认行为不变）
        self.assertEqual(self._related_block(self._cli_whoami()), [])

    def test_G3_cli_inject_sections_and_no_board(self):
        """`inject --sections` / `--no-board`：与 MCP 的 `sections` / `include_board` 同口径。"""
        # ① 同口径：CLI 的字符串形态与 MCP 的数组形态归一结果必须相等
        self.assertEqual(fish.normalize_inject_sections_arg("status,board"),
                         mcp_server._inject_sections_arg(
                             {"sections": ["status", "board"]}))
        self.assertEqual(fish.normalize_inject_sections_arg(None, False),
                         mcp_server._inject_sections_arg({"include_board": False}))

        # ② `--sections status`：包里没有看板/规矩/索引/候选
        only_status = self._cli_inject(sections="status")
        self.assertEqual(only_status["sections"], ["status"])
        pack = only_status["pack"]
        for header in ("看板（", "规矩段：", "可查项目：", "该注意什么：", "错题本："):
            self.assertNotIn(header, pack, f"--sections status 不该含「{header}」：\n{pack}")
        self.assertIn("登记身份", pack, "L0 身份段永远保留")
        self.assertIn("本包含 L0 身份", pack, "尾行要如实说包里有哪几段")

        # ③ `--no-board`：段列表里没有 board，包里没有看板块
        no_board = self._cli_inject(no_board=True)
        self.assertNotIn("board", no_board["sections"])
        self.assertNotIn("看板（", no_board["pack"], "说了不要看板，包里就不许有看板")
        self.assertEqual(len(no_board["sections"]), len(fish.INJECTION_SECTIONS) - 1,
                         "--no-board 只去掉 board 一段，其余段照旧")

        # ④ 拼错的段名要响亮失败（不许静默少一段）
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self._cli_inject(sections="status,nosuch")
        self.assertEqual(ctx.exception.code, 2, "未知段名 = 参数错误")


# ================================================= 🟡5 ④⑤ 的 N/M 段头
class G5SectionHeadTest(_TempRoot):
    """🟡5：④注意 / ⑤错题本 与 ③规矩 同款「显示 N/M 条」段头。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("G5")
        self.owner = self._citizen("G5-作者")
        for i, title in enumerate(("未闭合：甲", "未闭合：乙", "下一步：丙"), start=1):
            self._mem(f"G5-261026-{i:02d}", title, recorded_at=_z_ago(minutes=30 - i))
        for i, title in enumerate(("教训甲", "教训乙", "教训丙"), start=4):
            self._mem(f"G5-261026-{i:02d}", title, mem_type="lesson",
                      recorded_at=_z_ago(minutes=20 - i))

    def _mem(self, code, title, *, mem_type="fact", recorded_at=None):
        ts = recorded_at or fish.now_utc()
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared','active','[]',NULL,?,?,0,0)",
            (code, title, self.prefix, self.owner, mem_type, ts, ts))
        self.conn.commit()

    def test_G5_attention_head_declares_shown_and_total(self):
        """④ 段头：显示 2/3 条（每类最多 2 条）——固定条数必须声明自己不全。"""
        blk = fish.build_attention_block(self.conn, self.root, citizen_id=self.owner,
                                         session_actor="g5-sess", project=self.prefix)
        head = blk["text"].splitlines()[0]
        self.assertTrue(head.startswith("该注意什么："), head)
        matched = SHOW_RE.search(head)
        self.assertIsNotNone(matched, f"④ 段头缺「显示 N/M 条」：{head}")
        shown, total = int(matched.group(1)), int(matched.group(2))
        self.assertEqual(shown, 2, f"每类最多 2 条 → 只可能印 2 条：{head}")
        self.assertEqual(total, 3, f"M 必须是窗口内的真实条数（3 条未闭合）：{head}")
        self.assertIn("每类最多 2 条", head, "没给 N/M 的时候至少要有固定上限声明")
        self.assertEqual(len(blk["codes"]), shown, "段头说的 N 必须等于真的印了几条")

    def test_G5_lesson_head_declares_shown_and_total(self):
        """⑤ 段头：显示 2/3 条（同项目优先 + 每类最多 2 条）。"""
        blk = fish.build_lesson_block(self.conn, project=self.prefix)
        head = blk["text"].splitlines()[0]
        self.assertTrue(head.startswith("错题本："), head)
        matched = SHOW_RE.search(head)
        self.assertIsNotNone(matched, f"⑤ 段头缺「显示 N/M 条」：{head}")
        self.assertEqual((int(matched.group(1)), int(matched.group(2))), (2, 3), head)
        self.assertIn("每类最多 2 条", head)
        self.assertIn("同项目优先", head, "补齐口径也要写在段头（读者要能判断这批坑的代表性）")
        self.assertEqual(len(blk["codes"]), 2)


# ============================================ 🟡6 日报里的归档接线（e2e）
class G6DailyReportWiringTest(unittest.TestCase):
    """🟡6：判据必须走 `daily_report.main()`——删掉那行接线就要红。

    旧判据只直接调 `archive_citizens_step`，于是把 `main()` 里的
    `citizen_line = archive_citizens_step()` 整行删掉，测试照样全绿：
    "能力在"与"真的接上了调度"是两件事，只有走 `main()` 才测得到后者。
    """

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="fish-g6-"))
        self.data = self.base / "data"
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        import daily_report
        self.dr = daily_report
        # 只改**常量**（与既有 daily_report 判据同一手法）：整套流程落在临时目录，
        # 备份/报告/日志一个字节都不碰生产 `p0/data`。
        self._saved = (self.dr.DATA, self.dr.DB, self.dr.BACKUPS, self.dr.LOG)
        self.dr.DATA = self.data
        self.dr.DB = self.data / "index.sqlite"
        self.dr.BACKUPS = self.data / "_backups"
        self.dr.LOG = self.data / "_reports" / "_run.log"

    def tearDown(self):
        (self.dr.DATA, self.dr.DB, self.dr.BACKUPS, self.dr.LOG) = self._saved
        try:
            self.conn.close()
        except Exception:               # noqa: BLE001
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    # ------------------------------------------------------------------ 夹具
    def _idle_citizen(self, name, *, idle_days=2):
        cid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,host,status,"
            "created_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",
            (cid, name, "visitor", "cli", "active",
             _z_ago(days=idle_days + 1), _z_ago(days=idle_days)))
        self.conn.commit()
        return cid

    def _run_main(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.dr.main(["--skip-report"])
        return rc, buf.getvalue()

    def _hot(self, cid):
        return self.conn.execute(
            "SELECT COUNT(*) FROM citizens WHERE citizen_id=?", (cid,)).fetchone()[0]

    def _archived_at(self, cid):
        row = self.conn.execute(
            "SELECT archived_at FROM citizens_all WHERE citizen_id=?", (cid,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ 判据
    def test_G6_main_archives_when_configured(self):
        """配 `session.archive_after_days=1` + 2 天前闲置公民 → main 跑完后被归档。"""
        fish.set_config(self.conn, "session.archive_after_days", 1)
        self.conn.commit()
        cid = self._idle_citizen("G6-闲置者", idle_days=2)
        self.assertEqual(self._hot(cid), 1, "起点必须在热表里（否则测不到归档）")

        _rc, out = self._run_main()

        line = _line_containing(out, "公民归档：")
        self.assertTrue(line, f"main() 的回执里必须有公民归档这一行：\n{out}")
        self.assertNotIn("失败", line, f"归档不该失败：{line}")
        self.assertIn("1 个", line, f"应归档 1 个闲置公民：{line}")
        self.assertEqual(self._hot(cid), 0, "归档后必须从热表搬走")
        self.assertIsNotNone(self._archived_at(cid), "归档行必须带 archived_at（冷表可查）")

    def test_G6_main_skips_when_config_is_zero(self):
        """配 0（= 不自动归档）→ 不归档且**显式**输出「跳过」。"""
        fish.set_config(self.conn, "session.archive_after_days", 0)
        self.conn.commit()
        cid = self._idle_citizen("G6-不该动", idle_days=30)

        _rc, out = self._run_main()

        line = _line_containing(out, "公民归档：")
        self.assertIn("跳过", line, f"0 = 不自动归档，必须说出来而不是静默：{line!r}")
        self.assertEqual(self._hot(cid), 1, "配 0 时一条都不许动")
        self.assertIsNone(self._archived_at(cid))


# ============================== 🟡7 + ⚪15 活动→项目解析→看板（端到端）
class G7ActivityToBoardTest(_TempRoot):
    """🟡7：payload(cwd + file_path) → 项目解析 → 看板出现该会话（真链路，不塞 project）。

    D 批判据直接给 classification 塞 `project`，绕过了"cwd/路径 → workspace_map →
    项目"这一段（FB-14 的根因恰在这一段：会话有活动、但项目解析不到就看板看不见）。
    ⚪15 一并在这里读：节标题必须说「未在本板登记」并解释"可能签在别的板"。
    """

    def setUp(self):
        super().setUp()
        self.prefix = self._project("G7")
        self.dev = self.base / "devproj"
        self.dev.mkdir(parents=True, exist_ok=True)
        # 只写**临时库**里的 workspace_map（不碰生产映射）
        self.conn.execute(
            "INSERT INTO workspace_map (path_prefix,project,note,created_at) "
            "VALUES (?,?,NULL,?)",
            (fish_surface.normalize_path(str(self.dev)), self.prefix, fish.now_utc()))
        self.conn.commit()

    def _post(self, *, path, cwd, session_id):
        """走采集层唯一入口（payload 里只有宿主给的东西，**没有 project**）。"""
        payload = {
            "session_id": session_id,
            "cwd": cwd,
            "tool_name": "Write",
            "tool_input": {"file_path": path, "content": "x"},
            "tool_response": {"content": "ok"},
        }
        return fish_activity.handle_post_tool_use(
            self.conn, host="dsh", payload=payload, now=fish.now_utc(), data_dir=None)

    def _board_text(self):
        board = fish.board_ensure(self.conn, "project", self.prefix)
        fish.board_render(self.conn, self.root, board)
        return (fish.board_out_path(self.root, board)).read_text(encoding="utf-8")

    def _stored_project(self, session_key):
        row = self.conn.execute(
            "SELECT project FROM tool_activity WHERE session_key=? ORDER BY id DESC",
            (session_key,)).fetchone()
        return row[0] if row else None

    def test_G7_mapped_dir_session_appears_on_board(self):
        """已映射目录里的写动作 → 活动行 project 非空 → 看板出现该会话。"""
        result = self._post(path=str(self.dev / "p0" / "fish.py"),
                            cwd=str(self.base), session_id="g7-mapped")
        self.assertTrue(result["recorded"], f"写动作应当落库：{result}")
        self.assertEqual(result["project"], self.prefix,
                         "项目必须由 cwd/路径经 workspace_map 解析出来")
        self.assertEqual(self._stored_project(result["session_key"]), self.prefix,
                         "落库行的 project 不许为空（为空 = 看板看不见它）")

        text = self._board_text()
        # 2026-10-26（撞车自撞修复 F2/QA）：人类面只出会话**短标识**，
        # 完整键保留在机器面（`board show --json` / `board_unchecked_sessions`）。
        self.assertIn(fish_naming.session_short(result["session_key"]), text,
                      f"窗口内在动的会话必须出现在看板上：\n{text}")
        self.assertNotIn(result["session_key"], text, "人类面不许出现完整会话键")
        # 目录是**相对项目根**的（`devproj/p0/fish.py` → `p0`），与看板其他列同一口径
        self.assertIn("p0（1 次）", text, f"在动目录要印出来（推断的原始证据）：\n{text}")

    def test_G7_unmapped_dir_session_absent(self):
        """未映射目录 → 不归属本项目 → 本板不出现（宁可不显示，也不显示错的）。"""
        outside = self.base / "nomap"
        outside.mkdir(parents=True, exist_ok=True)
        result = self._post(path=str(outside / "x.py"), cwd=str(outside),
                            session_id="g7-nomap")
        self.assertTrue(result["recorded"], "写动作仍要落库（只是没有项目归属）")
        self.assertIsNone(result["project"], "未映射目录不该被猜成某个项目")
        self.assertIsNone(self._stored_project(result["session_key"]))

        text = self._board_text()
        self.assertNotIn(result["session_key"], text,
                         "没有项目归属的会话不许串到本板上")

    def test_G15_heading_says_not_registered_on_this_board(self):
        """⚪15：标题是「未在本板登记」，并解释"可能签在别的板"。"""
        result = self._post(path=str(self.dev / "p0" / "fish.py"),
                            cwd=str(self.dev), session_id="g15-session")
        text = self._board_text()
        self.assertIn("### 在动但未在本板登记（推断：来自 tool_activity）", text,
                      f"标题语义要准：在别的板签过到 ≠ 没签到：\n{text}")
        self.assertNotIn("### 在动但未签到", text, "旧措辞要彻底退场（少一档就是误报漏签）")
        self.assertIn("未在本板签到 ≠ 没签到", text, "那一档必须在节内被说出来")
        self.assertIn("可能签在别的板", text)
        self.assertIn(fish_naming.session_short(result["session_key"]), text,
                      "该节仍要列出在动的会话（人类面用短标识）")


# ============================================= 🟡8 distill 参数错误的退出码
class G8DistillExitTest(_TempRoot):
    """🟡8：`fish distill --days -1` 必须以 2 退出（`main()` 不传播返回值）。"""

    def test_G8_bad_days_exits_2(self):
        self._project("G8")
        args = argparse.Namespace(root=str(self.root), project="G8", days=-1,
                                  min=2, max_groups=10, write=False)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    fish.cmd_distill(args)
        self.assertEqual(ctx.exception.code, 2,
                         "参数错误必须显式 sys.exit(2)：return 2 会被 main() 丢掉")
        self.assertIn("错误", err.getvalue(), "失败要有可读的错误行")

    def test_G8_ok_case_still_exits_zero_via_main(self):
        """对照组：参数正常时不退出（改退出码不许把正常路径也带崩）。"""
        self._project("G8")
        args = argparse.Namespace(root=str(self.root), project="G8", days=30,
                                  min=2, max_groups=10, write=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_distill(args)          # 不抛 SystemExit
        self.assertIn("窗口 30 天", buf.getvalue())


# ======================================================= 🟡9 标签 LIKE 转义
class G9TagsLikeTest(_TempRoot):
    """🟡9：tag 里的 `_` / `%` 是**字面量**，不许当通配符（`a_b` 不许命中 `axb`）。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("G9")
        self.codes = {}
        for tag, title in (("a_b", "标签命中：下划线"), ("axb", "标签命中：x"),
                           ("a%b", "标签命中：百分号")):
            code, _rel = fish.record_memory(
                self.conn, self.root, project=self.prefix, title=title,
                body="正文占位。", mem_type="fact", tags=[tag])
            self.codes[tag] = code
        fish.ensure_fts(self.conn, self.root)

    def test_G9_list_and_count_match_only_literal_tag(self):
        rows = fish.list_memories(self.conn, project=self.prefix, tags=["a_b"])
        self.assertEqual([r[0] for r in rows], [self.codes["a_b"]],
                         f"`a_b` 只许命中 `a_b`（不许通配到 axb）：{[r[0] for r in rows]}")
        self.assertEqual(fish.count_memories(self.conn, project=self.prefix,
                                             tags=["a_b"]), 1,
                         "计数与列表必须同一谓词（否则「共 2 条」却只回 1 条）")

    def test_G9_percent_is_literal(self):
        rows = fish.list_memories(self.conn, project=self.prefix, tags=["a%b"])
        self.assertEqual([r[0] for r in rows], [self.codes["a%b"]],
                         "`a%b` 里的 % 是字面量，不许匹配 a_b / axb")

    def test_G9_same_family_sites_fixed(self):
        """同族两处（`recent_fallback` / `search_memories`）一并钉住。"""
        fallback = fish.recent_fallback(self.conn, project=self.prefix, tags=["a_b"])
        self.assertEqual([c for c, _t in fallback], [self.codes["a_b"]],
                         f"recent_fallback 的标签过滤同样不许通配：{fallback}")
        rows, _src = fish.search_memories(self.conn, query="标签命中",
                                          project=self.prefix, tags=["a_b"])
        self.assertEqual([r[0] for r in rows], [self.codes["a_b"]],
                         f"search_memories 的 FTS 下推过滤同样不许通配：{[r[0] for r in rows]}")


# ================================================== ⚪11 tags 为空时的提示
class G11TagsHintTest(_TempRoot):
    """⚪11：**没给** tags 时不许说「去掉 --tags」（那是他没做过的动作）。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("G11")     # 项目存在但一条记忆都没有

    def _cli_recall(self, tags=None):
        args = argparse.Namespace(root=str(self.root), query=None, code=None,
                                  project=self.prefix, tags=tags, limit=10,
                                  scope="all")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_recall(args)
        return buf.getvalue()

    def test_G11_cli_branches_on_tags_presence(self):
        without = self._cli_recall()
        self.assertNotIn("去掉 --tags", without,
                         f"没给 tags 却让人去删它——提示在描述不存在的动作：\n{without}")
        self.assertIn("还没有记忆", without, f"真实原因要说出来：\n{without}")

        with_tags = self._cli_recall(tags=["fish-feedback"])
        self.assertIn("去掉 --tags", with_tags,
                      f"给了 tags 且没命中时，「去掉 --tags」才是可执行下一步：\n{with_tags}")

    def test_G11_mcp_branches_on_tags_presence(self):
        without = self.srv.call_tool("recall", {"project": self.prefix})
        self.assertNotIn("去掉 tags", without, f"MCP 同样不许提不存在的参数：\n{without}")
        self.assertIn("还没有记忆", without, without)

        with_tags = self.srv.call_tool("recall", {"project": self.prefix,
                                                  "tags": ["fish-feedback"]})
        self.assertIn("去掉 tags", with_tags, with_tags)


# ===================================================== ⚪13 忽略规则
class G13GitignoreTest(unittest.TestCase):
    """⚪13：`p0/_audit/` 与探针/运行产物要被忽略（不污染 `git status`）。"""

    def setUp(self):
        # 判据文件在 `p0/` 下，.gitignore 在仓库根（`fish-fix/`）
        self.gitignore = Path(__file__).resolve().parent.parent / ".gitignore"

    def test_G13_ignores_batch_artifacts(self):
        self.assertTrue(self.gitignore.is_file(), f"找不到 .gitignore：{self.gitignore}")
        text = self.gitignore.read_text(encoding="utf-8")
        rules = {line.strip() for line in text.splitlines()
                 if line.strip() and not line.strip().startswith("#")}
        for rule in ("p0/_audit/", "p0/_probe_*", "p0/_run_*"):
            self.assertIn(rule, rules, f".gitignore 缺规则 {rule}；现有规则：{sorted(rules)}")
        # 仓库原有文件不能被自己的忽略规则误伤
        self.assertNotIn("p0/_sample_inject.py", rules)
        self.assertFalse(self.gitignore.with_name(".gitignore").name != ".gitignore")


if __name__ == "__main__":
    unittest.main(verbosity=2)
