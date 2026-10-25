#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`session start --cwd` 与 hook 传 cwd 的判据（2026-10-24 接线）。

背景
----
R1 的根治需要「项目由配置决定，不是猜」。链条是：

    hook payload.cwd
      → fish session start --cwd <path>
        → resolve_project_by_cwd（workspace_map 最长前缀 + 分量边界）
          → 开项目档期

**为什么不让 hook 自己查再传 --project**：多一次进程启动、多一处可能忘传；
放进 CLI 后，任何调用方（hook / AI / 脚本）给 cwd 就能拿到正确项目。

判据
----
C1  --cwd 命中映射 → 开档期，且 project 字段是映射的项目
C2  --cwd 无匹配   → **不开档期**（返回 None 而不是猜）
C3  --project 压过 --cwd（显式声明最高优先）
C4  stderr 不得污染 stdout 的 key:value 契约（诊断句必须走 stderr）
C5  hook 端到端：payload.cwd → 档期真的落到库里
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fish

PY = sys.executable
P0 = Path(__file__).resolve().parent
FISH_PY = str(P0 / "fish.py")
HOOK_PY = str(P0 / "fish_dsh_hook.py")

MAPPING = "i:/app/dsh-home/workspace/learn/大肥鱼生命系统"
CWD_HIT = "I:/app/DSH-Home/workspace/learn/大肥鱼生命系统/p0"
CWD_MISS = "C:/some/other/place"


class SessionCwdTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        fish.set_workspace_mapping(conn, MAPPING, "ANYI")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 工具
    def _run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # 关键：--root 传 **data 目录本身**（connect(root) 的 root 就是 data）
        return subprocess.run(
            [PY, FISH_PY, "--root", str(self.data), "session", "start", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)

    def _fields(self, stdout):
        out = {}
        for line in (stdout or "").splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
        return out

    def _stints(self):
        c = fish.connect(self.data)
        rows = list(c.execute("SELECT project, to_ts FROM project_stints"))
        c.close()
        return rows

    # ------------------------------------------------------------------ C1
    def test_C1_cwd_hit_opens_stint(self):
        """--cwd 命中映射 → 开档期，project 字段是映射的项目。"""
        r = self._run_cli("--cwd", CWD_HIT, "--alias", "c1")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr[:300]}")
        f = self._fields(r.stdout)
        self.assertEqual(f.get("project"), "ANYI",
                         f"--cwd 命中却没解析出项目；stdout={r.stdout[:300]}")
        stints = self._stints()
        self.assertEqual(len(stints), 1, f"应开 1 条档期，实际 {stints}")
        self.assertEqual(stints[0][0], "ANYI")

    # ------------------------------------------------------------------ C2
    def test_C2_cwd_miss_opens_nothing(self):
        """--cwd 无匹配 → 不开档期（宁可没有，不猜）。"""
        r = self._run_cli("--cwd", CWD_MISS, "--alias", "c2")
        self.assertEqual(r.returncode, 0)
        f = self._fields(r.stdout)
        self.assertEqual(f.get("project"), "-",
                         f"无匹配却开了项目（说明在猜）；stdout={r.stdout[:300]}")
        self.assertEqual(self._stints(), [], "无匹配时不该留下任何档期")

    # ------------------------------------------------------------------ C3
    def test_C3_project_overrides_cwd(self):
        """显式 --project 压过 --cwd。"""
        r = self._run_cli("--cwd", CWD_HIT, "--project", "CDQL", "--alias", "c3")
        self.assertEqual(r.returncode, 0)
        f = self._fields(r.stdout)
        self.assertEqual(f.get("project"), "CDQL",
                         f"--project 应压过 --cwd；stdout={r.stdout[:300]}")
        projects = [s[0] for s in self._stints()]
        self.assertIn("CDQL", projects)
        self.assertNotIn("ANYI", projects, "--cwd 解析的项目不该在被 --project 压过时开档")

    # ------------------------------------------------------------------ C4
    def test_C4_diagnostic_goes_to_stderr_not_stdout(self):
        """诊断句必须走 stderr：stdout 是 key:value 契约面，混进带冒号的 prose 会污染解析。"""
        r = self._run_cli("--cwd", CWD_MISS, "--alias", "c4")
        self.assertIn("未匹配", r.stderr, "诊断句应在 stderr")
        self.assertNotIn("未匹配", r.stdout, "诊断句不能出现在 stdout")
        # stdout 的每一行要么是 key: value，要么是已知的括号说明行
        for line in (r.stdout or "").splitlines():
            if not line.strip():
                continue
            if ":" in line:
                k = line.split(":", 1)[0].strip()
                self.assertNotIn("（", k, f"stdout 混入了非字段行: {line!r}")

    # ------------------------------------------------------------------ C5
    def test_C5_hook_end_to_end(self):
        """hook 端到端：payload.cwd → 档期落到库里。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["FISH_ROOT"] = str(self.data)   # ← 传 data 目录本身

        payload = {"session_id": "hook-e2e-c5", "cwd": CWD_HIT,
                   "hook_event_name": "SessionStart"}
        r = subprocess.run([PY, HOOK_PY], input=json.dumps(payload),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ANYI", ctx, f"hook 注入内容里应带项目；ctx={ctx[:300]}")

        stints = self._stints()
        self.assertEqual(len(stints), 1, f"hook 应开出 1 条档期，实际 {stints}")
        self.assertEqual(stints[0][0], "ANYI")


if __name__ == "__main__":
    unittest.main(verbosity=2)
