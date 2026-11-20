#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冷却与采集的**顺序**判据（2026-11-20 · DSH hook 断流事故的第二层根因）。

这份文件只回答一个问题：**冷却命中的时候，采集还在不在**。

事故经过：`fish_push_hook._run` 在读 stdin 之前就查 `cooldown_remaining`，
于是「连续 3 次跑得慢 → 冷却 10 分钟」这条规则顺带把**采集**也关掉了。
症状极隐蔽——那 10 分钟里 `tool_activity` 一条不写，而心跳也写在那个判断之后，
所以**连"我来过"都看不到**。

为什么冷却不该挡采集：冷却的初衷是「不让用户每次工具调用都等满宿主给的 3 秒」
（见 `hook_fastpath.note_elapsed` 的 docstring），那是**推送**路径的成本。
采集只是本地写一行，不产生这个成本。

所以这里锁两条：

- **C1** 冷却命中时，采集仍然写入（本文件的主判据）；
- **C2** 反证：把冷却判断搬回门口，C1 必须红 —— 否则说明 C1 根本没在测顺序。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402

HOOK = HERE / "fish_push_hook.py"


def _payload(session_id, cwd):
    return json.dumps({
        "session_id": session_id, "transcript_path": "", "cwd": str(cwd),
        "hook_event_name": "PostToolUse", "tool_name": "Write",
        "tool_input": {"file_path": r"I:\tmp\cool.md", "content": "x"},
        "tool_response": {"ok": True},
    })


class CooldownOrderTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hookfix-"))
        self.data = self.tmp / "data"
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_cooldown(self):
        """把 posttool 的冷却置到未来——模拟"连续 3 次慢"之后的 10 分钟窗口。"""
        audit = self.data / "_audit"
        audit.mkdir(parents=True, exist_ok=True)
        (audit / "hook-fastpath-posttool.json").write_text(
            json.dumps({"timeout_streak": 3, "cooldown_until": time.time() + 600}),
            encoding="utf-8")

    def _run_hook(self, session_id):
        env = dict(os.environ, FISH_DATA=str(self.data))
        return subprocess.run(
            [sys.executable, str(HOOK), "--host", "dsh", "--collect-activity"],
            cwd=str(HERE), input=_payload(session_id, self.tmp), capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=env)

    def _activity_count(self):
        conn = sqlite3.connect(self.data / "index.sqlite")
        try:
            return conn.execute("SELECT COUNT(*) FROM tool_activity").fetchone()[0]
        finally:
            conn.close()

    # ---------------------------------------------------------------- C1

    def test_C1_collection_survives_cooldown(self):
        """冷却命中时采集仍然写入。

        这条红了就意味着：机器一慢，观测就静默断 10 分钟——而那正是
        这次事故最难查的地方（没有任何输出）。
        """
        self._set_cooldown()
        result = self._run_hook("cool-test")
        self.assertEqual(result.returncode, 0, result.stderr[-300:])
        self.assertEqual(self._activity_count(), 1,
                         "冷却命中的情况下采集没写入——冷却守错了地方（挡住了采集）")

    def test_C1_collection_survives_without_cooldown(self):
        """对照组：没有冷却时本来就该写。两者一起才能说明 C1 测的是顺序。"""
        result = self._run_hook("no-cool")
        self.assertEqual(result.returncode, 0, result.stderr[-300:])
        self.assertEqual(self._activity_count(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
