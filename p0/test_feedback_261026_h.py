#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批 H：按**第二轮独立评审**（对 `828c71c` 的报告）逐条闭合的判据（2026-10-26）。

每个类对应评审报告里的**一条编号**，判据名带同一编号，便于回查：

  | 编号 | 现象 | 本文件的判据 |
  |---|---|---|
  | 🟠N1 | ④⑤ 被预算整段裁掉时尾行说「本次无截断」，且尾句按**请求段**而非**实际段** | `HN1TailTruncationTest` |
  | 🟠N2 | CLI `whoami` 带有效 token 仍答「未登记访客」，还建议 CLI 里不存在的 `register` | `HN2WhoamiTokenIdentityTest` |
  | 🟡N3 | `injection_tail_worst()` 不是真最坏（预留 83 / 实测 118） | `HN3TailWorstTest` |
  | 🟡N4 | `main()` 丢返回码：`board checkin` 缺 `--citizen`、`archive one` 缺 code 都 exit 0 | `HN4ExitCodeTest` |
  | ⚪N5 | 含反斜杠的标签永远查不到（JSON 层未转义） | `HN5TagBackslashTest` |
  | ⚪N6 | `FISH_SESSION_TOKEN` 只有 whoami 一个消费者 | `HN6EnvTokenActorTest` |
  | ⚪N7 | 别处读过印成 `2/50`，实为下限 `≥50` | `HN7GapCapTest` |
  | ⚪N8 | `count_*` 读失败返回 0 → 段头「显示 2/2」更自信地假 | `HN8UnknownTotalTest` |
  | ⚪N10 | 垃圾 `expires_at`（如 `!!bad!!`）被念成「已过期」 | `HN10BadExpiresTest` |

## 每条判据为什么这么读

- **读印出来的文本**，不读实现里的中间变量：包尾、段头、回执上写什么，读者就信什么。
- **不拿实现自证实现**：🟠N1 用真预算把 ④⑤ 裁掉再读尾两行；🟡N3 穷举 2^8 子集算
  "真最坏"，而不是再问一遍被测函数；⚪N8 把 `sqlite3.Error` **真抛进** COUNT 语句，
  而不是把 `count_*` 直接替换成 None（替换掉就测不到 except 分支）。
- **判据要能失败**：本轮做过的三次变异（尾句改回 `want` / 身份段忽略 token /
  `main()` 恢复丢返回码）都会让对应判据变红，见文件末尾 `MUTATION_LOG`。

