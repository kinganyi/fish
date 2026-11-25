#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-10（外部审查 2026-11-25）判据：`install_mcp.py` **缺省只读**、写居家目录护栏。

## 修的是什么

旧版：`--check` / `--dry-run` 是只读，**不带任何模式时直接 `return cmd_apply(...)`**
——一次误执行 `python install_mcp.py` 就改 `~/.workbuddy/mcp.json`；`--check` 的帮助
文案还写着"缺省是写"，等于把危险默认写在帮助里。对照 `install_hooks.py`：写类动作
必须 `--apply`，缺省是 check/dry-run。

## 这批判据为什么能失败

  · 缺省档：跑真子进程，断言配置文件**字节不变**、没有 `.bak-*` 备份落盘
    （旧实现缺省就是写 → 这条必红）；
  · `--apply`：断言真的写入并回读（否则"永远不写"也能让上一条变绿）；
  · 护栏：`FISH_WORKBUDDY_CFG` 指到**宿主家目录之外** → 拒绝写、文件不变
    （去掉 `_refuse_unsafe_target` 这条必红）；
  · 占位符/网络路径 → 拒绝（同上）。

所有用例都用临时 `WORKBUDDY_HOME`，**绝不碰真 `~/.workbuddy`**。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "install_mcp.py"


def _run(args, env_extra):
    env = dict(os.environ)
    env.pop("FISH_WORKBUDDY_CFG", None)
    env.update(env_extra)
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=str(HERE), env=env)


class InstallMcpDefaultReadOnlyTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-installmcp-")
        self.home = Path(self._tmp) / "wbhome"
        self.home.mkdir()
        self.cfg = self.home / "mcp.json"
        self.cfg.write_text(json.dumps(
            {"mcpServers": {"other": {"command": "other.exe"}}},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.env = {"WORKBUDDY_HOME": str(self.home)}
        self.before = self.cfg.read_bytes()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _backups(self):
        return sorted(p.name for p in self.home.iterdir() if ".bak-" in p.name)

    # ------------------------------------------------------- 缺省 = 只读
    def test_no_args_writes_nothing(self):
        """**核心**：不带参数 → 零写盘、无备份、退出码是"检查"的语义（0/2）。"""
        r = _run([], self.env)
        self.assertIn(r.returncode, (0, 2),
                      f"缺省档必须是只读检查（0/2），实际 {r.returncode}\n{r.stdout}{r.stderr}")
        self.assertEqual(self.cfg.read_bytes(), self.before,
                         "**无参数运行改写了宿主配置** —— 这正是 P1-10：缺省动作不该是写")
        self.assertEqual(self._backups(), [],
                         f"无参数运行留下了备份文件（说明它走了写档）：{self._backups()}")
        self.assertIn("零写盘", r.stdout, f"缺省档要说清自己没写：{r.stdout!r}")

    def test_check_and_dry_run_are_read_only(self):
        """`--check` / `--dry-run` 仍然只读（旧行为不许被这次改动破坏）。"""
        for flag in ("--check", "--dry-run"):
            r = _run([flag], self.env)
            self.assertIn(r.returncode, (0, 2), f"{flag} 退出码异常：{r.returncode}")
            self.assertEqual(self.cfg.read_bytes(), self.before, f"{flag} 改写了配置")

    # ------------------------------------------------------- --apply 才写
    def test_apply_writes_and_reads_back(self):
        """对照组：`--apply` 必须真的写入并回读校验（否则"永远不写"也能让上面变绿）。"""
        r = _run(["--apply"], self.env)
        self.assertEqual(r.returncode, 0, f"--apply 失败：\n{r.stdout}\n{r.stderr}")
        self.assertNotEqual(self.cfg.read_bytes(), self.before, "--apply 没写入")
        obj = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertIn("fish", obj["mcpServers"], "--apply 后没有 fish 条目")
        self.assertIn("other", obj["mcpServers"], "--apply 丢了原有的其它 server")
        self.assertTrue(self._backups(), "--apply 必须先备份再写（没找到 .bak 文件）")
        self.assertIn("回读校验通过", r.stdout, r.stdout)

    # ------------------------------------------------------- 家目录护栏
    def test_refuses_target_outside_home(self):
        """`FISH_WORKBUDDY_CFG` 指到宿主家目录之外 → 拒绝写、文件不变。"""
        outside = Path(self._tmp) / "elsewhere" / "mcp.json"
        outside.parent.mkdir(parents=True)
        outside.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        before = outside.read_bytes()
        r = _run(["--apply"], {**self.env, "FISH_WORKBUDDY_CFG": str(outside)})
        self.assertEqual(r.returncode, 2,
                         f"越界目标必须拒绝（2），实际 {r.returncode}\n{r.stdout}{r.stderr}")
        self.assertEqual(outside.read_bytes(), before, "越界目标被写了")
        self.assertIn("不在宿主家目录", r.stdout,
                      f"拒绝理由要说清越界：{r.stdout!r}")

    def test_refuses_network_path(self):
        """网络路径（UNC）一律拒绝：远程共享是另一个信任面。"""
        r = _run(["--apply"],
                 {**self.env, "FISH_WORKBUDDY_CFG": "\\\\server\\share\\mcp.json"})
        self.assertEqual(r.returncode, 2, f"UNC 路径必须拒绝：{r.returncode}\n{r.stdout}")
        self.assertIn("网络路径", r.stdout, f"拒绝理由要说清是网络路径：{r.stdout!r}")

    def test_refuses_placeholder_path(self):
        """含未解析占位符的路径一律拒绝（写进去宿主永远读不到）。"""
        r = _run(["--apply"],
                 {**self.env, "FISH_WORKBUDDY_CFG": str(self.home / "%NOPE%" / "mcp.json")})
        self.assertEqual(r.returncode, 2, f"占位符路径必须拒绝：{r.returncode}\n{r.stdout}")
        self.assertIn("占位符", r.stdout, f"拒绝理由要说清占位符：{r.stdout!r}")

    def test_file_flag_is_explicit_and_still_works(self):
        """`--file` 是显式指定（演练/测试入口）：越界也允许，但**只写那一个文件**。"""
        other = Path(self._tmp) / "drill" / "mcp.json"
        other.parent.mkdir(parents=True)
        other.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        r = _run(["--apply", "--file", str(other)], self.env)
        self.assertEqual(r.returncode, 0, f"--file 演练失败：\n{r.stdout}\n{r.stderr}")
        self.assertIn("fish", json.loads(other.read_text(encoding="utf-8"))["mcpServers"])
        self.assertEqual(self.cfg.read_bytes(), self.before,
                         "`--file` 演练把**真配置**也改了——那是 P1-10 更坏的一种")

    # ------------------------------------------------------- 帮助文案
    def test_help_says_default_is_read_only(self):
        """帮助必须与行为一致（旧文案写"缺省是写"，正相反）。"""
        r = _run(["--help"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("缺省只读", r.stdout, f"帮助没声明缺省只读：{r.stdout[:300]}")
        self.assertNotIn("缺省是写", r.stdout, "帮助还在写「缺省是写」——与行为相反")


if __name__ == "__main__":
    unittest.main(verbosity=2)
