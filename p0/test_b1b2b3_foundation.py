#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1/B2/B3 · T01 基础设施与共用底座 · 判据（2026-10-26）。

测的是"三块共用的底座能不能被反复踩"：
  F1  知识库两表 + 索引 + `kb_path_stats` 视图（新库路径 & 旧库升级路径各一条）
  F2  `migrate()` 幂等：第二次跑零结构变更；正确视图不被无条件 DROP/重建
  F3  `resolve_home_root` 四档解析 + 三条断言（不进 data_root / 不进 vault / 必 resolve）
  F4  `iso_local` / `KBError` / 错误码词表 / `KB_UNATTRIBUTED`
  F5  `kb_sha256_*` / `kb_detect_eol` / `kb_atomic_write`（含 inode 判定）
  F6  `_ensure_boards_home_paths` 幂等（重算比对；第二次零写入）
  F7  CLI 三族 parser 已注册；T02/T03/T04 未实装前占位命令**明确 exit 2**

## 变异自证（X-1：每条判据都必须能变红；本文件承担 T01 的三条）

变异 A —— 对应 `test_legacy_db_gets_kb_tables`：
  把 `migrate()` 链尾的 `_ensure_kb_tables(conn)` 注释掉 →
  已经 DROP 掉 kb 表的旧库，升级后 `kb_vault` / `kb_access` 仍然不存在 → 本测试红。

变异 B —— 对应 `test_home_root_standard_layout`：
  把 `resolve_home_root` 的规则②改成 `data_root.parent / "fish-home"` →
  期望 `<项目根>/fish-home`，实际 `p0/fish-home` → 本测试红。

变异 C —— 对应 `test_atomic_write_replaces_inode`：
  把 `kb_atomic_write` 里的 `os.replace(tmp, path)` 换成 `path.write_bytes(data)` →
  "文件 inode 必须变"的断言红（证明它不是原地覆盖，而是原子替换）。