## 最小运行（**不要**跑全量）

    & "C:\\Users\\15596\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" `
      -m unittest test_feedback_261026_h -v

必须在 `fish-fix/p0` 目录下跑（`fish.py` 按相对路径加载，与其他测试一致）。
"""
import argparse
import contextlib
import datetime
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server        # noqa: E402

# `显示 N/M 条`（段头契约）。⚪N7/N8 都要用"没长出这个形状"来断言。
SHOW_RE = re.compile(r"显示 (\d+)/(\d+) 条")
# 尾句里可能出现的中文段名 → 包里对应的层（`meta["layers"]` 的键）。
# `最近三件事` 不在这张表里：它不是 `meta["layers"]` 的一层，而由 `recent_codes` 判。
_TAIL_LABEL_TO_LAYER = {
    "L1 状态坐标": "L1",
    "看板": "board",
    "规矩": "rules",
    "该注意什么": "attention",
    "错题本": "lesson",
    "候选": "push",
    "L2 索引": "L2",
}
_WORST_COUNTS = dict(fish._INJECTION_TAIL_WORST_COUNTS)
_WORST_AS_OF = fish._INJECTION_TAIL_WORST_AS_OF

# 会让身份段漂移的环境变量：判据里全部清空，只留被测的那一个。
_TOKEN_ENV = "FISH_SESSION_TOKEN"
_NOISE_ENVS = ("FISH_ACTOR", "FISH_SESSION_ACTOR", _TOKEN_ENV,
               "DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID")

P0_DIR = Path(__file__).resolve().parent


def _z_ago(days=0, minutes=0) -> str:
    """`days` 天 / `minutes` 分钟前的 UTC ISO（与库里 `recorded_at` 同一口径）。"""
    moment = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days, minutes=minutes))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tail_lines(pack):
    """包尾两行（截断声明 + as-of）——空行不算，MCP 会在它之后追加注释。"""
    lines = [ln for ln in (pack or "").strip().splitlines() if ln.strip()]
    return lines[-2], lines[-1]


class _FailingConn:
    """把"读失败"注入成**真异常**：只对命中的 COUNT 语句抛 `sqlite3.Error`。

    为什么不直接 monkeypatch `count_*`：那样测的只是"调用方见到 None 会怎么写"，
    `count_*` 自己的 `except sqlite3.Error → None` 分支仍然没被跑过——
    而这轮修的正是"读失败时它返回 0"。注入进 execute 才走完整条真实路径。
    """

    def __init__(self, conn, needles):
        self._conn = conn
        self._needles = tuple(needles)

    def execute(self, sql, params=()):
        if any(n in sql for n in self._needles):
            raise sqlite3.OperationalError("simulated read failure（判据注入）")
        return self._conn.execute(sql, params)

    def __getattr__(self, name):           # executionscript / commit / close … 原样透传
        return getattr(self._conn, name)


class _TempRoot(unittest.TestCase):
    """一个临时根 + 一个 `mcp_server.FishServer`（临时库，绝不碰生产）。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="fish-h-"))
        self.root = self.base / "data"
        self.srv = mcp_server.FishServer(self.root)
        self.conn = self.srv.conn

    def tearDown(self):
        try:
            self.conn.close()
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

    def _mem(self, code, title, *, project, owner=None, mem_type="fact",
             tags=None, recorded_at=None):
        ts = recorded_at or fish.now_utc()
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared','active',?,NULL,?,?,0,0)",
            (code, title, project, owner, mem_type,
             json.dumps(list(tags or []), ensure_ascii=False), ts, ts))
        self.conn.commit()

    def _token(self, citizen_id, *, session_key="fish-h:sess", ttl_hours=24):
        res = fish.issue_session_token(
            self.conn, citizen_id=citizen_id, session_key=session_key,
            evidence="self-reported", channel="cli", ttl_hours=ttl_hours)
        self.conn.commit()
        return res["token"]

    @staticmethod
    @contextlib.contextmanager
    def _clean_env(**extra):
        """清掉会让身份段漂移的环境变量；`extra` 用来注入被测的那一个。"""
        env = {name: "" for name in _NOISE_ENVS}
        env.update(extra)
        with mock.patch.dict(os.environ, env):
            yield


# ============================================ 🟠N1 ④⑤ 被裁不许说「本次无截断」
class HN1TailTruncationTest(_TempRoot):
    """🟠N1：包尾两行必须与**包里真的有什么**一致，而不是与"要了什么"一致。

    评审实测：`sections=['attention','lessons']` + 小预算 → `trimmed` 里写着
    `['注意事项','错题本']`，尾行却写「本次无截断」，尾句仍称含这两段。
    """

    def setUp(self):
        super().setUp()
        self.prefix = self._project("HN1")
        self.cid = self._citizen("HN1-行动者")
        # ④ 的信号：标题命中开放词表（窗口 30 天内）
        for i, title in enumerate(("未闭合：甲", "未闭合：乙", "下一步：丙"), start=1):
            self._mem(f"HN1-261026-{i:02d}", title, project=self.prefix,
                      owner=self.cid, recorded_at=_z_ago(minutes=30 - i))
        # ⑤ 的信号：lesson（窗口 7 天内）
        for i, title in enumerate(("教训甲", "教训乙", "教训丙"), start=4):
            self._mem(f"HN1-261026-{i:02d}", title, project=self.prefix,
                      owner=self.cid, mem_type="lesson",
                      recorded_at=_z_ago(minutes=20 - i))

    def _pack(self, **kw):
        kw.setdefault("project", self.prefix)
        return fish.build_injection_pack(self.conn, self.root, self.cid, **kw)

    def _find_budget_trimming_both(self):
        """找到"④ 与 ⑤ 都被预算整段裁掉"的那个预算（找不到 = 判据失去靶点）。"""
        for budget in (900, 800, 700, 600, 500, 450, 400, 350, 300, 260,
                       220, 180, 150, 120, 100, 80, 60, 50):
            pack, meta = self._pack(budget=budget,
                                    sections=["attention", "lessons"])
            if meta["truncated"]["attention"] and meta["truncated"]["lessons"]:
                return budget, pack, meta
        return None

    def _head(self, **kw):
        kw.setdefault("project", self.prefix)
        return fish.build_attention_block(self.conn, self.root, citizen_id=self.cid,
                                          session_actor="hn1-sess", **kw)

    # ------------------------------------------------------------------ 判据
    def test_HN1_cut_attention_and_lessons_is_reported(self):
        """小预算裁掉 ④⑤ → 尾行**必须**说截断，并点名这两段。"""
        hit = self._find_budget_trimming_both()
        self.assertIsNotNone(
            hit, "找不到把 ④⑤ 整段裁掉的预算，判据失去靶点（请检查两段是否真有内容）")
        budget, pack, meta = hit
        first, _second = _tail_lines(pack)
        self.assertNotIn("本次无截断", first,
                         f"budget={budget} 时 ④⑤ 已被裁，尾行却说没截断：\n{first}")
        self.assertIn("本次截断", first, f"尾行要报截断：{first}")
        self.assertIn("该注意什么整段", first, f"被裁的段要点名（④）：{first}")
        self.assertIn("错题本整段", first, f"被裁的段要点名（⑤）：{first}")
        # 与 meta 同源同值（两处各算一份 = 读者不知道信谁）
        self.assertEqual(first, fish.announce_truncation(meta["truncated"]))
        self.assertEqual(
            (meta["truncated"]["attention"], meta["truncated"]["lessons"]), (1, 1),
            f"meta 也必须记着这两段被裁：{meta['truncated']}")

    def test_HN1_tail_never_names_segments_absent_from_pack(self):
        """尾句不许点名**包里没有的段**——这是🟠N1 的核心，覆盖多种段选择。"""
        cases = [["attention", "lessons"], ["status"], ["push", "index"],
                 ["board", "rules"], list(fish.INJECTION_SECTIONS)]
        checked = 0
        for sections in cases:
            for budget in (None, 600, 400, 260, 150):
                pack, meta = self._pack(budget=budget, sections=sections)
                _first, second = _tail_lines(pack)
                phrase = second.split("）", 1)[0] if "（" in second else second
                for label, layer in _TAIL_LABEL_TO_LAYER.items():
                    if label not in phrase:
                        continue
                    self.assertIn(label, phrase, second)
                    self.assertGreater(
                        int((meta["layers"] or {}).get(layer) or 0), 0,
                        f"尾句点名「{label}」，但包里这一层是空的"
                        f"（sections={sections} budget={budget}）：\n{second}")
                    checked += 1
                if "最近三件事" in phrase:
                    self.assertTrue(meta["recent_codes"],
                                    f"尾句点名「最近三件事」但包里没有：\n{second}")
                if "L0 身份" in phrase:
                    checked += 1
        self.assertGreater(checked, 0, "一次都没读到段名 = 判据没在看尾句")

    def test_HN1_announce_truncation_unit(self):
        """`announce_truncation` 单测：④⑤ 是"整段"档，不编条数。"""
        self.assertEqual(fish.announce_truncation({"attention": 1}),
                         "本次截断：该注意什么整段")
        self.assertEqual(fish.announce_truncation({"lessons": 1}),
                         "本次截断：错题本整段")
        self.assertEqual(fish.announce_truncation({}), "本次无截断")
        self.assertEqual(fish.announce_truncation(
            {"rules": 0, "push": 0, "board": 0, "l2": 0,
             "attention": 0, "lessons": 0}), "本次无截断",
            "六类全 0 才是「本次无截断」")

    def test_HN1_full_pack_without_cut_keeps_legacy_phrase(self):
        """全段、预算一个段都没裁 → 仍是旧措辞（不把健康包说得更残缺）。"""
        pack, meta = self._pack(budget=800)
        _first, second = _tail_lines(pack)
        if not meta["trimmed"]:
            self.assertIn("本包含 L0–L2 与候选", second, second)
        else:
            self.assertFalse(meta["over_budget"], "预算够用时不该超预算")


# ==================================== 🟠N2 CLI whoami 带有效 token 的身份段
class HN2WhoamiTokenIdentityTest(_TempRoot):
    """🟠N2：token 有效时 CLI 的身份段必须承认它，且与 MCP 给**同一个公民 id**。"""

    def setUp(self):
        super().setUp()
        self.cid = self._citizen("HN2-持证人")
        self.token = self._token(self.cid)

    def _cli_whoami(self, **kw):
        args = argparse.Namespace(root=str(self.root), citizen=None,
                                  token=self.token, all=False)
        for key, value in kw.items():
            setattr(args, key, value)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_whoami(args)
        return buf.getvalue()

    def _mcp_whoami(self):
        return self.srv.call_tool("whoami", {"token": self.token})

    @staticmethod
    def _citizen_ids(text):
        """回执里出现过的所有公民 id（按 `公民 <id>` 的字面形状取）。"""
        return set(re.findall(r"公民 ([0-9A-Za-z_-]+)", text or ""))

    # ------------------------------------------------------------------ 判据
    def test_HN2_valid_token_is_the_identity(self):
        """带有效 token → 首屏出现 `凭证归属：公民 <id>`，不再答「未登记访客」。"""
        with self._clean_env():
            out = self._cli_whoami()
        self.assertIn(f"凭证归属：公民 {self.cid}", out,
                      f"有效凭证必须进身份段（与 MCP 同源）：\n{out}")
        self.assertNotIn("未登记访客", out,
                         f"手上握着有效凭证还说「未登记访客」是自相矛盾：\n{out}")
        self.assertNotIn("register", out,
                         f"CLI 里没有 register 命令，不许建议它（会造出第二个公民）：\n{out}")

    def test_HN2_cli_and_mcp_agree_on_citizen_id(self):
        """同一 token：CLI 与 MCP 的首屏身份必须指向同一个公民。"""
        with self._clean_env():
            cli = self._cli_whoami()
        mcp = self._mcp_whoami()
        self.assertIn(f"公民 {self.cid}", mcp, f"MCP 回执应含该公民：\n{mcp}")
        self.assertEqual(self._citizen_ids(cli) & {self.cid}, {self.cid},
                         f"CLI 回执里没有这个公民：\n{cli}")
        self.assertEqual(self._citizen_ids(cli), self._citizen_ids(cli) & {self.cid},
                         f"CLI 回执出现了别的公民（归属漂了）：\n{cli}")

    def test_HN2_env_token_reaches_identity_section(self):
        """env 凭证（不带 --token）同样进身份段——CLI 唯一的会话级载体。"""
        with self._clean_env(**{_TOKEN_ENV: self.token}):
            out = self._cli_whoami(token=None)
        self.assertIn(f"凭证归属：公民 {self.cid}", out, out)
        self.assertNotIn("未登记访客", out, out)

    def test_HN2_no_token_still_honest(self):
        """无凭证、无 actor：仍是「未登记访客」，但建议必须是 CLI 真有的命令。"""
        with self._clean_env():
            out = self._cli_whoami(token=None)
        self.assertIn("你：未登记访客", out, f"旧文案不许被弄丢：\n{out}")
        self.assertIn("citizen-add", out, f"建议要给 CLI 真有的命令：\n{out}")
        self.assertIn("session start", out, f"或引导去签发凭证：\n{out}")
        self.assertNotIn("register", out, f"register 是 MCP 工具，CLI 没有：\n{out}")


# ================================================= 🟡N3 真·最坏包尾
class HN3TailWorstTest(_TempRoot):
    """🟡N3：`injection_tail_worst()` 必须是**枚举意义下**的最坏，而不是一种猜测。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("HN3")
        self.cid = self._citizen("HN3-行动者")
        self._mem("HN3-261026-01", "未闭合：甲", project=self.prefix, owner=self.cid)
        self._mem("HN3-261026-02", "教训甲", project=self.prefix, owner=self.cid,
                  mem_type="lesson")

    def _pack(self, **kw):
        kw.setdefault("project", self.prefix)
        return fish.build_injection_pack(self.conn, self.root, self.cid, **kw)

    # ------------------------------------------------------------------ 判据
    def test_HN3_worst_covers_all_256_subsets(self):
        """穷举 2^8 子集（+ 旧措辞那一档）：没有一种包尾比"最坏"更长。"""
        worst = fish.injection_tail_worst()
        worst_len, worst_tok = len(worst), fish.count_tokens(worst)
        names = list(fish.INJECTION_SECTIONS)
        tails = [fish.injection_tail_text(_WORST_COUNTS, _WORST_AS_OF, sections=None)]
        for mask in range(1 << len(names)):
            subset = [n for i, n in enumerate(names) if (mask >> i) & 1]
            tails.append(fish.injection_tail_text(_WORST_COUNTS, _WORST_AS_OF,
                                                 sections=subset))
        longest = max(tails, key=len)
        self.assertLessEqual(len(longest), worst_len,
                             f"最坏穷举长度 {len(longest)} > 预留 {worst_len}"
                             f"\n最长的一种：{longest!r}\n预留：{worst!r}")
        self.assertLessEqual(max(fish.count_tokens(t) for t in tails), worst_tok,
                             "最坏穷举的 token 数超过了预留")
        self.assertGreaterEqual(worst_len, 100,
                                f"最坏只有 {worst_len} 字符，明显没把段名与整段标记算全")

    def test_HN3_worst_is_not_the_full_pack_phrase(self):
        """🔴 旧实现的病根：它算的其实是**最短**那句（全段走旧措辞）。"""
        worst = fish.injection_tail_worst()
        legacy = fish.injection_tail_text(_WORST_COUNTS, _WORST_AS_OF,
                                          sections=None)
        self.assertGreater(len(worst), len(legacy),
                           "最坏若等于旧措辞，说明没枚举段名组合（N3 未修）")
        self.assertNotIn("本包含 L0–L2 与候选", worst,
                         f"最坏必须是动态措辞那一档：{worst!r}")

    def test_HN3_end_to_end_no_over_budget_when_almost_all_sections(self):
        """端到端：除 board 外全选 + 小预算 → 不许超预算（预留要真的盖住包尾）。"""
        sections = [s for s in fish.INJECTION_SECTIONS if s != "board"]
        for budget in (500, 400, 320):
            pack, meta = self._pack(budget=budget, sections=sections)
            actual = fish.count_tokens(_tail_lines(pack)[0] + "\n" + _tail_lines(pack)[1])
            self.assertLessEqual(
                actual, fish.count_tokens(fish.injection_tail_worst()),
                f"实际包尾 {actual} > 预留（budget={budget}）")
            self.assertFalse(meta["over_budget"],
                             f"budget={budget} 时超预算：{meta['tokens']}/{meta['budget']}"
                             f" trimmed={meta['trimmed']}")
            self.assertLessEqual(meta["tokens"], meta["budget"], meta["trimmed"])


# ================================================= 🟡N4 CLI 返回码传播
class HN4ExitCodeTest(unittest.TestCase):
    """🟡N4：子命令的失败返回码必须在 CLI 边界变成进程退出码。

    旧实现 `main()` 里只写 `args.func(args)`，返回值被丢掉 → 两条"参数错误"
    的命令都 exit 0：脚本与 CI 读到的是"成功"，而动作根本没发生。
    """

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="fish-h4-"))
        self.root = self.base / "data"

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _run(self, *argv):
        cmd = [sys.executable, "fish.py", "--root", str(self.root), *argv]
        return subprocess.run(cmd, cwd=str(P0_DIR), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=120)

    # ------------------------------------------------------------------ 判据
    def test_HN4_board_checkin_without_citizen_exits_2(self):
        res = self._run("board", "checkin", "--doing", "HN4 演练")
        self.assertEqual(res.returncode, 2,
                         f"缺 --citizen 必须非零退出（实际 {res.returncode}）\n"
                         f"stdout={res.stdout}\nstderr={res.stderr}")
        self.assertIn("必须带 --citizen", res.stderr or "",
                      f"还要说清为什么：\n{res.stderr}")

    def test_HN4_archive_one_without_code_exits_2(self):
        res = self._run("archive", "one")
        self.assertEqual(res.returncode, 2,
                         f"`archive one` 没给编码必须非零退出（实际 {res.returncode}）\n"
                         f"stdout={res.stdout}\nstderr={res.stderr}")
        self.assertIn("one 需要给编码", res.stderr or "", res.stderr)

    def test_HN4_success_still_exits_0(self):
        """成功的命令不许被这次改动误伤（返回 None / 0 一律 exit 0）。"""
        res = self._run("init")
        self.assertEqual(res.returncode, 0,
                         f"正常命令必须 exit 0：\nstdout={res.stdout}\nstderr={res.stderr}")


# ================================================= ⚪N5 含反斜杠的标签
class HN5TagBackslashTest(_TempRoot):
    """⚪N5：标签里的 `\\` 在库里是 JSON 转义过的，查询必须先过 JSON 层。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("HN5")
        self.owner = self._citizen("HN5-作者")
        self.backslash = "a\\b"          # 用户眼里的标签：a\b（一个反斜杠）
        self.plain_underscore = "a_b"
        self.lookalike = "axb"

    def _write(self, title, tags):
        code, _rel = fish.record_memory(
            self.conn, self.root, project=self.prefix, title=title, body="正文。",
            mem_type="fact", tags=tags, owner=self.owner)
        return code

    def _codes(self, **kw):
        rows = fish.list_memories(self.conn, project=self.prefix, **kw)
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ 判据
    def test_HN5_backslash_tag_is_findable(self):
        """`tags=["a\\b"]` 必须能查到自己（旧实现永远查不到）。"""
        want = self._write("HN5 反斜杠标签", [self.backslash])
        self._write("HN5 下划线标签", [self.plain_underscore])
        stored = self.conn.execute(
            "SELECT tags FROM code_index WHERE code=?", (want,)).fetchone()[0]
        self.assertIn('"a\\\\b"', stored,
                      f"库里的形态必须是 JSON 转义过的：{stored!r}")
        self.assertEqual(self._codes(tags=[self.backslash]), [want],
                         "含反斜杠的标签查不到自己 = JSON 层没转义（⚪N5 未修）")
        self.assertEqual(fish.count_memories(self.conn, project=self.prefix,
                                             tags=[self.backslash]), 1)

    def test_HN5_underscore_does_not_match_wildcard(self):
        """`a_b` 不许命中 `axb`（LIKE 通配符回归闸）。"""
        want = self._write("HN5 下划线标签", [self.plain_underscore])
        self._write("HN5 长得像的标签", [self.lookalike])
        self.assertEqual(self._codes(tags=[self.plain_underscore]), [want],
                         "`a_b` 命中了 `axb`：LIKE 转义被 JSON 层改动带坏了")

    def test_HN5_quote_tag_is_findable(self):
        """标签里的引号同样要先过 JSON 层（与反斜杠同一类数据）。"""
        want = self._write("HN5 引号标签", ['a"b'])
        self.assertIn(want, self._codes(tags=['a"b']),
                      "含引号的标签查不到自己（JSON 层转义漏了引号）")


# ============================================ ⚪N6 env 凭证接进行者解析
class HN6EnvTokenActorTest(_TempRoot):
    """⚪N6：`FISH_SESSION_TOKEN` 必须参与行动者解析（不再只有 whoami 一个消费者）。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("HN6")
        self.cid = self._citizen("HN6-凭证持有者")
        self.token = self._token(self.cid, session_key="fish-h:hn6")

    def _inject(self):
        args = argparse.Namespace(root=str(self.root), citizen=None, budget=None,
                                  project=self.prefix, cwd=None, sections=None,
                                  no_board=False, auto_presence=False, json=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_inject(args)
        return buf.getvalue()

    # ------------------------------------------------------------------ 判据
    def test_HN6_env_token_resolves_actor(self):
        """设 env 凭证 → `inject` 不再报「未确定行动者」，且身份段是那个公民。"""
        with self._clean_env(**{_TOKEN_ENV: self.token}):
            row = fish.resolve_actor(self.conn)
            out = self._inject()
        self.assertIsNotNone(row, "env 凭证必须能给出行动者（⚪N6 未修）")
        self.assertEqual(row[0], self.cid)
        self.assertNotIn("未确定行动者", out,
                         f"握着有效凭证还说没有行动者：\n{out}")
        payload = json.loads(out)
        self.assertIn(f"公民 {self.cid}", payload["pack"],
                      f"L0 身份段必须写这个公民：\n{payload['pack'][:400]}")

    def test_HN6_invalid_env_token_falls_back_to_none(self):
        """无效凭证不许被当成身份（宁可为空，也不许猜）。"""
        with self._clean_env(**{_TOKEN_ENV: "fsh_not_a_real_token"}):
            self.assertIsNone(fish.resolve_actor(self.conn))

    def test_HN6_remember_attributes_to_token_citizen(self):
        """`remember` 不给 --owner 时记给凭证的公民（与 MCP 的 `_owner_value` 同口径）。"""
        env = dict(os.environ)
        env.update({name: "" for name in _NOISE_ENVS})
        env[_TOKEN_ENV] = self.token
        cmd = [sys.executable, "fish.py", "--root", str(self.root),
               "remember", self.prefix, "HN6 带凭证写入", "--body", "正文。"]
        res = subprocess.run(cmd, cwd=str(P0_DIR), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env, timeout=120)
        self.assertEqual(res.returncode, 0, f"写入应成功：\n{res.stderr}")
        code = (res.stdout or "").split("\t")[0].strip()
        row = self.conn.execute(
            "SELECT owner FROM code_index WHERE code=?", (code,)).fetchone()
        self.assertIsNotNone(row, f"没读到刚写的条目：{res.stdout!r}")
        self.assertEqual(row[0], self.cid,
                         "没给 --owner 时应记给凭证的公民（token 是唯一归属）")


# ============================================ ⚪N7 别处读过的下限读数
class HN7GapCapTest(_TempRoot):
    """⚪N7：C 类扫描命中上限时，M 是**下限**，必须印 `≥50` 而不是 `50`。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("HN7")
        self.cid = self._citizen("HN7-行动者")

    def test_HN7_capped_gap_prints_ge_limit(self):
        """扫描到上限 → 段头印「别处读过 2/≥50」，不印确切的 50。"""
        fake = [{"code": f"HN7-261026-{i:02d}", "title": f"别处读过 {i}"}
                for i in range(1, fish.ATTENTION_GAP_SCAN_LIMIT + 1)]
        with mock.patch.object(fish, "_attention_gaps", return_value=fake):
            blk = fish.build_attention_block(self.conn, self.root, citizen_id=self.cid,
                                             session_actor="hn7-sess",
                                             project=self.prefix)
        head = blk["text"].splitlines()[0]
        self.assertIn(f"≥{fish.ATTENTION_GAP_SCAN_LIMIT}", head,
                      f"命中扫描上限时必须印下限（≥50）：{head}")
        self.assertIn(f"别处读过 2/≥{fish.ATTENTION_GAP_SCAN_LIMIT}", head,
                      f"按类的那一段也要是下限：{head}")
        self.assertNotIn(f"别处读过 2/{fish.ATTENTION_GAP_SCAN_LIMIT} ", head,
                         f"不许把下限印成确切值：{head}")

    def test_HN7_below_limit_stays_exact(self):
        """没到上限仍是确切值（别把正常读数也加上 ≥）。"""
        fake = [{"code": "HN7-261026-01", "title": "只有一条"}]
        with mock.patch.object(fish, "_attention_gaps", return_value=fake):
            blk = fish.build_attention_block(self.conn, self.root, citizen_id=self.cid,
                                             session_actor="hn7-sess",
                                             project=self.prefix)
        head = blk["text"].splitlines()[0]
        self.assertIn("别处读过 1/1", head, head)
        self.assertNotIn("≥", head, f"没到上限不该出现下限标记：{head}")


# ============================================ ⚪N8 读失败不许念成 0
class HN8UnknownTotalTest(_TempRoot):
    """⚪N8：`count_*` 读失败返回 None → 段头写「总数未知（读取失败）」。"""

    def setUp(self):
        super().setUp()
        self.prefix = self._project("HN8")
        self.cid = self._citizen("HN8-行动者")
        for i, title in enumerate(("未闭合：甲", "未闭合：乙"), start=1):
            self._mem(f"HN8-261026-{i:02d}", title, project=self.prefix,
                      owner=self.cid, recorded_at=_z_ago(minutes=10 - i))
        for i, title in enumerate(("教训甲", "教训乙"), start=3):
            self._mem(f"HN8-261026-{i:02d}", title, project=self.prefix,
                      owner=self.cid, mem_type="lesson",
                      recorded_at=_z_ago(minutes=8 - i))

    # ------------------------------------------------------------------ 判据
    def test_HN8_attention_total_unknown_when_count_fails(self):
        """④ 的 COUNT 抛错 → 段头明说未知，不出现「显示 2/2」。"""
        broken = _FailingConn(self.conn, ["SELECT COUNT(*) FROM code_index WHERE status="])
        blk = fish.build_attention_block(broken, self.root, citizen_id=self.cid,
                                        session_actor="hn8-sess", project=self.prefix)
        head = blk["text"].splitlines()[0]
        self.assertIn("总数未知（读取失败）", head,
                      f"读失败必须说出来（⚪N8 未修）：{head}")
        self.assertIsNone(SHOW_RE.search(head),
                          f"总数未知时不许印「显示 N/M 条」：{head}")
        self.assertNotIn("显示 2/2", head, head)
        self.assertIsNone(blk["total"], "meta 的 total 也必须是 None，不许写 0")

    def test_HN8_lesson_total_unknown_when_count_fails(self):
        """⑤ 的 COUNT 抛错 → 段头同样降级为「总数未知（读取失败）」。"""
        broken = _FailingConn(self.conn, ["SELECT COUNT(*) FROM code_index WHERE mem_type="])
        blk = fish.build_lesson_block(broken, project=self.prefix)
        head = blk["text"].splitlines()[0]
        self.assertIn("总数未知（读取失败）", head, head)
        self.assertIsNone(SHOW_RE.search(head), head)
        self.assertIsNone(blk["total"], "读不出来就别编一个 0")

    def test_HN8_counts_return_none_not_zero(self):
        """底层读数函数本身：读失败 → None（0 是"真的没有"，两者不可混）。"""
        broken = _FailingConn(self.conn, ["SELECT COUNT(*) FROM code_index"])
        self.assertIsNone(fish.count_open_loop_rows(broken, _z_ago(days=30)))
        self.assertIsNone(fish.count_lesson_rows(broken, _z_ago(days=7)))


# ============================================ ⚪N10 垃圾 expires_at 的读法
class HN10BadExpiresTest(_TempRoot):
    """⚪N10：`expires_at` 是坏数据时，口径是「无法解析」，不是「已过期」。"""

    def setUp(self):
        super().setUp()
        self.cid = self._citizen("HN10-持证人")
        self.good = self._token(self.cid, session_key="fish-h:hn10-a")
        self.bad = self._token(self.cid, session_key="fish-h:hn10-b")
        self.conn.execute("UPDATE session_tokens SET expires_at='!!bad!!' WHERE token_hash=?",
                          (fish.hash_session_token(self.bad),))
        self.conn.commit()

    def test_HN10_bad_value_is_unparseable_not_expired(self):
        line = fish.credential_state_line(self.conn, self.bad)
        self.assertIn("到期时刻无法解析", line, line)
        self.assertNotIn("已过期", line,
                         f"格式坏 ≠ 过期（把坏数据念成一个具体事实）：{line}")
        self.assertIn("!!bad!!", line, f"原值要印出来（可核对，不用猜）：{line}")

    def test_HN10_cli_whoami_survives_bad_value(self):
        """探针不许崩，且与底层口径一致。"""
        args = argparse.Namespace(root=str(self.root), citizen=None,
                                  token=self.bad, all=False)
        buf = io.StringIO()
        with self._clean_env(), contextlib.redirect_stdout(buf):
            fish.cmd_whoami(args)
        out = buf.getvalue()
        self.assertIn("到期时刻无法解析", out, out)
        self.assertNotIn("已过期", out, out)

    def test_HN10_good_token_unaffected(self):
        """好数据照旧（别把正常档也降级）。"""
        line = fish.credential_state_line(self.conn, self.good)
        self.assertIn("剩余", line, line)
        self.assertNotIn("无法解析", line, line)


# 变异自证（本轮实际跑过，红→还原）：
#   1. `build_injection_pack` 尾句传回 `sorted(want)`（N1-② 未修）
#      → `HN1_tail_never_names_segments_absent_from_pack` 与
#        `HN1_cut_attention_and_lessons_is_reported` 变红；
#   2. `cmd_whoami` 身份段忽略 token（N2 未修）→ `HN2_valid_token_is_the_identity`
#      与 `HN2_cli_and_mcp_agree_on_citizen_id` 变红；
#   3. `main()` 恢复 `args.func(args)`（N4 未修）→ `HN4_*_exits_2` 两条变红。
MUTATION_LOG = "见上方注释"


if __name__ == "__main__":
    unittest.main(verbosity=2)
