#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T02/T05 判据 · hook 行为层（真跑脚本，不是调函数）。

## 为什么要跑真进程

hook 的**失败方式**几乎都在边界上：stdin 编码、坏 JSON、子进程超时、目录不存在……
调函数永远测不到这些。所以这里用 `subprocess` 真跑四个 hook 脚本，
只靠环境变量（`FISH_DATA`）隔离数据目录——**绝不碰生产库**。

## 判据清单

  H1  采集：写类落一行、读类不落行、Bash 强特征落 exec
  H2  **隐私（设计 §2.3 H1/H2）**：
        · 写含 `SECRET-XYZ` 的内容 → 库里命中 0；心跳文件命中 0；`detours.jsonl` 命中 0
        · payload 里带 `fsh_` 凭证 → 心跳文件里命中 0（只留长度与哈希）
  H3  失败静默：坏 JSON / 空 stdin → exit 0 且 **stdout 完全为空**
  H4  两宿主等价：同一 payload 在 dsh / workbuddy 下都落行，只有会话键前缀不同
  H5  频率闸：计数器按 (event, session) 递增，BOM 也能读
  H6  跨午夜板键：`local_period_key` 用本地日期（+8），与看板一致
  H7  QA Round 1 追加（三条 P 级修复的判据）：
        · E1.8 绕路审计**不落 command**（凭证不进 detours.jsonl，字段封闭）；
        · P3   心跳的 `session` 是 8 位十六进制哈希，不是会话键原文；
        · E2   SessionStart 自动签到后**重渲染**看板文件（文件里能看到刚签到的行）
  H8  QA Round 2 追加（P2 边界，首 token 自身就是凭证形态）：
        · `fsh_EDGECASE…` / `fsh_EDGECASE….exe` → 只留 `bash:len=N#sha8`，零凭证字节；
        · 反面控制：词表内的 `python fish.py …` 仍留可读摘要 `bash:python#sha8`
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_activity  # noqa: E402

PY = sys.executable


def _run(script, payload, *, data_dir, args=(), timeout=60):
    """真跑一个 hook 脚本，返回 `(exit_code, stdout, stderr)`。"""
    env = dict(os.environ)
    env["FISH_DATA"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("FISH_HOOK_DEBUG", None)
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    proc = subprocess.run([PY, str(HERE / script), *args], input=raw,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=timeout)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


class HookBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-hook-"))
        self.root = self.tmp / "data"
        conn = fish.connect(self.root)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.execute(
            "INSERT INTO workspace_map (path_prefix,project,note,created_at) "
            "VALUES ('i:/proj','PJ',NULL,?)", (fish.now_utc(),))
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self):
        return fish.connect(self.root)

    def _activity_rows(self):
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT host, session_key, tool_name, action, path, path_dir "
                "FROM tool_activity ORDER BY id").fetchall()
        finally:
            conn.close()

    def _payload(self, tool, tool_input, *, session="h1", cwd="i:/proj", response=None):
        return {"session_id": session, "cwd": cwd, "tool_name": tool,
                "tool_input": tool_input, "tool_response": response}


class CollectTest(HookBase):
    """H1：DSH 侧采集（`fish_push_hook --collect-activity`）。"""

    def test_write_recorded_read_not(self):
        code, out, err = _run("fish_push_hook.py", self._payload(
            "write", {"file_path": "i:/proj/p0/a.py", "content": "x"}), data_dir=self.root,
            args=("--host", "dsh", "--collect-activity"))
        self.assertEqual(code, 0, f"hook 必须 exit 0：{err}")
        rows = self._activity_rows()
        self.assertEqual(len(rows), 1, f"写类应落 1 行，实际 {rows}")
        self.assertEqual(rows[0][0], "dsh")
        self.assertEqual(rows[0][1], "dsh:h1")
        self.assertEqual(rows[0][3], "write")
        self.assertEqual(rows[0][4], "p0/a.py")

        _run("fish_push_hook.py",
             self._payload("read", {"file_path": "i:/proj/p0/a.py"}),
             data_dir=self.root, args=("--host", "dsh"))
        self.assertEqual(len(self._activity_rows()), 1, "读类不该落行")

    def test_bash_strong_feature(self):
        _run("fish_push_hook.py",
             self._payload("bash", {"command": "echo hi > out.txt"}),
             data_dir=self.root, args=("--host", "dsh"))
        rows = self._activity_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "exec")
        self.assertEqual(rows[0][4], "out.txt")


