#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T1 判据：cwd 解析不出项目时，会话落到兜底项目「未分类」。

背景
----
交接清单 T1 写的是"hook 落「未分类」"，但代码实况是 `cmd_session_start` 在
cwd 未匹配时**干脆不开档期**（原注释："宁可没有，不猜"）。

后果不是"少一条记录"，而是 `project_stints` 在生产库**恒为 0**——
"这个会话什么时候在哪个项目干活"这条最基础的事实，系统一条都没记过
（2026-10-25 实测：`workspace_map` 只有 1 条映射，其余 cwd 一律匹配不到）。

决策 12 已经把「生活 / 工作 / 未分类」定成**真实项目**，所以"落兜底"不是猜：
兜底项目的语义本身就是"我不知道"。但**诚实信号不能丢**——L1 必须标注出来，
否则 AI 看到「当前项目：未分类」会把它当成一个正常项目名。

判据
----
F1  库里有「未分类」前缀 → cwd 未匹配时开该档期，stdout 的 project 字段是它
F2  库里**没有**「未分类」→ 维持旧行为：不开档期
    （名字是数据不是代码：代码不许凭空造一个项目，那是往库里写假事实）
F3  L1 渲染「未分类」时带"兜底项目"标注
F4  L1 渲染真项目时**不带**该标注（防标注泛滥）
F5  变异：把兜底名改掉后渲染「未分类」→ F3 的标注必须消失
    （证明 F3 命中的是"兜底"这个语义，而不是"任何项目行都会追加一句"）

跑法
----
    python test_fallback_project.py -v
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

PY = sys.executable
P0 = Path(__file__).resolve().parent
FISH_PY = str(P0 / "fish.py")

CWD_MISS = "C:/some/other/place/with-no-mapping"


class FallbackProjectCliTest(unittest.TestCase):
    """F1 / F2：CLI 层的落点。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, prefix):
        c = fish.connect(self.data)
        c.execute("INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,"
                  "last_seq,status) VALUES (?,?,NULL,0,'active')", (prefix, prefix))
        c.commit()
        c.close()

    def _run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
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

    # ---------------------------------------------------------------- F1
    def test_F1_falls_back_when_prefix_registered(self):
        """F1：库里登记过「未分类」→ cwd 未匹配时开该档期。"""
        self._register(fish.FALLBACK_PROJECT_NAME)
        r = self._run_cli("--cwd", CWD_MISS, "--alias", "f1")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr[:300]}")
        f = self._fields(r.stdout)
        self.assertEqual(f.get("project"), fish.FALLBACK_PROJECT_NAME,
                         f"没落到兜底项目；stdout={r.stdout[:400]}")
        stints = self._stints()
        self.assertEqual(len(stints), 1, f"应开 1 条兜底档期，实际 {stints}")
        self.assertEqual(stints[0][0], fish.FALLBACK_PROJECT_NAME)

    # ---------------------------------------------------------------- F2
    def test_F2_no_fallback_when_prefix_absent(self):
        """F2：库里没有「未分类」→ 不开档期（不许在代码里凭空造项目）。"""
        r = self._run_cli("--cwd", CWD_MISS, "--alias", "f2")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr[:300]}")
        f = self._fields(r.stdout)
        self.assertEqual(f.get("project"), "-",
                         f"库里没这个前缀却落了项目（凭空造）；stdout={r.stdout[:400]}")
        self.assertEqual(self._stints(), [], "库没登记兜底项目时不该留下档期")


class FallbackProjectRenderTest(unittest.TestCase):
    """F3 / F4 / F5：L1 渲染上的诚实标注。"""

    # ---------------------------------------------------------------- F3
    def test_F3_fallback_line_is_annotated(self):
        """F3：渲染兜底项目时必须带"兜底项目"标注。"""
        line = fish._render_project_head(fish.FALLBACK_PROJECT_NAME, 3, "", "stint")
        self.assertIn("兜底项目", line,
                      f"兜底项目没被标注——AI 会把它当成正常项目；实际：{line!r}")
        self.assertIn("workspace set", line, "标注里应给出补救动作")

    # ---------------------------------------------------------------- F4
    def test_F4_real_project_line_is_clean(self):
        """F4：真项目不许带兜底标注（防标注泛滥——每条都加等于没加）。"""
        line = fish._render_project_head("ANYI", 107, "2026-10-25T00:00:00Z", "stint")
        self.assertNotIn("兜底项目", line, f"真项目被误标成兜底：{line!r}")
        self.assertIn("当前项目：ANYI", line)

    # ---------------------------------------------------------------- F5 变异
    def test_F5_mutation_annotation_follows_constant(self):
        """F5 变异自证：把兜底名改成别的值，渲染「未分类」就不再带标注。

        若改掉常量后标注仍在，说明 F3 命中的不是"兜底语义"而是"所有项目行都会追加"，
        那 F4 的反向断言也就失去意义了。
        """
        with mock.patch.object(fish, "FALLBACK_PROJECT_NAME", "别的名字"):
            line = fish._render_project_head("未分类", 3, "", "stint")
        self.assertNotIn("兜底项目", line,
                         f"改了常量却仍标注——F3 命中的不是兜底语义：{line!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
