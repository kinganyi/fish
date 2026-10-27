#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3 · 知识库读路径（tree / open / search / stats / doctor）判据（T04）。

被测定义：
  KB-2  `tree` **只统计不 open**（monkeypatch `builtins.open` 计数必须为 0）；
  KB-3  双预算（`chars ≤ max_chars` **且** `tokens ≤ max_tokens`），截断要明示原因；
  KB-7  `open` 必须给显式相对路径；`> max_open_bytes` 拒绝并给字节数；
  KB-8  `open` **不写 events**（知识库的读不产生记忆事件）；
  KB-9  `search` 的头部三行固定（来源/未建索引/扫描读数），每条命中给全路径+行号；
  KB-10 `search` **不建索引**：独特词只被 kb search 命中，`recall` 0 命中；
  KB-11 只扫已登记 vault 的 `.md`（.txt / 忽略目录不扫）；
  KB-14 `doctor` 探 Obsidian CLI：app 未运行 → `unavailable`，读写照常。

本轮新增（R4 · 评审 🟠-2 的收口）：
  R4-a `tree --depth` 的**唯一语义**：depth = 显示的最大目录层级（起点 = 第 0 层）；
       depth=1 只有起点 + 一级子目录 + 它们各自的直接 .md；更深的一律不出现；
  R4-b **每个被显示的目录都列出它的直接 .md 文件**（相对库根的完整路径）；
  R4-c 文本随 depth 增大**单调不减**（预算内不截断）；
  R4-d 命中预算时文本末尾必须有"已截断：用 `path=<目录>` 深入"那一句；
  R4-e **入口归一化**：`tree/open/search` 收到原始库行（`sqlite3.Row` / `ignore_globs`
       还是 JSON 字符串）与收到 `kb_vault_get` 归一后的 dict，回执逐字节相同；
       非法 `ignore_globs` 响亮失败（`bad_usage`），绝不当字符串逐字符当 glob。

## 变异自证（X-1）

变异 —— 对应 `test_tree_never_opens_files`：
  把 `_kb_walk` 里的 `entry.stat()` 换成 `open(entry.path,'rb').read(1)`（"顺手读一下"）→
  open 计数 > 0 → 断言红。这条守的是"tree 是廉价操作"这个**承诺**，不是实现细节。

变异 —— 对应 R4 这一组（真跑读数见 `docs/变异自证-B1B2B3-v0.1.md` §6 M-R4）：
  把 `_kb_tree_dir` 的深度裁剪（`if level >= max_depth:`）与 `_kb_tree_lines.emit` 的
  `if level >= max_level: return` 一起注释掉（= depth 判断被摘掉）→ depth=1 会渲染出
  二/三级目录与它们的文件 → `test_depth_1_...` / `test_depth_2_...` 必红。