class PrivacyTest(HookBase):
    """H2：两处历史隐患的**唯一**判据（内容与凭证都不许落盘）。"""

    def test_content_never_on_disk(self):
        code, out, err = _run("fish_posttool_hook.py", self._payload(
            "write", {"file_path": "i:/proj/_smoke.txt",
                      "content": "SECRET-XYZ-123"}), data_dir=self.root,
            args=("--host", "workbuddy"))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "", "采集/绕路/推送都不该有输出（这里没到推送点）")
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix not in ("", ".json", ".jsonl", ".sqlite",
                                                         ".sqlite-wal", ".sqlite-shm"):
                continue
            raw = path.read_bytes()
            self.assertEqual(raw.count(b"SECRET-XYZ-123"), 0,
                             f"内容出现在了 {path.name}（隐私红线）")

    def test_token_never_in_heartbeat(self):
        payload = {
            "session_id": "h2", "cwd": "i:/proj", "tool_name": "write",
            "tool_input": {"file_path": "i:/proj/a.py",
                           "content": "token=fsh_ABCdef0123456789_verylongsecretvalue"},
        }
        code, _out, _err = _run("fish_posttool_hook.py", payload, data_dir=self.root,
                                args=("--host", "workbuddy"))
        self.assertEqual(code, 0)
        beats = list((self.root / "_audit" / "hooks").glob("heartbeat-*.json"))
        self.assertTrue(beats, "心跳文件必须存在（诊断 hook 是否跑过）")
        for beat in beats:
            text = beat.read_text(encoding="utf-8")
            self.assertNotIn("fsh_", text, f"心跳里出现了凭证：{text}")
            self.assertNotIn("fsh_ABCdef", text)
            data = json.loads(text)
            self.assertIn("event", data)
            self.assertIn("ts", data)

    def test_detour_log_has_no_content(self):
        """绕路审计只写路径（H2）：写一个**含 SECRET 内容**但路径不绕路的调用。"""
        _run("fish_posttool_hook.py", self._payload(
            "write", {"file_path": "i:/proj/data/projects/x.md",
                      "content": "SECRET-XYZ-999"}), data_dir=self.root,
            args=("--host", "workbuddy"))
        log = self.root / "_audit" / "detours.jsonl"
        if log.exists():
            text = log.read_text(encoding="utf-8")
            self.assertNotIn("SECRET-XYZ-999", text, "绕路日志不许落内容")
            row = json.loads(text.splitlines()[0])
            self.assertEqual(set(row), {"ts", "tool", "path", "session", "host"},
                             "绕路日志字段是封闭的（新增字段必须先过隐私评审）")

    # ── QA Round 1 追加的三条判据（E1.8 / P3）────────────────────────────

    # 假凭证样本（**故意显眼**）：任何 secret scanner 都该一眼看出这是夹具，
    # 而不是把它当成真 token 拦下来。真 token 是 fsh_ + 43 位，这里只有 30 位。
    LEAK_TOKEN = "fsh_FAKE_FIXTURE_NOT_A_REAL_TOKEN_0000"

    def test_detour_log_never_writes_command(self):
        """E1.8：`command` 只参与**匹配**，绝不落盘（设计 §10.3）。

        真实现场（QA 抓到的那条）：`python fish.py session end --token fsh_…`。
        命令里既命中 marker（`fish.py`）又带凭证。期望：
          · 仍然留一行审计（绕路确实发生了——别把功能一起砍掉）；
          · `path` 是脱敏形态 `bash:<名>#<sha8>`，不含命令参数；
          · 整个数据目录里**没有凭证字节**。
        """
        code, out, err = _run("fish_posttool_hook.py", self._payload(
            "bash", {"command": f"python fish.py session end --token {self.LEAK_TOKEN}"},
            session="h2-cmd"), data_dir=self.root, args=("--host", "workbuddy"))
        self.assertEqual(code, 0, err)
        log = self.root / "_audit" / "detours.jsonl"
        self.assertTrue(log.exists(), "marker 命中必须留一行审计（否则等于关掉绕路检测）")
        text = log.read_text(encoding="utf-8")
        self.assertEqual(text.count(self.LEAK_TOKEN), 0, f"凭证被写进审计：{text}")
        row = json.loads(text.splitlines()[0])
        self.assertEqual(set(row), {"ts", "tool", "path", "session", "host"})
        self.assertTrue(row["path"].startswith("bash:"),
                        f"Bash 的落盘形态应是 bash:<名|len>#<sha8>：{row['path']!r}")
        self.assertNotIn("session end", row["path"], "命令参数不许进 path")
        self.assertNotIn("--token", row["path"])
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            self.assertNotIn(self.LEAK_TOKEN.encode(), raw,
                             f"凭证出现在 {path.name}（E1.8 隐私红线）")

    def test_detour_log_keeps_path_fields(self):
        """E1.8 的反面：正常路径仍然原样记录（修复不许顺手废掉审计）。"""
        _run("fish_posttool_hook.py", self._payload(
            "write", {"file_path": "i:/proj/data/projects/x.md",
                      "content": "SECRET-XYZ-PATH"}, session="h2-path"),
            data_dir=self.root, args=("--host", "workbuddy"))
        text = (self.root / "_audit" / "detours.jsonl").read_text(encoding="utf-8")
        row = json.loads(text.splitlines()[0])
        self.assertEqual(row["path"], "i:/proj/data/projects/x.md")
        self.assertNotIn("SECRET-XYZ-PATH", text)

    def test_heartbeat_session_is_short_hash(self):
        """P3：心跳的 `session` 是 **8 位十六进制哈希**，不是会话键原文（§5.3/§10.3）。"""
        seen = {}
        for sid in ("h2d-a", "h2d-b"):
            _run("fish_posttool_hook.py",
                 self._payload("write", {"file_path": "i:/proj/a.py"}, session=sid),
                 data_dir=self.root, args=("--host", "workbuddy"))
            beat = (self.root / "_audit" / "hooks" /
                    "heartbeat-workbuddy-PostToolUse.json")
            data = json.loads(beat.read_text(encoding="utf-8"))
            self.assertRegex(data["session"], r"^[0-9a-f]{8}$",
                             f"心跳 session 必须是 8 位十六进制哈希：{data['session']!r}")
            self.assertNotIn("h2d-", json.dumps(data), "会话键原文不许进心跳")
            seen[sid] = data["session"]
        self.assertNotEqual(seen["h2d-a"], seen["h2d-b"],
                            "不同会话要得到不同哈希（否则判据分不清是谁的心跳）")

    # ── QA Round 2 追加（P2 边界：首位 token 自己就是凭证形态）──────────────

    EDGE_TOKEN = "fsh_EDGECASE1234567890"

    def test_detour_edge_token_not_echoed(self):
        """P2 边界：**首位 token 自己就是凭证形态**时，同样不许原文落盘。

        现场：`fsh_EDGECASE1234567890 data/projects` —— 首 token 全落在旧的
        「ASCII 字母数字 `._-` + ≤40 字符」字符集内，旧判据放行，审计落
        `bash:fsh_EDGECASE1234567890#<sha8>`（凭证形态原文泄漏）。
        现在的判据：名字只能来自**固定命令表**，查不到一律 `bash:len=N#sha8`，
        所以审计行里没有任何一段字节来自入参。
        第二条命令是同一个口子的另一半：`.exe` 归一化不得把
        `fsh_EDGECASE1234567890.exe` 变成词表命中。
        """
        cmds = (f"{self.EDGE_TOKEN} data/projects",
                f"{self.EDGE_TOKEN}.exe data/projects")
        for i, cmd in enumerate(cmds):
            code, _out, err = _run("fish_posttool_hook.py", self._payload(
                "bash", {"command": cmd}, session=f"h2-edge-{i}"),
                data_dir=self.root, args=("--host", "workbuddy"))
            self.assertEqual(code, 0, err)
        log = self.root / "_audit" / "detours.jsonl"
        self.assertTrue(log.exists(), "marker 命中必须留一行审计（不许顺手关掉检测）")
        text = log.read_text(encoding="utf-8")
        self.assertNotIn("EDGECASE", text, f"凭证形态原文进了审计：{text}")
        self.assertNotIn(self.EDGE_TOKEN, text)
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(len(rows), 2, f"两条命令各应留一行：{rows}")
        for row in rows:
            self.assertEqual(set(row), {"ts", "tool", "path", "session", "host"},
                             "绕路日志字段是封闭的（新增字段必须先过隐私评审）")
            self.assertRegex(row["path"], r"^bash:len=\d+#[0-9a-f]{8}$",
                             f"不在词表就该退化成 bash:len=N#sha8：{row['path']!r}")
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            self.assertNotIn(self.EDGE_TOKEN.encode(), raw,
                             f"凭证出现在 {path.name}（P2 隐私红线）")

    def test_detour_known_command_stays_readable(self):
        """P2 的反面控制：词表里的命令名仍可读（修复不许把审计砍成纯数字）。

        `python fish.py …` 是本项目最常见的绕路形态；如果连它都退化成 `len=N`，
        审计的可读性就归零。这条闸同时钉住"可读 ≠ 可以带参数/凭证"。
        """
        code, _out, err = _run("fish_posttool_hook.py", self._payload(
            "bash", {"command": f"python fish.py session end --token {self.LEAK_TOKEN}"},
            session="h2-known"), data_dir=self.root, args=("--host", "workbuddy"))
        self.assertEqual(code, 0, err)
        log = self.root / "_audit" / "detours.jsonl"
        self.assertTrue(log.exists(), "marker 命中必须留一行审计")
        text = log.read_text(encoding="utf-8")
        row = json.loads(text.splitlines()[0])
        self.assertRegex(row["path"], r"^bash:python#[0-9a-f]{8}$",
                         f"词表内的解释器名应当保留：{row['path']!r}")
        self.assertNotIn(self.LEAK_TOKEN, text, "可读 ≠ 可以带参数/凭证")


