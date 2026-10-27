#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1 门面 · `fish layout render --all` / `fish layout verify` 判据（2026-10-26）。

被测定义（HOME-1~9）：
  门面 = `fish-home/`（与 `p0/` 同级），五个一级目录、任何文件深度 ≤3，
  每个一级目录有 README 四要素，`20-记忆库` 只有索引没有正文，
  `render` 只写 `fish-home/**`（对 `data_root` 零写入），`verify` 有问题 exit≠0。

## 变异自证（X-1：每条判据都必须能变红；本文件承担 B1 的两条）

变异 A —— 对应 `test_five_top_dirs_are_exact` + `test_verify_extra_top_dir`：
  把 `LAYOUT_TOP` 里的 `"20-记忆库"` 删掉 → 五目录断言红，`verify` 报 `top_dir_missing`。

变异 B —— 对应 `test_data_root_untouched`：
  在 `render_layout` 里顺手 `(data_root / "_reports").mkdir()` 并写一个 json →
  快照 diff 非空 → 本测试红（HOME-6）。

变异 C —— 对应 `test_asof_freshness_and_idempotency`（两条必须同时存在）：
  把 as-of 写死成 `2026-01-01` → 新鲜度判据红（与 now 差 >60 s），而幂等判据仍绿；
  反过来忽略 `--as-of`（每次用 now）→ 幂等判据红。这正是 PRD §4.1 说的
  "为了幂等而造假"与"为了真实而放弃幂等"两个方向都要能抓。