纪律（K12）：vault 全部在 `tempfile.mkdtemp()` 里造，**绝不指向真实知识库**。
"""
import builtins
import contextlib
import importlib.util
import io
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

# 拆包（2026-10-27 · kb 拆包 R1）：kb 的实现整段搬进了 `fish_kb`。
# `mock.patch.object(fish, "<kb 内部函数>")` 从此打不到调用点（函数体在 `fish_kb` 的
# globals 里解析名字），所以这一处 patch 目标改指 `fish_kb`；断言一个都没动。
import fish_kb  # noqa: E402 —— 与 fish.py 同目录，测试用 cwd 导入


class KBReadTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-kbr-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault_dir = self.tmp / "vault"
        (self.vault_dir / "00-首页").mkdir(parents=True)
        (self.vault_dir / "10-项目" / "a").mkdir(parents=True)
        (self.vault_dir / "附件").mkdir()
        (self.vault_dir / "00-首页" / "欢迎.md").write_text(
            "# 欢迎\n\n这是第一篇。\n", encoding="utf-8")
        (self.vault_dir / "00-首页" / "说明.md").write_text(
            "# 说明\n\n独特词甲出现在这一行。\n", encoding="utf-8")
        (self.vault_dir / "10-项目" / "a" / "a1.md").write_text(
            "# a1\n\n普通内容。\n", encoding="utf-8")
        (self.vault_dir / "10-项目" / "b.md").write_text(
            "# b\n\n普通内容。\n", encoding="utf-8")
        (self.vault_dir / "附件" / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        (self.vault_dir / ".obsidian").mkdir()
        (self.vault_dir / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        self.vault = fish.kb_vault_add(self.conn, name="读测试库",
                                       path=str(self.vault_dir), mode="rw")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- 工具
    def _rel(self, name):
        return name

    def _write(self, rel, text, *, newline="\n"):
        p = self.vault_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline=newline) as f:
            f.write(text)
        return p

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

    # ============================================================ tree
    def test_tree_counts_without_reading(self):
        """KB-2：tree 只统计（笔记/附件/字节），且**一个文件都不 open**。"""
        fish.count_tokens("预热")          # 先让 tiktoken 的 import 发生在计数之外
        opened = []
        real_open = builtins.open

        def counting_open(*args, **kwargs):
            opened.append(args[0] if args else kwargs.get("file"))
            return real_open(*args, **kwargs)

        with mock.patch("builtins.open", counting_open):
            res = fish.kb_tree(self.conn, self.vault, rel="", depth=3)
        self.assertEqual(opened, [], f"tree 期间 open 了文件：{opened[:3]}")
        self.assertEqual(res["scan"]["files"], 5, "5 个文件（.obsidian 被忽略）")
        self.assertEqual(res["scan"]["stale"], False)
        # 00-首页(2 md) + 10-项目(a1.md,b.md) + 附件(1 png) = 5 文件 / 4 笔记 / 1 附件
        self.assertEqual(res["summary"]["notes"], 4)
        self.assertEqual(res["summary"]["attachments"], 1)
        self.assertIn("生效忽略规则", res["text"])
        self.assertIn(".obsidian/**", res["text"])
        self.assertIn("source=知识库（未建索引）", res["text"])

    def test_tree_budget_both_limits(self):
        """KB-3：字符与 token 两个上限**同时**成立，并说明截断原因。"""
        res = fish.kb_tree(self.conn, self.vault, rel="", depth=3,
                           max_chars=120, max_tokens=30)
        self.assertLessEqual(res["chars"], 120)
        self.assertLessEqual(res["tokens"], 30)
        self.assertTrue(res["truncated"])
        self.assertTrue(res["truncated_reasons"])
        tokens_only = fish.kb_tree(self.conn, self.vault, rel="", depth=3,
                                   max_chars=100000, max_tokens=12)
        self.assertLessEqual(tokens_only["tokens"], 12)
        self.assertIn("tokens", tokens_only["truncated_reasons"])

    def test_tree_dir_items_capped_at_20(self):
        """每目录最多列 20 项，余下记 `more=N`（预算可控 + 确定性）。"""
        for i in range(25):
            self._write(f"99-多/{i:02d}.md", f"# {i}\n")
        res = fish.kb_tree(self.conn, self.vault, rel="99-多", depth=1,
                           max_chars=100000, max_tokens=100000)
        node = res["summary"]
        self.assertEqual(len(node["children"]), fish.KB_MAX_ITEMS_PER_DIR)
        self.assertEqual(node["more"], 5)
        self.assertIn("more=5", res["text"])

    def test_tree_ignores_dot_dirs(self):
        """忽略目录即使存在也不进统计（`.obsidian/app.json` 不出现）。"""
        res = fish.kb_tree(self.conn, self.vault, rel="", depth=3,
                           max_chars=100000, max_tokens=100000)
        text = res["text"]
        self.assertNotIn("- .obsidian/", text, "忽略目录不该作为节点出现")
        self.assertNotIn("app.json", text, "忽略目录里的文件不该被统计")
        self.assertEqual(res["scan"]["files"], 5)

    def test_tree_offline_falls_back_to_last_scan(self):
        """离线：有旧扫描数 → 用旧数据 + 明说离线（不是空库）。"""
        fish.kb_vault_scan(self.conn, self.vault["vault_id"])
        self.vault = fish.kb_vault_get(self.conn, self.vault["vault_id"])
        shutil.rmtree(self.vault_dir)
        res = fish.kb_tree(self.conn, self.vault, rel="", depth=2)
        self.assertTrue(res["scan"]["stale"])
        self.assertIn("知识库不在线", res["text"])
        self.assertIn("不是空库", res["text"])

    def test_tree_offline_without_last_scan_raises(self):
        shutil.rmtree(self.vault_dir)
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_tree(self.conn, self.vault, rel="")
        self.assertEqual(ctx.exception.code, "offline")

    # ============================================================ open
    def test_open_reads_text_and_three_statements(self):
        """KB-7：给定显式相对路径读到正文；三句固定声明；sha256 与文件一致。"""
        res = fish.kb_open(self.conn, self.vault, "00-首页/欢迎.md")
        self.assertIn("这是第一篇", res["text"])
        self.assertEqual(res["sha256"],
                         fish.kb_sha256_file(self.vault_dir / "00-首页" / "欢迎.md"))
        self.assertEqual(res["encoding"], "utf-8")
        self.assertFalse(res["truncated"])
        self.assertEqual(len(res["statements"]), 3)
        self.assertIn("知识库（未建索引）", res["statements"][0])
        self.assertIn("按需扫描", res["statements"][1])
        self.assertIn("未截断", res["statements"][2])

    def test_open_truncates_and_reports_bytes(self):
        self._write("长文.md", "字" * 500)
        res = fish.kb_open(self.conn, self.vault, "长文.md", max_chars=50)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["chars_shown"], 50)
        self.assertEqual(res["chars_total"], 500)
        self.assertLess(res["shown"], res["bytes"])
        self.assertIn("已截断", res["statements"][2])

    def test_open_refuses_too_large_with_byte_count(self):
        """KB-7：> max_open_bytes → `too_large`，并把字节数与上限说出来。"""
        fish.kb_vault_set_mode(self.conn, "读测试库", "rw")
        big = self._write("大文件.md", "x" * 3000)
        fish.kb_vault_add(self.conn, name="读测试库", path=str(self.vault_dir),
                          mode="rw", max_open_bytes=1024)
        self.vault = fish.kb_vault_get(self.conn, "读测试库")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_open(self.conn, self.vault, "大文件.md")
        err = ctx.exception
        self.assertEqual(err.code, "too_large")
        self.assertIn(str(big.stat().st_size), err.message)
        self.assertIn("1024", err.message)

    def test_open_refuses_media_by_extension_and_magic(self):
        """KB-12：扩展名与魔数双判——`.png` 与"改名成 .md 的 png"都拒绝。"""
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_open(self.conn, self.vault, "附件/x.png")
        self.assertEqual(ctx.exception.code, "media")
        (self.vault_dir / "假笔记.md").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_open(self.conn, self.vault, "假笔记.md")
        self.assertEqual(ctx.exception.code, "media")
        self.assertIn("magic", str(ctx.exception.message))

    def test_open_refuses_bad_encoding(self):
        (self.vault_dir / "乱码.md").write_bytes(b"abc\xff\xff\xff")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_open(self.conn, self.vault, "乱码.md")
        self.assertEqual(ctx.exception.code, "bad_encoding")

    def test_open_handles_bom_and_crlf(self):
        p = self.vault_dir / "bom.md"
        p.write_bytes("\ufeff第一行\r\n第二行\r\n".encode("utf-8"))
        res = fish.kb_open(self.conn, self.vault, "bom.md")
        self.assertEqual(res["encoding"], "utf-8-sig")
        self.assertFalse(res["text"].startswith("\ufeff"))
        self.assertEqual(res["eol"], "crlf")

    def test_open_logs_access_but_not_events(self):
        """KB-8：open 写 kb_access，**不写 events**（知识库读不是记忆事件）。"""
        before = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        fish.kb_open(self.conn, self.vault, "00-首页/欢迎.md", citizen_id="CIT1")
        after = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(before, after, "kb open 不该写 events")
        row = self.conn.execute(
            "SELECT path, citizen_id, action, result, bytes, sha256 FROM kb_access "
            "WHERE action='open'").fetchone()
        self.assertEqual(row[0], "00-首页/欢迎.md")
        self.assertEqual(row[1], "CIT1")
        self.assertEqual(row[2], "open")
        self.assertEqual(row[3], "ok")
        self.assertGreater(row[4], 0)
        self.assertEqual(len(row[5]), 64)

    def test_rejections_are_logged(self):
        """全事件流水（含 reject）：被拒绝的读也要有痕迹。"""
        with self.assertRaises(fish.KBError):
            fish.kb_open(self.conn, self.vault, "附件/x.png")
        rows = self.conn.execute(
            "SELECT action, result FROM kb_access WHERE action='reject'").fetchall()
        self.assertTrue(rows, "拒绝没有写 kb_access")
        self.assertEqual(rows[0][1], "media")

    # ============================================================ search
    def test_search_hits_md_with_path_and_line(self):
        """KB-9：命中给全路径 + 行号 + 上下文；头部三行（来源/未建索引/扫描读数）。"""
        res = fish.kb_search(self.conn, self.vault, "独特词甲")
        self.assertEqual(res["total"], 1)
        hit = res["hits"][0]
        self.assertEqual(hit["rel"], "00-首页/说明.md")
        self.assertEqual(hit["lines"][0]["lineno"], 3)
        self.assertIn("独特词甲", hit["lines"][0]["text"])
        self.assertEqual(hit["abs"], str(self.vault_dir / "00-首页" / "说明.md"))
        self.assertIn("source=知识库（未建索引）", res["text"])
        self.assertIn("scan=", res["text"])
        self.assertIn("files /", res["text"])
        self.assertIn("ms", res["text"])
        self.assertIn("00-首页/说明.md", res["text"])

    def test_search_scans_only_registered_md(self):
        """KB-11：只扫 `.md`；`.txt` 与忽略目录都不在扫描面内。"""
        (self.vault_dir / "笔记.txt").write_text("独特词甲也在这里", encoding="utf-8")
        (self.vault_dir / ".obsidian" / "hidden.md").write_text(
            "独特词甲", encoding="utf-8")
        res = fish.kb_search(self.conn, self.vault, "独特词甲")
        rels = [h["rel"] for h in res["hits"]]
        self.assertEqual(rels, ["00-首页/说明.md"])
        self.assertEqual(res["scanned"]["files"], 4, "只数 .md 且跳过 .obsidian")

    def test_search_limit_and_sorting(self):
        for i in range(3):
            self._write(f"命中{i}.md", "独特词乙 独特词乙\n" if i == 0 else "独特词乙\n")
        res = fish.kb_search(self.conn, self.vault, "独特词乙", limit=2)
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["shown"], 2)
        self.assertEqual(res["hits"][0]["rel"], "命中0.md", "命中次数多的排前面")

    def test_search_rejects_empty_query(self):
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_search(self.conn, self.vault, "   ")
        self.assertEqual(ctx.exception.code, "bad_usage")

    def test_search_never_mixes_with_recall(self):
        """KB-10：独特词 `recall` 0 命中、`kb search` 1 命中；不建索引、不动 code_index。"""
        self._write("只属于知识库.md", "独一无二词丙 只存在于知识库。\n")
        fish.ensure_fts(self.conn, self.root)
        recall_hits, _meta = fish.search_memories(self.conn, query="独一无二词丙")
        self.assertEqual(recall_hits, [],
                         "recall 不该命中知识库内容（KB-10 边界被破了）")
        res = fish.kb_search(self.conn, self.vault, "独一无二词丙")
        self.assertEqual(res["total"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM code_index").fetchone()[0], 0,
            "kb search 不许往 code_index 写东西")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory_fts").fetchone()[0], 0,
            "kb search 不许进全文索引")

    def test_search_meta_has_no_plaintext(self):
        """§5.6：meta 只有 query_hash/qlen；**不存 query 原文、不存正文**。"""
        fish.kb_search(self.conn, self.vault, "独特词甲", citizen_id="CIT9")
        rows = self.conn.execute(
            "SELECT path, meta, sha256 FROM kb_access WHERE action='search'").fetchall()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0][1])
        self.assertEqual(len(meta["query_hash"]), 12)
        self.assertEqual(meta["qlen"], 4)
        self.assertNotIn("query", meta)
        blob = json.dumps([list(r) for r in self.conn.execute(
            "SELECT path,meta FROM kb_access").fetchall()], ensure_ascii=False)
        self.assertNotIn("独特词甲", blob, "query 原文漏进了 kb_access")
        self.assertNotIn("独特词甲", rows[0][0])

    def test_search_hit_rows_per_shown_hit(self):
        res = fish.kb_search(self.conn, self.vault, "独特词甲")
        hits = self.conn.execute(
            "SELECT path FROM kb_access WHERE action='search_hit'").fetchall()
        self.assertEqual([r[0] for r in hits],
                         [h["rel"] for h in res["hits"]])
        self.assertEqual(len(hits), res["shown"])

    # ============================================================ stats / doctor
    def test_stats_shape(self):
        fish.kb_open(self.conn, self.vault, "00-首页/欢迎.md")
        res = fish.kb_stats(self.conn, "读测试库")
        self.assertEqual(len(res["vaults"]), 1)
        item = res["vaults"][0]
        self.assertEqual(item["notes"], 4)
        self.assertEqual(item["files"], 5)
        self.assertEqual(item["counts_source"], "live")
        self.assertGreaterEqual(item["opened_paths"], 1)
        self.assertGreaterEqual(res["access_rows"], 1)
        self.assertTrue(res["recent"])
        self.assertIn("# 知识库规模", res["text"])

    def test_doctor_reports_unavailable_obsidian(self):
        """KB-14：app 未运行 → `obsidian_cli=unavailable`，诊断照常完成。"""
        with mock.patch.object(fish_kb, "_kb_obsidian_cli",
                               return_value={"available": False, "path": None,
                                             "version": None,
                                             "reason": "app_not_running"}):
            res = fish.kb_doctor(self.conn)
        self.assertFalse(res["obsidian_cli"]["available"])
        self.assertIn("obsidian_cli=unavailable", res["text"])
        self.assertEqual(len(res["vaults"]), 1)
        self.assertTrue(res["vaults"][0]["exists"])
        self.assertTrue(res["vaults"][0]["writable"])

    def test_doctor_flags_same_name_and_oversize(self):
        self._write("20-甲/同名.md", "A\n")
        self._write("20-乙/同名.md", "B\n")
        fish.kb_vault_add(self.conn, name="读测试库", path=str(self.vault_dir),
                          mode="ro", max_open_bytes=8)
        res = fish.kb_doctor(self.conn)
        item = res["vaults"][0]
        names = [d["name"] for d in item["same_name"]]
        self.assertIn("同名.md", names)
        self.assertTrue(item["oversize"], "超过 8 B 的文件应被列出")
        self.assertIn("同名笔记：", res["text"])

    def test_doctor_offline_vault_does_not_raise(self):
        shutil.rmtree(self.vault_dir)
        res = fish.kb_doctor(self.conn)
        self.assertFalse(res["vaults"][0]["exists"])
        self.assertEqual(res["vaults"][0]["counts_source"], "last_scan")

    # ============================================================ CLI
    def test_cli_tree_json_budget(self):
        """验收 ③：`kb tree --json` 可 json.loads，且 chars/tokens 都在预算内。"""
        code, out, err = self._run_main(
            ["--root", str(self.root), "kb", "tree", "--vault", "读测试库", "--json"])
        self.assertIsNone(code, f"tree --json 应 exit 0；stderr={err}")
        data = json.loads(out)
        self.assertLessEqual(data["chars"], 8000)
        self.assertLessEqual(data["tokens"], 1200)
        self.assertGreater(data["scan"]["files"], 0)

    def test_cli_open_and_search(self):
        code, out, err = self._run_main(
            ["--root", str(self.root), "kb", "open", "00-首页/欢迎.md",
             "--vault", "读测试库"])
        self.assertIsNone(code, f"open 应 exit 0；stderr={err}")
        self.assertIn("知识库（未建索引）", out)
        self.assertIn("这是第一篇", out)
        code, out, err = self._run_main(
            ["--root", str(self.root), "kb", "search", "独特词甲",
             "--vault", "读测试库"])
        self.assertIsNone(code, f"search 应 exit 0；stderr={err}")
        self.assertIn("00-首页/说明.md", out)
        self.assertIn("未建索引", out)

    def test_cli_open_bad_path_exits_1(self):
        code, _out, err = self._run_main(
            ["--root", str(self.root), "kb", "open", "../../x.md",
             "--vault", "读测试库"])
        self.assertEqual(code, 1, f"坏路径应 exit 1；stderr={err}")
        self.assertIn("path_invalid", err)


class KBDepthSemanticsTest(unittest.TestCase):
    """`kb tree --depth` 的唯一语义 + 入口归一化（R4 · 评审 🟠-2 的收口）。

    判据定义与 `fish._kb_tree_lines` 的 docstring **同一句**：
      depth = **显示的最大目录层级**，起点（这里是库根）= 第 0 层；
      depth=1 → 根 + 一级子目录 + **它们各自的直接 .md 文件**；二级及更深一律不出现；
      每个被显示的目录都要列出它的直接 .md 文件（相对库根的完整路径）；
      文本随 depth 增大**单调不减**（预算内不截断）。

    夹具是评审给的复现形态（A/a.md、A/B/b.md、A/B/C/c.md），另加：
      · 库根一个 `根笔记.md`——"根下的文件"必须出现在 depth=1；
      · `.obsidian/配置笔记.md`——默认忽略规则必须仍然生效（归一化防御的对照物，
        同一个检索词故意也写进它，用来证明 search 没被逐字符误判带偏）。
    """

    TERM = "独特词甲"
    TS_RE = re.compile(r"as_of \S+")
    MS_RE = re.compile(r"\d+ ms")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-kbdepth-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault_dir = self.tmp / "vault"
        (self.vault_dir / "A" / "B" / "C").mkdir(parents=True)
        (self.vault_dir / ".obsidian").mkdir()
        (self.vault_dir / "根笔记.md").write_text("# 根\n", encoding="utf-8")
        (self.vault_dir / "A" / "a.md").write_text(
            f"# a\n\n{self.TERM} 在 a 里。\n", encoding="utf-8")
        (self.vault_dir / "A" / "B" / "b.md").write_text("# b\n", encoding="utf-8")
        (self.vault_dir / "A" / "B" / "C" / "c.md").write_text("# c\n", encoding="utf-8")
        (self.vault_dir / ".obsidian" / "配置笔记.md").write_text(
            f"{self.TERM} 不该被扫到。\n", encoding="utf-8")
        self.vault = fish.kb_vault_add(self.conn, name="深度库",
                                       path=str(self.vault_dir), mode="ro")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- 工具
    def _tree(self, depth, row=None):
        res = fish.kb_tree(self.conn, row or self.vault, rel="", depth=depth,
                           max_chars=100000, max_tokens=100000)
        self.assertFalse(res["truncated"], "预算给足了还被截断，读数不可信")
        return res

    def _stable_tree(self, res):
        """抹掉时间戳与耗时后用于"逐字节相同"比较（其余字段一个都不许差）。"""
        scan = dict(res["scan"])
        scan.pop("ms", None)
        return {"text": self.TS_RE.sub("as_of <T>", res["text"]),
                "chars": res["chars"], "tokens": res["tokens"],
                "truncated": res["truncated"],
                "truncated_reasons": res["truncated_reasons"],
                "root": res["root"], "depth": res["depth"], "dirs": res["dirs"],
                "summary": res["summary"], "scan": scan,
                "ignore_globs": res["ignore_globs"], "vault": res["vault"]}

    def _stable_search(self, res):
        scanned = dict(res["scanned"])
        scanned.pop("ms", None)
        text = self.MS_RE.sub("<N> ms", self.TS_RE.sub("as_of <T>", res["text"]))
        return {"text": text, "hits": res["hits"], "total": res["total"],
                "shown": res["shown"], "query_hash": res["query_hash"],
                "qlen": res["qlen"], "scanned": scanned}

    # ============================================================ R4-a/b/c
    def test_depth_1_keeps_everything_up_to_first_level(self):
        """R4-a/b：depth=1 = 根 + 一级子目录 + 它们各自的直接 .md；二级及更深不出现。"""
        res = self._tree(1)
        text = res["text"]
        self.assertIn("- A/", text, "一级子目录必须出现")
        self.assertIn("根笔记.md", text, "根下的直接文件必须出现")
        self.assertIn("A/a.md", text, "一级目录的直接文件必须出现（旧版只给 basename）")
        self.assertNotIn("A/B/", text, "depth=1 不该显示二级目录")
        self.assertNotIn("A/B/b.md", text, "depth=1 不该显示二级目录下的文件")
        self.assertNotIn("A/B/C/", text)
        self.assertNotIn("A/B/C/c.md", text)

    def test_depth_2_adds_second_level_and_its_files(self):
        """R4-a/b：depth=2 加上二级子目录**及其直接文件**（`A/B/` 显示了 b.md 就必须在）。"""
        res = self._tree(2)
        text = res["text"]
        self.assertIn("A/B/", text)
        self.assertIn("A/B/b.md", text, "已显示的 A/B/ 下文件必须出现（旧版永远不出现）")
        self.assertIn("A/a.md", text)
        self.assertNotIn("A/B/C/", text, "depth=2 不该显示三级目录")
        self.assertNotIn("A/B/C/c.md", text)

    def test_depth_3_adds_third_level_and_its_files(self):
        """R4-a/b：depth=3 加上三级子目录及其直接文件，且浅层内容一个都不少。"""
        res = self._tree(3)
        text = res["text"]
        self.assertIn("A/B/C/", text)
        self.assertIn("A/B/C/c.md", text, "已显示的 A/B/C/ 下文件必须出现")
        self.assertIn("A/B/b.md", text)
        self.assertIn("A/a.md", text)
        self.assertIn("根笔记.md", text)

    def test_chars_do_not_shrink_as_depth_grows(self):
        """R4-c：chars 随 depth 单调不减（上一版 depth 2→3 反而短了 91 字）。

        上一版的成因是"深层未展开"提示随 depth 消失；现在说明行成本恒定，
        所以单调性只取决于内容——内容只增不减。
        """
        samples = [(d, self._tree(d)) for d in (1, 2, 3, 4)]
        readings = [(d, res["chars"]) for d, res in samples]
        for (d1, c1), (d2, c2) in zip(readings, readings[1:]):
            self.assertLessEqual(
                c1, c2, f"depth {d1}→{d2} 的 chars 变短了（{c1} → {c2}）：单调性破了")
        # 显示集合是前缀闭包：更深时浅层的目录行/文件行必须还在
        by_depth = {d: res["text"] for d, res in samples}
        for depth in (2, 3, 4):
            for needle in ("- A/", "A/a.md"):
                self.assertIn(needle, by_depth[depth],
                              f"depth={depth} 丢了浅层内容 {needle}")

    def test_truncated_tree_ends_with_how_to_go_deeper(self):
        """R4-d：命中预算时末尾必须是"已截断：用 `path=<目录>` 深入"，且不超预算。"""
        res = fish.kb_tree(self.conn, self.vault, rel="", depth=3,
                           max_chars=260, max_tokens=100000)
        self.assertTrue(res["truncated"], "260 字预算应截断")
        self.assertLessEqual(res["chars"], 260, "截断后仍超字符预算")
        last = [ln for ln in res["text"].splitlines() if ln.strip()][-1]
        self.assertTrue(last.startswith("> 已截断：用 `path="),
                        f"末句不是'怎么深入'：{last!r}")
        self.assertIn("深入", last)

    # ============================================================ R4-e
    def test_raw_vault_row_matches_normalized_row(self):
        """R4-e：原始库行（JSON 字符串 globs）与归一化 dict 的回执逐字节相同。

        这是"入口先归一"的判据：若把 `ignore_globs` 的 JSON 字符串当可迭代对象**逐字符**
        匹配，`.obsidian/配置笔记.md` 会漏进 tree 与 search——**不报错、只泄漏**。
        四种入参（conn 默认的 tuple 行 / `sqlite3.Row` / 原始 dict / 混合 dict）都必须
        与 `kb_vault_get` 归一后的回执一致。
        """
        globs_at = fish._KB_VAULT_COLS.index("ignore_globs")
        raw_tuple = self._raw_row(None)      # conn 默认工厂：行就是 tuple
        self.assertIsInstance(raw_tuple, tuple)
        self.assertIsInstance(raw_tuple[globs_at], str, "库里存的应是 JSON 文本")
        raw_row = self._raw_row(sqlite3.Row)
        self.assertIsInstance(raw_row["ignore_globs"], str, "库里存的应是 JSON 文本")
        raw_dict = dict(zip(fish._KB_VAULT_COLS, raw_tuple))
        mixed = dict(raw_dict)
        mixed["ignore_globs"] = json.loads(raw_tuple[globs_at])
        norm = fish.kb_vault_get(self.conn, self.vault["vault_id"])
        self.assertIsInstance(norm["ignore_globs"], list)

        base_tree = self._stable_tree(self._tree(3, row=norm))
        base_open = fish.kb_open(self.conn, norm, "A/B/b.md")
        base_search = self._stable_search(fish.kb_search(self.conn, norm, self.TERM))

        for label, row in (("tuple 行", raw_tuple), ("sqlite3.Row", raw_row),
                           ("原始 dict", raw_dict),
                           ("globs 已是 list 的 dict", mixed)):
            with self.subTest(row=label):
                res = self._tree(3, row=row)
                self.assertEqual(self._stable_tree(res), base_tree,
                                 "原始行的 tree 回执与归一化后不一致")
                self.assertNotIn("- .obsidian/", res["text"],
                                 "忽略规则失效了（JSON 字符串被逐字符当 glob）")
                self.assertNotIn("配置笔记.md", res["text"],
                                 "忽略目录里的文件被列出来了")
                self.assertEqual(res["scan"]["files"], 4,
                                 ".obsidian 里的文件被算进扫描面了")
                self.assertEqual(fish.kb_open(self.conn, row, "A/B/b.md"), base_open)
                self.assertEqual(
                    self._stable_search(fish.kb_search(self.conn, row, self.TERM)),
                    base_search, "原始行的 search 回执与归一化后不一致")
        self.assertEqual(base_search["total"], 1, ".obsidian 里的命中漏进来了")

    def _raw_row(self, factory):
        """按指定 `row_factory` 取一行原始库行（默认 conn 返回 tuple，不是 Row）。"""
        old = self.conn.row_factory
        self.conn.row_factory = factory
        try:
            return self.conn.execute(fish._KB_VAULT_SELECT + " WHERE vault_id=?",
                                     (self.vault["vault_id"],)).fetchone()
        finally:
            self.conn.row_factory = old

    def test_illegal_vault_row_fails_loudly(self):
        """R4-e：非法 `ignore_globs` / 不认识的行走 `bad_usage`，绝不静默当空表。"""
        norm = fish.kb_vault_get(self.conn, self.vault["vault_id"])
        for bad in ("not-json", ".obsidian/**", 5, {"a": 1}, ["ok", ""]):
            with self.subTest(ignore_globs=bad):
                row = dict(norm)
                row["ignore_globs"] = bad
                with self.assertRaises(fish.KBError) as ctx:
                    fish.kb_tree(self.conn, row, rel="", depth=1)
                self.assertEqual(ctx.exception.code, "bad_usage")
        for label, row in (("整行是字符串", "不是库行"), ("整行是数字", 42),
                           ("缺 path", {"vault_id": "X", "name": "X"})):
            with self.subTest(row=label):
                with self.assertRaises(fish.KBError) as ctx:
                    fish.kb_search(self.conn, row, self.TERM)
                self.assertEqual(ctx.exception.code, "bad_usage")


if __name__ == "__main__":
    unittest.main(verbosity=2)