class SessionStartRenderTest(HookBase):
    """R9（QA Round 1 · E2）：SessionStart 自动签到后，**看板文件**必须是新的。

    真实现场：hook 把 `presence` 写成 1 人，而 `看板-*.md` 仍显示「在场（0 人）」。
    这里**真跑** `fish_session_hook.py`（不是调函数）：判据要看的是 hook 这条链路
    有没有把渲染带上，而不是某个函数能不能渲染。
    """

    CID = "01SESSIONSTARTRENDER0000E5"

    def _register(self, session_key):
        """登记一个**已存在**的会话公民（hook 只查身份、不签发，见其模块 docstring）。"""
        conn = self._conn()
        try:
            now = fish.now_utc()
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host)"
                " VALUES (?,?,'resident','active',?,'workbuddy')",
                (self.CID, "QA-自动行", now))
            conn.execute(
                "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,"
                "channel,issued_at) VALUES ('deadbeefcafe',?,?,'machine','hook',?)",
                (self.CID, session_key, now))
            conn.commit()
        finally:
            conn.close()

    def test_hook_renders_board_after_auto_checkin(self):
        sid = "qa-e2-auto"
        self._register("codebuddy:" + sid)
        code, out, err = _run("fish_session_hook.py",
                              {"session_id": sid, "cwd": "i:/proj"},
                              data_dir=self.root)
        self.assertEqual(code, 0, f"SessionStart hook 必须 exit 0：{err}")
        self.assertIn("hookSpecificOutput", out, f"hook 必须输出协议块：{out[:200]}")
        conn = self._conn()
        try:
            n_presence = conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(n_presence, 1, "自动签到没写 presence，渲染判据就失去意义")
        rendered = sorted(fish.resolve_home_root(self.root).rglob("看板-*.md"))
        self.assertTrue(rendered, "签到之后必须重渲染看板文件（QA E2 的观测点）")
        text = "\n".join(p.read_text(encoding="utf-8") for p in rendered)
        self.assertIn("QA-自动行", text, f"渲染文件里没有刚签到的自动行：{text[:300]}")
        self.assertIn("自动登记", text, "自动行必须带来源文案（不编造 doing）")
        self.assertNotIn("在场（0 人）", text, "文件就是这里被打脸的那份：不许还是 0 人")