纪律（K12）：所有测试用 `tempfile.mkdtemp()` 隔离，**不指向 `p0/data` 或真实 vault**。
"""
import contextlib
import datetime
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

TZ8 = datetime.timezone(datetime.timedelta(hours=8))
FIXED_AS_OF = datetime.datetime(2026, 10, 26, 0, 0, 0, tzinfo=TZ8)


class LayoutTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-layout-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('P1','项目一',NULL,0,'active')")
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,status,"
            "file_path,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            ("P1-261026-01", "门面判据的记忆", "P1", "01OWNER00000000000000001",
             "fact", "active", "projects/P1/P1-261026-01__门面.md",
             "2026-10-26T01:00:00Z"))
        self.conn.commit()
        self.home = fish.resolve_home_root(self.root, conn=self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- 工具
    def _render(self, as_of=FIXED_AS_OF, **kw):
        return fish.render_layout(self.conn, self.root, as_of=as_of, **kw)

    def _snapshot(self, base):
        """`(相对路径, 字节, mtime_ns)` 快照：用于"零写入"判据。"""
        out = {}
        base = Path(base)
        for p in sorted(base.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[p.relative_to(base).as_posix()] = (st.st_size, st.st_mtime_ns)
        return out

    def _file_hashes(self):
        out = {}
        for p in sorted(self.home.rglob("*")):
            if p.is_file():
                out[p.relative_to(self.home).as_posix()] = fish.kb_sha256_file(p)
        return out

    def _codes(self, res):
        return {it["code"] for it in res["items"]}

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

    # ------------------------------------------------------------- render
    def test_render_produces_all_product_categories(self):
        """五目录 + 五个 README + 索引/最近 + vaults + 系统三页 + 状态表。"""
        res = self._render()
        written = {it["path"] for it in res["written"]}
        for name in fish.LAYOUT_TOP:
            self.assertTrue((self.home / name).is_dir(), f"缺一级目录 {name}")
            self.assertIn(f"{name}/README.md", written, f"缺 {name}/README.md")
        self.assertIn("20-记忆库/索引-2026-10.md", written, "缺当月索引")
        self.assertIn("20-记忆库/最近.md", written, "缺最近列表")
        self.assertIn("30-知识库/vaults.md", written, "缺知识库登记投影")
        self.assertIn("10-项目库/项目状态表.md", written, "缺项目状态表（HOME-7 的链接目标）")
        for rel in ("90-系统/IDENTITY.md", "90-系统/CAPABILITIES.md",
                    "90-系统/渲染清单.md"):
            self.assertIn(rel, written, f"缺系统页 {rel}")
        # 全部产物都在 fish-home 内，data_root 下没有任何镜像
        for rel in written:
            self.assertTrue((self.home / rel).exists(), f"产物没落盘：{rel}")
            self.assertFalse((self.root / rel).exists(), f"产物不该写进 data_root：{rel}")
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["degraded"], [])

    def test_five_top_dirs_are_exact(self):
        """**变异 A**：一级目录集合严格等于五元组（多余/缺失都不允许）。"""
        self._render()
        tops = sorted(p.name for p in self.home.iterdir())
        self.assertEqual(tops, sorted(fish.LAYOUT_TOP),
                         "门面一级目录集合必须严格等于 LAYOUT_TOP")
        self.assertEqual(len(fish.LAYOUT_TOP), 5)

    def test_readme_has_four_elements(self):
        """HOME-2：README 四要素各命中（正则 4 条全中，不是只看"文件存在"）。"""
        self._render()
        for name in fish.LAYOUT_TOP:
            text = (self.home / name / "README.md").read_text(encoding="utf-8")
            self.assertEqual(fish._readme_missing_elements(text), [],
                             f"{name}/README.md 四要素不全")
            self.assertIn(fish.README_NEGATIVE_WORD, text)

    def test_readme_not_overwritten_and_force_readme(self):
        """裁决 C5：已存在不覆盖；`--force-readme` 才覆盖。"""
        self._render()
        readme = self.home / "00-首页" / "README.md"
        readme.write_text("HAND-EDITED\n", encoding="utf-8")
        res = self._render()
        self.assertEqual(readme.read_text(encoding="utf-8"), "HAND-EDITED\n",
                         "已存在的 README 不该被覆盖")
        skipped = {it["path"] for it in res["skipped"]}
        self.assertIn("00-首页/README.md", skipped)
        self._render(force_readme=True)
        text = readme.read_text(encoding="utf-8")
        self.assertNotIn("HAND-EDITED", text, "--force-readme 应覆盖")
        self.assertEqual(fish._readme_missing_elements(text), [])

    def test_home_page_links_status_table(self):
        """HOME-7：首页必须有指向 `10-项目库/项目状态表.md` 的相对链接，且目标存在。"""
        self._render()
        text = (self.home / "00-首页" / "README.md").read_text(encoding="utf-8")
        links = fish._markdown_links(text)
        target = [x for x in links if x.endswith("10-项目库/项目状态表.md")]
        self.assertTrue(target, f"首页缺状态表相对链接，实际链接={links}")
        ok, why = fish._link_target_ok(self.home / "00-首页" / "README.md", target[0])
        self.assertTrue(ok, f"首页链接目标不存在：{why}")

    def test_render_idempotent_with_fixed_as_of(self):
        """HOME-4：同一 as-of 连跑两次，逐文件 sha256 全等（diff=0）。"""
        self._render()
        first = self._file_hashes()
        self._render()
        second = self._file_hashes()
        self.assertEqual(sorted(first), sorted(second), "两次渲染的文件清单不同")
        diff = [k for k in first if first[k] != second.get(k)]
        self.assertEqual(diff, [], f"同一 as-of 两次渲染有 {len(diff)} 个文件不同")

    def test_data_root_untouched(self):
        """**变异 B（HOME-6）**：渲染前后对 `data_root` 做快照，diff 必须为空。"""
        before = self._snapshot(self.root)
        self._render()
        after = self._snapshot(self.root)
        self.assertEqual(before, after,
                         "layout render 写了 data_root（门面必须是只读投影）")

    def test_asof_freshness_and_idempotency(self):
        """**变异 C**：默认 as-of 必须"新鲜"（与 now 差 <60 s），而固定 as-of 必须幂等。"""
        started = datetime.datetime.now(datetime.timezone.utc)
        self._render(as_of=datetime.datetime.now().astimezone())
        cap = (self.home / "90-系统" / "CAPABILITIES.md").read_text(encoding="utf-8")
        m = re.search(r"as-of\s+([0-9T:\-+\.Z]+)", cap)
        self.assertIsNotNone(m, "系统页必须带 as-of")
        stamped = datetime.datetime.fromisoformat(m.group(1))
        gap = abs((stamped - started).total_seconds())
        self.assertLess(gap, 60, f"默认 as-of 与 now 差 {gap:.0f} s（>60 s 说明写死了）")
        # 固定 as-of：幂等（另一条判据，不能因为上面那条而放弃）
        self._render(as_of=FIXED_AS_OF)
        first = self._file_hashes()
        self._render(as_of=FIXED_AS_OF)
        self.assertEqual(first, self._file_hashes())

    def test_render_respects_as_of_month(self):
        """索引按 as-of 的月份命名（不是按 now 的月份）。"""
        res = self._render(as_of=datetime.datetime(2026, 9, 30, 23, 0, 0, tzinfo=TZ8))
        paths = {it["path"] for it in res["written"]}
        self.assertIn("20-记忆库/索引-2026-09.md", paths)
        self.assertNotIn("20-记忆库/索引-2026-10.md", paths)

    def test_degraded_source_is_explicit(self):
        """HOME-9：数据源读不到 → 页里出现"数据源不可用：…"，**不生成空文件/静默 0**。"""
        self.conn.execute("DROP TABLE code_index")
        self.conn.commit()
        res = self._render()
        self.assertFalse(res["failed"], "数据源故障不该表现为写盘失败")
        text = (self.home / "20-记忆库" / "索引-2026-10.md").read_text(encoding="utf-8")
        self.assertIn("数据源不可用：", text, "降级必须明说")
        ver = fish.verify_layout(self.conn, self.root)
        self.assertEqual(ver["errors"], 0, "数据源降级是 warning，不是 error")
        self.assertIn("degraded_source", self._codes(ver))

    # ------------------------------------------------------------- R1（索引文件）
    def test_memory_index_r1_readable_and_budgeted(self):
        """R1（主理人裁决）：索引的归属列可读、标题截断、路径列保留、128 KB 硬上限。

        背景：实测生产索引 65 KB / 246 行，其中"归属"是 26 字符的 citizen ULID、
        标题是全量、每行还带全路径——人读时第一列就看不见"这是谁"。
        四条判据各钉一处（任何一条单独退回去都会被抓住）：
          ① 归属列 = `display_name`（不是 ULID）；
          ② 标题列 ≤ 48 字，超出以 `…` 结尾；
          ③ 「正文相对路径」列仍在（HOME-3 要求，不许因为瘦身删掉）；
          ④ 文件 ≤ 128 KB；超限时按行截断 + 页内脚注明示（不静默丢行）。
        """
        cid = "01R1OWNER0000000000000001"
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (cid, "小可读", "resident", "member", None, "active", fish.now_utc()))
        long_title = "很长的标题" * 30
        # 600 行：远超过 128 KB（每行约 250+ 字节 → 150 KB 级），用来触发截断路径
        for i in range(600):
            self.conn.execute(
                "INSERT INTO code_index (code,title,project,owner,mem_type,status,"
                "file_path,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"P1-261026-{i:03d}", long_title, "P1", cid, "fact", "active",
                 f"projects/P1/P1-261026-{i:03d}__{'x' * 40}.md",
                 "2026-10-20T01:00:00Z"))
        self.conn.commit()

        self._render()
        target = self.home / "20-记忆库" / "索引-2026-10.md"
        raw = target.read_bytes()
        text = raw.decode("utf-8")

        self.assertLessEqual(len(raw), fish.LAYOUT_INDEX_MAX_BYTES,
                             f"索引 {len(raw)} B 超过 128 KB 硬上限")
        self.assertTrue("上限 128 KB" in text, "页内必须写明 128 KB 上限（R1 裁决）")
        self.assertTrue(cid not in text, "归属列仍是 ULID——display_name 没生效")
        self.assertTrue("小可读" in text, "归属列应显示 display_name")
        self.assertTrue("| 编码 | 标题 | 归属 | 状态 | 正文相对路径 |" in text,
                        "「正文相对路径」列不许删（HOME-3）")
        self.assertTrue(f"projects/P1/P1-261026-000__{'x' * 40}.md" in text,
                        "正文相对路径列的内容不见了")
        # 标题被截到 48 字（含省略号）
        cells = [ln.split("|")[2].strip() for ln in text.splitlines()
                 if ln.startswith("| `P1-261026-")]
        self.assertTrue(cells, "没找到数据行")
        self.assertTrue(all(len(c) <= fish.LAYOUT_INDEX_TITLE_MAX_CHARS
                            for c in cells), "标题列超长（应截到 48 字）")
        self.assertTrue(any(c.endswith("…") for c in cells), "超长标题应以 … 结尾")
        # 超限时：合计行说明「显示 M 条」，且脚注给出省略条数
        self.assertTrue("本页显示" in text, "截断必须明示（不许静默丢行）")
        self.assertTrue("截断说明" in text)
        self.assertTrue("合计：600 条" in text, "合计必须是**真实总数**（不是显示条数）")

    # ------------------------------------------------------------- verify
    def test_verify_clean_layout_is_green(self):
        """全绿：0 error / 0 warning，exit 0（CLI 侧）。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        ver = fish.verify_layout(self.conn, self.root)
        self.assertEqual(ver["errors"], 0, f"items={ver['items']}")
        self.assertEqual(ver["warnings"], 0, f"items={ver['items']}")
        code, _out, _err = self._run_main(
            ["--root", str(self.root), "layout", "verify"])
        self.assertIsNone(code, "全绿时 CLI 应以 exit 0 结束")

    def test_verify_missing_home(self):
        """门面根不存在 → 五个 top_dir_missing，exit 1（不自动建目录）。"""
        ver = fish.verify_layout(self.conn, self.root)
        self.assertEqual(ver["errors"], 5)
        self.assertEqual(self._codes(ver), {"top_dir_missing"})
        self.assertFalse(self.home.exists(), "verify 不得顺手建目录")
        code, out, _err = self._run_main(
            ["--root", str(self.root), "layout", "verify"])
        self.assertEqual(code, 1)
        self.assertIn("top_dir_missing", out)

    def test_verify_extra_top_dir(self):
        """HOME-1 变异：多一个 `99-垃圾` → `top_dir_extra` + exit 1。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        (self.home / "99-垃圾").mkdir()
        ver = fish.verify_layout(self.conn, self.root)
        self.assertGreaterEqual(ver["errors"], 1)
        self.assertIn("top_dir_extra", self._codes(ver))
        code, out, _err = self._run_main(
            ["--root", str(self.root), "layout", "verify"])
        self.assertEqual(code, 1)
        self.assertIn("top_dir_extra", out)

    def test_verify_missing_readme(self):
        """HOME-2：删 `20-记忆库/README.md` → `readme_missing` + exit 1。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        os.remove(self.home / "20-记忆库" / "README.md")
        ver = fish.verify_layout(self.conn, self.root)
        self.assertIn("readme_missing", self._codes(ver))
        paths = {it["path"] for it in ver["items"] if it["code"] == "readme_missing"}
        self.assertIn("20-记忆库/README.md", paths)
        code, out, _err = self._run_main(
            ["--root", str(self.root), "layout", "verify"])
        self.assertEqual(code, 1)
        self.assertIn("readme_missing", out)

    def test_verify_readme_element_missing(self):
        """HOME-2：删掉"谁在写"那一节 → 该目录判红（不是整体 exit 0）。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        p = self.home / "10-项目库" / "README.md"
        text = p.read_text(encoding="utf-8").replace("## 谁在写", "## 谁在写（被删）")
        p.write_text(text, encoding="utf-8")
        ver = fish.verify_layout(self.conn, self.root)
        self.assertIn("readme_element_missing", self._codes(ver))
        paths = {it["path"] for it in ver["items"]
                 if it["code"] == "readme_element_missing"}
        self.assertIn("10-项目库/README.md", paths)

    def test_verify_depth_exceeded(self):
        """HOME-1：`10-项目库/P1/sub/note.md` 是 4 层 → `depth_exceeded`。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        deep = self.home / "10-项目库" / "P1" / "sub"
        deep.mkdir(parents=True, exist_ok=True)
        (deep / "note.md").write_text("x\n", encoding="utf-8")
        ver = fish.verify_layout(self.conn, self.root)
        codes = {it["code"] for it in ver["items"]}
        self.assertIn("depth_exceeded", codes)
        self.assertEqual(len([it for it in ver["items"]
                              if it["code"] == "depth_exceeded"]), 1)

    def test_verify_orphan_file_at_top(self):
        """一级目录下直接放文件 → `orphan_file`。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        (self.home / "散落.md").write_text("x\n", encoding="utf-8")
        ver = fish.verify_layout(self.conn, self.root)
        self.assertIn("orphan_file", self._codes(ver))

    def test_verify_memory_body_copy_by_name(self):
        """HOME-3 第一重：正文形态文件名出现在 `20-记忆库` → 红。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        (self.home / "20-记忆库" / "ANYI-261026-01__正文.md").write_text(
            "正文\n", encoding="utf-8")
        ver = fish.verify_layout(self.conn, self.root)
        self.assertIn("memory_body_copy", self._codes(ver))

    def test_verify_memory_body_copy_by_hash(self):
        """HOME-3 第二重：改名复制也抓得到（sha256 与 `data_root/projects/**` 相交）。"""
        body = self.root / "projects" / "P1" / "P1-261026-09__原文.md"
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_text("# 原文\n\n这是正文。\n", encoding="utf-8")
        self._render(as_of=datetime.datetime.now().astimezone())
        (self.home / "20-记忆库" / "看起来像索引.md").write_text(
            "# 原文\n\n这是正文。\n", encoding="utf-8")
        ver = fish.verify_layout(self.conn, self.root)
        hits = [it for it in ver["items"] if it["code"] == "memory_body_copy"]
        self.assertTrue(hits, "改名复制必须靠 sha256 交集抓到")
        self.assertIn("sha256", hits[0]["message"])

    def test_verify_credential_like(self):
        """HOME-8：`90-系统/**` 出现凭证形态 → 红。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        (self.home / "90-系统" / "evil.md").write_text(
            "凭证：" + "fsh_" + "A" * 24 + "\n", encoding="utf-8")
        ver = fish.verify_layout(self.conn, self.root)
        self.assertIn("credential_like", self._codes(ver))

    def test_verify_link_broken(self):
        """HOME-5：首页多一个指向不存在目标的相对链接 → `link_broken`。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        readme = self.home / "00-首页" / "README.md"
        with readme.open("a", encoding="utf-8") as f:
            f.write("\n[坏链接](../99-没有这个页.md)\n")
        ver = fish.verify_layout(self.conn, self.root)
        codes = {it["code"] for it in ver["items"]}
        self.assertIn("link_broken", codes)
        self.assertGreaterEqual(ver["errors"], 1)

    def test_verify_board_entry_missing_is_warning(self):
        """状态表里的看板入口目标不存在 → 只记 warning（今天没人开工是合法状态）。"""
        self._render(as_of=datetime.datetime.now().astimezone())
        status = self.home / "10-项目库" / "项目状态表.md"
        with status.open("a", encoding="utf-8") as f:
            f.write("\n| P1 | 进行中 | — | [看板-261026.md](看板-261026.md) | — | — |\n")
        ver = fish.verify_layout(self.conn, self.root)
        self.assertIn("board_entry_missing", self._codes(ver))
        self.assertEqual(ver["errors"], 0, "看板入口缺失不能升级成 error")

    def test_verify_stale_render_is_warning(self):
        """门面 as-of 早于最后一件事 → `stale_render`（warning，不改退出码）。"""
        self._render(as_of=datetime.datetime.now().astimezone()
                     - datetime.timedelta(hours=2))
        fish.append_event(self.conn, "layout.test",
                          {"citizen_id": "system"},
                          {"kind": "test", "id": "layout-stale"},
                          {"k": "v"})
        ver = fish.verify_layout(self.conn, self.root)
        self.assertIn("stale_render", self._codes(ver))
        self.assertEqual(ver["errors"], 0)

    # ------------------------------------------------------------- CLI
    def test_cli_render_requires_all(self):
        """`layout render` 缺 `--all` → exit 2（用法错误，不是静默成功）。"""
        code, _out, err = self._run_main(
            ["--root", str(self.root), "layout", "render"])
        self.assertEqual(code, 2)
        self.assertIn("--all", err)

    def test_cli_render_writes_manifest_and_reports(self):
        """`layout render --all --as-of` 输出清单三列 + 汇总行，且 exit 0。"""
        code, out, _err = self._run_main(
            ["--root", str(self.root), "layout", "render", "--all",
             "--as-of", "2026-10-26T00:00:00+08:00"])
        self.assertIsNone(code)
        self.assertIn("00-首页/README.md\t", out)
        self.assertIn("改了 ", out)
        self.assertIn("重建命令", out)

    def test_layout_paths_shape(self):
        """`layout_paths` 的路径键与契约一致（render/verify 共用的唯一拼路径处）。"""
        paths = fish.layout_paths(self.home)
        self.assertEqual(paths["home"], self.home)
        self.assertEqual(paths["status_table"], self.home / fish.LAYOUT_STATUS_TABLE)
        self.assertEqual(paths["recent"], self.home / "20-记忆库" / "最近.md")
        self.assertIn("identity", paths["system"])
        self.assertEqual(sorted(paths["top"]), sorted(fish.LAYOUT_TOP))

    def test_relative_data_root_still_finds_project_level_home(self):
        """回归：`--root data`（相对路径）也必须解析到 `<项目根>/fish-home`。

        踩过的坑：`Path("data").parent.parent` 仍然是 `"."`，于是规则②算出
        `p0/fish-home`——门面跑到 p0 里面去了（与裁决 Q3 不符）。
        规则必须在**绝对路径**上套；这条判据用 chdir 复现当时的调用形态。
        """
        base = self.tmp / "proj" / "p0"
        (base / "data").mkdir(parents=True)
        old_cwd = os.getcwd()
        try:
            os.chdir(base)
            home = fish.resolve_home_root(Path("data"))
        finally:
            os.chdir(old_cwd)
        self.assertEqual(home, (self.tmp / "proj" / "fish-home").resolve(),
                         "相对 data_root 也必须落在与 p0 同级的位置")


if __name__ == "__main__":
    unittest.main(verbosity=2)
