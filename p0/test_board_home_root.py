#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C3 · 看板落点迁移到 `fish-home` 的判据（2026-10-26）。

背景（裁决 C3）：`boards.path` 从"相对 `data_root`"改成"相对 `home_root`"，
渲染写 `home_root / path`。三个 scope 的新口径：

    root    → `00-首页/看板-<pk>.md`
    project → `10-项目库/<项目>/看板-<pk>.md`
    branch  → `10-项目库/_分支-<名>/看板-<pk>.md`（**一级**，不是 `_分支/<名>` 两级）

## 变异自证（X-1）

变异 D —— 对应 `test_root_board_lands_in_home`：
  把 `board_relpath` 的 root 分支改回 `看板-<pk>.md`（去掉 `00-首页/` 前缀）→
  路径断言红（且 `board_render` 会写回 `fish-home/看板-*.md`，落点越界）。

变异 E —— 对应 `test_branch_board_depth3`：
  把 branch 分支写成 PRD 原文的 `10-项目库/_分支/<名>/看板-….md`（4 层）→
  **路径断言红 + `layout verify` 的 `depth_exceeded` 也必须出现**。
  这条变异专证"深度判据真的在跑"，而不是被路径断言掩盖。

纪律（K12）：`tempfile.mkdtemp()` 隔离；**不指向 `p0/data`**。
"""
import contextlib
import datetime
import importlib.util
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

CID = "01BOARDHOME00000000000001"


class BoardHomeRootTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-board-home-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (CID, "看板落点测试", "resident", "member", None, "active",
             fish.now_utc()))
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('P1','项目一',NULL,0,'active')")
        self.conn.commit()
        self.home = fish.resolve_home_root(self.root, conn=self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, argv):
        old_argv = sys.argv
        sys.argv = ["fish.py"] + argv
        out, err = io.StringIO(), io.StringIO()
        code = None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    fish.main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            sys.argv = old_argv
        return code, out.getvalue(), err.getvalue()

    def _snapshot(self, base):
        out = {}
        base = Path(base)
        for p in sorted(base.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[p.relative_to(base).as_posix()] = (st.st_size, st.st_mtime_ns)
        return out

    # ------------------------------------------------------------- 三个 scope
    def test_root_board_lands_in_home(self):
        """**变异 D**：根板落 `00-首页/看板-<pk>.md`，不落 `data_root`。"""
        board = fish.board_ensure(self.conn, "root", "")
        path, size, split = fish.board_render(self.conn, self.root, board)
        self.assertEqual(path, f"00-首页/看板-{board['period_key']}.md",
                         "根板新口径必须是 00-首页/看板-<pk>.md")
        target = self.home / path
        self.assertTrue(target.exists(), "看板文件必须落在 fish-home 下")
        self.assertFalse((self.root / path).exists(),
                         f"看板不该再写进 data_root：{self.root / path}")
        self.assertGreater(size, 0)
        self.assertFalse(split)

    def test_project_board_lands_in_home(self):
        board = fish.board_ensure(self.conn, "project", "P1")
        path, _size, _split = fish.board_render(self.conn, self.root, board)
        self.assertEqual(path, f"10-项目库/P1/看板-{board['period_key']}.md")
        self.assertTrue((self.home / path).exists())
        self.assertEqual(
            self.conn.execute("SELECT path FROM boards WHERE id=?",
                              (board["id"],)).fetchone()[0], path)

    def test_branch_board_depth3(self):
        """**变异 E**：分支板落 `10-项目库/_分支-<k>/…`，**且** verify 对它 0 条 depth。

        两条必须同时断言：只断路径不验 verify = 没钉住"4 层撞 HOME-1"这个矛盾。
        """
        board = fish.board_ensure(self.conn, "branch", "feat-x")
        path, _size, _split = fish.board_render(self.conn, self.root, board)
        expected = f"10-项目库/_分支-feat-x/看板-{board['period_key']}.md"
        self.assertEqual(path, expected, "分支看板必须扁平化为 _分支-<名> 一级")
        target = self.home / path
        self.assertTrue(target.exists())
        self.assertEqual(len(Path(path).parts), 3,
                         f"分支看板相对 fish-home 必须是 3 层，实际 {Path(path).parts}")

        # 再把整座门面渲染出来（README/系统页等），然后体检：
        # depth_exceeded 必须为 0——分支看板不能被自己的体检判红。
        fish.render_layout(self.conn, self.root,
                           as_of=datetime.datetime.now().astimezone())
        ver = fish.verify_layout(self.conn, self.root)
        depth_hits = [it for it in ver["items"] if it["code"] == "depth_exceeded"]
        self.assertEqual(depth_hits, [],
                         f"分支看板不该触发 depth_exceeded：{depth_hits}")
        self.assertEqual(ver["errors"], 0, f"items={ver['items']}")

    def test_board_out_path_matches_render(self):
        """唯一落点入口与真实写盘路径必须一致（否则测试与实现各钉各的）。"""
        board = fish.board_ensure(self.conn, "project", "P1")
        out = fish.board_out_path(self.root, board)
        self.assertEqual(out, self.home / board["path"])
        path, _size, _split = fish.board_render(self.conn, self.root, board)
        self.assertEqual(out, self.home / path)
        self.assertTrue(out.exists())

    def test_home_root_keyword_override(self):
        """`home_root=` 是仅关键字参数：给了就往给定根写，不再解析默认根。"""
        alt = self.tmp / "alt-home"
        board = fish.board_ensure(self.conn, "project", "P1")
        path, _size, _split = fish.board_render(self.conn, self.root, board,
                                                home_root=alt)
        self.assertTrue((alt / path).exists(), "显式 home_root 应生效")
        self.assertFalse((self.home / path).exists(), "不该同时写默认门面根")

    # ------------------------------------------------------------- 迁移
    def test_ensure_boards_home_paths_migrates_legacy_rows(self):
        """旧口径行（`projects/<项目>/看板-….md`）→ 新口径；重复执行零写入。"""
        pk = "261026"
        self.conn.execute(
            "INSERT INTO boards (scope,scope_key,period_key,path,status,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("project", "PA", pk, f"projects/PA/看板-{pk}.md", "open",
             fish.now_utc()))
        self.conn.commit()
        changed = fish._ensure_boards_home_paths(self.conn)
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["from"], f"projects/PA/看板-{pk}.md")
        self.assertEqual(changed[0]["to"], f"10-项目库/PA/看板-{pk}.md")
        row = self.conn.execute(
            "SELECT path FROM boards WHERE scope_key='PA'").fetchone()[0]
        self.assertEqual(row, f"10-项目库/PA/看板-{pk}.md")
        self.assertEqual(fish._ensure_boards_home_paths(self.conn), [],
                         "第二次迁移必须是空操作")

    def test_migrate_normalizes_board_paths(self):
        """真实旧库路径：executescript(SCHEMA) → migrate() 就把 7 行口径归一。

        这里用一条旧行模拟：migrate 之后 `boards.path` 必须已经符合 board_relpath。
        """
        pk = "261025"
        self.conn.execute(
            "INSERT INTO boards (scope,scope_key,period_key,path,status,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("branch", "feat-y", pk, f"boards/branch/feat-y/看板-{pk}.md", "open",
             fish.now_utc()))
        self.conn.commit()
        fish.migrate(self.conn)
        row = self.conn.execute(
            "SELECT path FROM boards WHERE scope='branch' AND scope_key='feat-y'"
        ).fetchone()[0]
        self.assertEqual(row, f"10-项目库/_分支-feat-y/看板-{pk}.md")

    # ------------------------------------------------------------- CLI 全量渲染
    def test_cli_render_all_writes_home_and_not_data_root(self):
        """`board render --all`：全部板落到 fish-home，data_root 零写入。"""
        fish.board_checkin(self.conn, CID, "做 A", scope="project", scope_key="P1")
        fish.board_ensure(self.conn, "root", "")
        before = self._snapshot(self.root)
        code, out, err = self._run_main(
            ["--root", str(self.root), "board", "render", "--all"])
        after = self._snapshot(self.root)
        self.assertIsNone(code, f"render --all 应 exit 0；stderr={err}")
        self.assertIn("共渲染", out, f"应有汇总行；stdout={out}")
        # 根板 + 项目板都在 fish-home
        self.assertTrue(list((self.home / "00-首页").glob("看板-*.md")),
                        "根板没落到 00-首页")
        self.assertTrue(list((self.home / "10-项目库" / "P1").glob("看板-*.md")),
                        "项目板没落到 10-项目库/P1")
        # data_root 下不再新增看板文件；快照也应零变更
        stale = list(self.root.rglob("看板-*.md"))
        self.assertEqual(stale, [], f"data_root 下不该再有看板文件：{stale}")
        self.assertEqual(before, after, "board render 不该改动 data_root")

    def test_legacy_renders_are_not_written_back(self):
        """迁移后即使 `boards.path` 已是新口径，旧位置也不会再被写出来。"""
        fish.board_ensure(self.conn, "project", "P1")
        self.conn.execute("UPDATE boards SET path=? WHERE scope='project'",
                          ("projects/P1/看板-261026.md",))
        self.conn.commit()
        # 先让 migrate 把 path 归一，再渲染
        fish.migrate(self.conn)
        board = dict(zip(
            ("id", "scope", "scope_key", "period_key", "path", "status",
             "created_at", "closed_at"),
            self.conn.execute(
                "SELECT id, scope, scope_key, period_key, path, status, created_at, "
                "closed_at FROM boards WHERE scope='project' LIMIT 1").fetchone()))
        fish.board_render(self.conn, self.root, board)
        self.assertFalse(
            (self.root / "projects" / "P1" / f"看板-{board['period_key']}.md").exists(),
            "旧落点不该被重新写出来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