class SilenceTest(HookBase):
    """H3：失败路径必须**零输出 + exit 0**（宿主看到的就像没装这个 hook）。"""

    def test_bad_json_all_hooks(self):
        for script in ("fish_push_hook.py", "fish_posttool_hook.py",
                       "fish_prompt_hook.py", "fish_pretool_hook.py"):
            code, out, err = _run(script, "{not json", data_dir=self.root)
            self.assertEqual(code, 0, f"{script} 坏输入必须 exit 0：{err}")
            self.assertEqual(out, "", f"{script} 坏输入必须零输出，实际：{out!r}")

    def test_empty_stdin(self):
        for script in ("fish_push_hook.py", "fish_posttool_hook.py",
                       "fish_prompt_hook.py", "fish_pretool_hook.py"):
            code, out, _err = _run(script, "", data_dir=self.root)
            self.assertEqual(code, 0)
            self.assertEqual(out, "")

    def test_missing_data_dir_does_not_crash(self):
        """数据目录不存在（新机器/未初始化）→ 依然零输出、exit 0。"""
        ghost = self.tmp / "ghost"
        code, out, _err = _run("fish_pretool_hook.py",
                               self._payload("write", {"file_path": "i:/proj/a.py"}),
                               data_dir=ghost)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


class HostEquivalenceTest(HookBase):
    """H4：方言差异只允许体现在会话键前缀上。"""

    def test_both_hosts_record(self):
        for host, session, want_prefix in (("dsh", "s1", "dsh:"),
                                           ("workbuddy", "s2", "codebuddy:")):
            _run("fish_posttool_hook.py",
                 self._payload("write", {"file_path": f"i:/proj/p0/{session}.py"},
                               session=session), data_dir=self.root, args=("--host", host))
        rows = self._activity_rows()
        self.assertEqual(len(rows), 2, rows)
        keys = {r[0]: r[1] for r in rows}
        self.assertEqual(keys["dsh"], "dsh:s1")
        self.assertEqual(keys["workbuddy"], "codebuddy:s2")

    def test_workbuddy_backslash_path(self):
        _run("fish_posttool_hook.py", {
            "session_id": "wb", "cwd": "I:\\proj", "tool_name": "Write",
            "tool_input": {"file_path": "I:\\proj\\p0\\deep\\x.py"}},
            data_dir=self.root, args=("--host", "workbuddy"))
        rows = self._activity_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], "p0/deep/x.py")
        self.assertEqual(rows[0][5], "p0/deep")