纪律（K12）：本文件只使用 `tempfile.mkdtemp()` 隔离，**不指向 `p0/data` 或真实 vault**。
"""
import contextlib
import datetime
import hashlib
import io
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


class FoundationTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-foundation-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ================================================================ F1
    def test_kb_tables_indexes_and_view(self):
        """两表 + 5 条索引（含默认库部分唯一索引）+ 聚合视图，列契约逐列对得上。"""
        for table in ("kb_vault", "kb_access"):
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]
            self.assertTrue(cols, f"表 {table} 没建出来")
        vault_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(kb_vault)")]
        for col in ("vault_id", "name", "path", "path_key", "mode", "ignore_globs",
                    "max_open_bytes", "is_default", "last_scan_at", "created_at",
                    "updated_at"):
            self.assertIn(col, vault_cols, f"kb_vault 缺列 {col}")

        index_names = {r[1] for r in self.conn.execute("PRAGMA index_list(kb_vault)")}
        self.assertIn("ux_kb_vault_path", index_names, "path_key 去重索引没建")
        self.assertIn("ux_kb_vault_default", index_names, "默认库唯一索引没建")
        access_indexes = {r[1] for r in self.conn.execute("PRAGMA index_list(kb_access)")}
        for name in ("ix_kb_access_path", "ix_kb_access_citizen", "ix_kb_access_action"):
            self.assertIn(name, access_indexes, f"kb_access 缺索引 {name}")

        view = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='kb_path_stats'"
        ).fetchone()
        self.assertIsNotNone(view, "kb_path_stats 视图没建")
        self.assertEqual(
            tuple(r[1] for r in self.conn.execute("PRAGMA table_info(kb_path_stats)")),
            fish._KB_PATH_STATS_COLUMNS,
            "视图列与 _KB_PATH_STATS_COLUMNS 不一致")

    def test_new_db_schema_contains_kb(self):
        """`SCHEMA` 已拼进 `_KB_SCHEMA`：全新库只需要 executescript(SCHEMA)。"""
        fresh_dir = Path(tempfile.mkdtemp(prefix="fish-fresh-"))
        try:
            conn = fish.connect(fresh_dir)
            try:
                conn.executescript(fish.SCHEMA)
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("kb_vault", names)
                self.assertIn("kb_access", names)
            finally:
                conn.close()
        finally:
            shutil.rmtree(fresh_dir, ignore_errors=True)

    def test_legacy_db_gets_kb_tables(self):
        """**变异 A**：旧库（kb 对象全缺）跑一次 migrate 必须补齐。

        这是"迁移只加在链尾、不改前人顺序"那条纪律的判据面：
        真实旧库路径 = `executescript(SCHEMA)`（对已存在的表是空操作）→ `migrate()`。
        """
        self.conn.execute("DROP VIEW IF EXISTS kb_path_stats")
        self.conn.execute("DROP TABLE kb_access")
        self.conn.execute("DROP TABLE kb_vault")
        self.conn.commit()
        missing = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'kb_%'")}
        self.assertEqual(missing, set(), "前置条件：kb 对象应已全部不存在")

        fish.migrate(self.conn)          # 变异 A：删掉这行，下面两条断言必红

        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'kb_%'")}
        self.assertIn("kb_vault", names, "旧库升级没补出 kb_vault（变异 A 的证明点）")
        self.assertIn("kb_access", names, "旧库升级没补出 kb_access（变异 A 的证明点）")
        self.assertIn("kb_path_stats", names, "旧库升级没补出 kb_path_stats 视图")

    # ================================================================ F2
    def test_migrate_is_idempotent(self):
        """第二次 migrate 不动任何结构（sqlite_master 逐行相等）。"""
        def snapshot():
            return sorted(
                (r[0], r[1], r[2] or "")
                for r in self.conn.execute(
                    "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))

        before = snapshot()
        fish.migrate(self.conn)
        fish.migrate(self.conn)
        self.assertEqual(before, snapshot(), "重复 migrate 不应改动任何对象定义")

    def test_kb_view_not_recreated_when_columns_match(self):
        """列契约已对齐时**不重建**视图（重建会 DROP，拿写锁打断并发查询）。

        判据形态：sqlite_master 的 rootpage 不变。真被 DROP+CREATE 过，
        视图会拿到新 rootpage（若恰好复用同一页，这条会假绿——但不会假红）。
        """
        def rootpage():
            return self.conn.execute(
                "SELECT rootpage FROM sqlite_master WHERE type='view' "
                "AND name='kb_path_stats'").fetchone()[0]

        before = rootpage()
        fish.migrate(self.conn)
        self.assertEqual(before, rootpage(), "列已对时不该重建 kb_path_stats")

    def test_kb_view_self_heals_when_columns_wrong(self):
        """视图定义漂了（列不符）→ 必须重建回 `_KB_PATH_STATS_COLUMNS`。"""
        self.conn.execute("DROP VIEW kb_path_stats")
        self.conn.execute("CREATE VIEW kb_path_stats AS SELECT 1 AS x")
        self.conn.commit()
        self.assertNotEqual(
            tuple(r[1] for r in self.conn.execute("PRAGMA table_info(kb_path_stats)")),
            fish._KB_PATH_STATS_COLUMNS)
        fish.migrate(self.conn)
        self.assertEqual(
            tuple(r[1] for r in self.conn.execute("PRAGMA table_info(kb_path_stats)")),
            fish._KB_PATH_STATS_COLUMNS,
            "坏视图没有被自愈")
        self.conn.execute("SELECT * FROM kb_path_stats LIMIT 0")   # 能真查

    # ================================================================ F3
    def test_home_root_standard_layout(self):
        """**变异 B**：`<项目根>/p0/data` → 门面在 `<项目根>/fish-home`（与 p0 同级）。"""
        base = Path(tempfile.mkdtemp(prefix="fish-layout-")) / "p0" / "data"
        base.mkdir(parents=True)
        try:
            home = fish.resolve_home_root(base)
            expected = (base.parent.parent / "fish-home").resolve()
            self.assertEqual(home, expected,
                             "标准布局下门面必须与 p0/ 同级（裁决 Q3）")
            self.assertNotEqual(home, (base.parent / "fish-home").resolve(),
                                "变异 B 的证明点：门面不能落在 p0/fish-home")
        finally:
            shutil.rmtree(base.parent.parent, ignore_errors=True)

    def test_home_root_fallback_is_sibling_of_data(self):
        """临时根 `<tmp>/data` → `<tmp>/fish-home`（落在会被 teardown 清理的那一层）。"""
        home = fish.resolve_home_root(self.root)
        self.assertEqual(home, (self.tmp / "fish-home").resolve())
        self.assertEqual(home.parent, self.tmp.resolve(),
                         "兜底档必须是 data_root 的兄弟目录，不能是上上级")

    def test_home_root_override_absolute_and_relative(self):
        target = self.tmp / "custom-home"
        self.assertEqual(fish.resolve_home_root(self.root, override=str(target)),
                         target.resolve())
        # 相对 --home 按 data_root.parent 解析（见 resolve_home_root 里的口径说明：
        # "按 data_root 解析"与"home 不得位于 data_root 内"自相矛盾，以断言为准）
        self.assertEqual(fish.resolve_home_root(self.root, override="nested-home"),
                         (self.root.parent / "nested-home").resolve())

    def test_home_root_config_tier(self):
        """`config layout.home_root` 是第 ① 档（低于 `--home`、高于规则②/③）。"""
        cfg_home = self.tmp / "cfg-home"
        fish.set_config(self.conn, "layout.home_root", str(cfg_home))
        self.assertEqual(fish.resolve_home_root(self.root, conn=self.conn),
                         cfg_home.resolve())
        override = self.tmp / "cli-home"
        self.assertEqual(
            fish.resolve_home_root(self.root, override=str(override), conn=self.conn),
            override.resolve(), "`--home` 必须压过 config")

    def test_home_root_refuses_inside_data(self):
        """home 等于/位于 data_root 内 → 直接 raise（HOME-6 的第一道闸）。"""
        with self.assertRaises(RuntimeError):
            fish.resolve_home_root(self.root, override=str(self.root / "home"))
        with self.assertRaises(RuntimeError):
            fish.resolve_home_root(self.root, override=str(self.root))
        # 变异入口：把配置指进 data_root，必须同样炸（不是只有 --home 才验）
        fish.set_config(self.conn, "layout.home_root", str(self.root / "bad"))
        with self.assertRaises(RuntimeError):
            fish.resolve_home_root(self.root, conn=self.conn)

    def test_home_root_refuses_inside_vault(self):
        """门面不得位于已登记知识库内（否则门面会污染知识库）。"""
        vault = self.tmp / "vault"
        vault.mkdir()
        self.conn.execute(
            "INSERT INTO kb_vault (vault_id,name,path,path_key,mode,ignore_globs,"
            "max_open_bytes,is_default,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("01VAULT000000000000000001", "测试库", str(vault),
             os.path.normcase(str(vault)), "ro", "[]", 2097152, 0, "active",
             fish.now_utc(), fish.now_utc()))
        self.conn.commit()
        with self.assertRaises(RuntimeError):
            fish.resolve_home_root(self.root, override=str(vault / "fish-home"),
                                   conn=self.conn)

    # ================================================================ F4
    def test_iso_local_roundtrip(self):
        tz = datetime.timezone(datetime.timedelta(hours=8))
        dt = datetime.datetime(2026, 10, 26, 2, 5, 11, tzinfo=tz)
        self.assertEqual(fish.iso_local(dt), "2026-10-26T02:05:11+08:00")
        self.assertEqual(datetime.datetime.fromisoformat(fish.iso_local(dt)), dt)
        naive = datetime.datetime(2026, 10, 26, 2, 5, 11)
        text = fish.iso_local(naive)
        self.assertIsNotNone(datetime.datetime.fromisoformat(text).tzinfo,
                             "无时区入参必须补成本地时区，不能输出裸时间")

    def test_kberror_carries_code_and_next_step(self):
        err = fish.KBError("bad_usage", "参数不对", hint="看 --help")
        self.assertEqual(err.code, "bad_usage")
        self.assertEqual(err.message, "参数不对")
        self.assertIn("下一步：看 --help", str(err))
        self.assertEqual(str(fish.KBError("offline", "离线")), "离线")

    def test_error_code_vocabulary_is_complete(self):
        """错误码词表是契约：少一个，CLI/MCP 的判据就会退化成认中文文案。

        `sensitive` 是 2026-10-26 第二轮（R15 · 安全 F4）新增的码：敏感文件默认拒读，
        要正文必须 `--allow-sensitive` / `allow_sensitive=true`。词表仍**严格相等**
        （不是"至少包含"），新增码必须同提交改这里——这是有意的摩擦。
        """
        expected = {
            "bad_usage", "path_invalid", "outside_vault", "reparse_point", "ignored",
            "not_found", "offline", "too_large", "media", "bad_encoding",
            "not_writable", "exists", "stale_base", "backup_missing",
            "content_too_large", "vault_missing", "mode_ro", "bad_action",
            "bad_frontmatter", "sensitive",
        }
        self.assertEqual(set(fish.KB_ERROR_CODES), expected)
        self.assertEqual(len(fish.KB_ERROR_CODES), len(expected), "词表不该有重复")
        self.assertEqual(fish.KB_UNATTRIBUTED, "unattributed")
        self.assertIn(fish.KB_UNATTRIBUTED, ("unattributed",))

    def test_kb_log_vocabularies(self):
        """action / result / via 三张词表是 `kb_access` 的唯一来源（库层不设 CHECK）。"""
        self.assertIn("search_hit", fish.KB_ACTIONS)
        self.assertIn("stale_base", fish.KB_RESULTS)
        self.assertEqual(set(fish.KB_VIAS), {"cli:kb", "mcp:kb", "mcp:kb_write"})
        self.assertIn("query_hash", fish.KB_META_KEYS)
        self.assertNotIn("query", fish.KB_META_KEYS, "meta 白名单不得允许 query 原文")

    # ================================================================ F5
    def test_sha256_helpers(self):
        self.assertEqual(fish.kb_sha256_bytes(b""),
                         hashlib.sha256(b"").hexdigest())
        p = self.tmp / "blob.bin"
        payload = "记忆库\n".encode("utf-8") * 3000
        p.write_bytes(payload)
        self.assertEqual(fish.kb_sha256_file(p), hashlib.sha256(payload).hexdigest())

    def test_detect_eol(self):
        self.assertEqual(fish.kb_detect_eol(b"a\r\nb\r\nc\r\n"), "crlf")
        self.assertEqual(fish.kb_detect_eol(b"a\nb\nc\n"), "lf")
        self.assertEqual(fish.kb_detect_eol(b""), "lf")
        self.assertEqual(fish.kb_detect_eol(b"no newline"), "lf")

    def test_atomic_write_roundtrip_and_no_tmp(self):
        target = self.tmp / "note.md"
        res = fish.kb_atomic_write(target, "正文\n".encode("utf-8"), eol="lf")
        self.assertEqual(target.read_bytes(), "正文\n".encode("utf-8"))
        self.assertEqual(res["bytes"], target.stat().st_size)
        self.assertEqual(res["sha256"], fish.kb_sha256_file(target))
        self.assertEqual(res["eol"], "lf")
        residues = [p.name for p in self.tmp.iterdir() if ".tmp-" in p.name]
        self.assertEqual(residues, [], "原子写成功后不该留下临时文件")

    def test_atomic_write_replaces_inode(self):
        """**变异 C**：写必须走 `os.replace`（换 inode），不是原地覆盖。

        为什么这条重要：`path.write_bytes` 也能写出正确内容，"内容对"这条判据
        抓不到"写到一半崩溃留下半篇"的失败模式。inode 变了才证明是"新文件原子顶上"。
        """
        target = self.tmp / "note.md"
        target.write_bytes(b"old")
        before = os.stat(target).st_ino
        fish.kb_atomic_write(target, b"new")
        after = os.stat(target).st_ino
        self.assertEqual(target.read_bytes(), b"new")
        self.assertNotEqual(before, after, "变异 C 的证明点：必须换 inode（原子替换）")

    # ================================================================ F6
    def test_boards_home_paths_idempotent(self):
        """`_ensure_boards_home_paths` 用 `board_relpath` 重算；第二次零写入。"""
        pk = "261026"
        want = fish.board_relpath("project", "P1", pk)
        self.conn.execute(
            "INSERT INTO boards (scope,scope_key,period_key,path,status,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("project", "P1", pk, want, "open", fish.now_utc()))
        self.conn.commit()
        self.assertEqual(fish._ensure_boards_home_paths(self.conn), [],
                         "已经符合口径的行不该被改写")

        self.conn.execute("UPDATE boards SET path=? WHERE scope_key='P1'",
                          ("boards/branch/P1/看板-261026.md",))
        self.conn.commit()
        changed = fish._ensure_boards_home_paths(self.conn)
        self.assertEqual(len(changed), 1, "旧口径行应被重算")
        row = self.conn.execute(
            "SELECT path FROM boards WHERE scope_key='P1'").fetchone()[0]
        self.assertEqual(row, want)
        self.assertEqual(fish._ensure_boards_home_paths(self.conn), [],
                         "第二次必须是空操作（幂等）")

    # ================================================================ F7
    def _run_main(self, argv):
        """跑一次 `fish.main()`，返回 `(exit_code, stdout, stderr)`；exit_code=None=正常返回。"""
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

    def test_placeholder_commands_exit_2(self):
        """三族 parser 已注册；`layout render` 缺 `--all` 仍**明确 exit 2**。

        ⚠️ 2026-10-26 修订（T03/T04 实装后）：本测试原先断言 `project status` 与
        `kb vault list` 这两个**占位**实现 exit 2。T03/T04 把两者都实装了，那条断言
        自然失效——继续留着它等于要求"实现了也要假装没实现"。改为：
          ① 三族 parser 仍注册（argparse 不报 invalid choice）；
          ② 真·用法错误（`layout render` 缺 `--all`）仍 exit 2，且错误信息指向参数；
          ③ 另外两条命令被**真的分派**到各自的 cmd_* 函数（用替身函数捕获 args）。
        判据强度没有降低：parser 被误删/改名仍会红（invalid choice / 未分派）。
        """
        code, _out, err = self._run_main(["--root", str(self.root), "layout", "render"])
        self.assertEqual(code, 2, "`layout render` 缺 `--all` 必须是 exit 2")
        self.assertIn("--all", err)
        self.assertNotIn("invalid choice", err, "layout 的 parser 没注册")

        for label, func_name, argv in (
                ("project status", "cmd_project_status",
                 ["--root", str(self.root), "project", "status", "--json"]),
                ("kb vault list", "cmd_kb",
                 ["--root", str(self.root), "kb", "vault", "list"])):
            with self.subTest(command=label):
                code, err, captured = self._capture(func_name, argv)
                self.assertEqual(code, 0, f"{label} 没有被分派到 {func_name}")
                self.assertIn("args", captured, f"{label} 的分派参数没传进来")
                self.assertNotIn("invalid choice", err,
                                 f"{label} 的 parser 没注册（argparse 报 invalid choice）")

    def _capture(self, func_name, argv):
        """把 fish.<func_name> 换成替身，跑一次 main()，返回 (code, stderr, 捕获的 args)。"""
        captured = {}

        def fake(args):
            captured["args"] = args
            raise SystemExit(0)

        old = getattr(fish, func_name)
        setattr(fish, func_name, fake)
        try:
            code, _out, err = self._run_main(argv)
        finally:
            setattr(fish, func_name, old)
        return code, err, captured

    def test_kb_cli_parses_documented_arguments(self):
        """CLI 契约先登记：`kb tree --depth/--vault` 这类参数现在就能被解析。"""
        captured = {}

        def fake_cmd_kb(args):
            captured["args"] = args
            raise SystemExit(0)

        old = fish.cmd_kb
        fish.cmd_kb = fake_cmd_kb
        try:
            code, _out, _err = self._run_main(
                ["--root", str(self.root), "kb", "tree",
                 "20-PPT", "--depth", "3", "--vault", "安逸知识库"])
        finally:
            fish.cmd_kb = old
        self.assertEqual(code, 0, "kb tree 的参数解析失败（契约破了）")
        self.assertEqual(captured["args"].path, "20-PPT")
        self.assertEqual(captured["args"].depth, 3)
        self.assertEqual(captured["args"].vault, "安逸知识库")


if __name__ == "__main__":
    unittest.main(verbosity=2)