class CounterTest(HookBase):
    """H5：共享频率闸（push / prompt 复用同一实现）。"""

    def test_counter_increments_and_tolerates_bom(self):
        n1 = fish_activity.bump_counter(self.root, session_id="dsh:s", event="evt")
        n2 = fish_activity.bump_counter(self.root, session_id="dsh:s", event="evt")
        self.assertEqual((n1, n2), (1, 2))
        path = self.root / "_audit" / "counters" / "evt-dsh_s.n"
        path.write_text("\ufeff7", encoding="utf-8")
        self.assertEqual(
            fish_activity.bump_counter(self.root, session_id="dsh:s", event="evt"), 8,
            "BOM 必须能读（PowerShell 5.1 的 Set-Content 会写 BOM）")

    def test_counter_rejects_bad_every_n(self):
        with self.assertRaises(ValueError):
            fish_activity.bump_counter(self.root, session_id="s", event="e", every_n=0)


class PeriodKeyTest(unittest.TestCase):
    """H6：看板周期键用**本地**日期（+8），否则后半夜的板会错一天。"""

    def test_local_date_matches_fish(self):
        self.assertEqual(fish_activity.local_period_key(), fish.board_period())
        # UTC 20:00 = 本地次日 04:00 → 必须算次日
        from datetime import datetime, timezone
        self.assertEqual(fish_activity.local_period_key("2026-10-25T20:00:00Z"),
                         "261026")


if __name__ == "__main__":
    unittest.main(verbosity=2)
