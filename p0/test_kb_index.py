# -*- coding: utf-8 -*-
"""`fish_kb_index` 的判据（K1–K6 + 安全 + KB-10 不混装 + 新鲜度）。

## 这份文件在守什么（34 号 §1.4 / 55 号 §2.3）

  · **K1** 全量建索引：300 篇 < 60s；**K1b** 真实 vault（`E:\\Obsidian\\安逸知识库`）
    在临时库上跑一遍，记录耗时/条数/库体积（只读，绝不写真实 vault）。
  · **K2** 无变化增量 < 2s 且全是 `unchanged`；**K2b** 改了内容要能看见
    （否则"增量"就是"不更新"）。
  · **K3** 100 次 `search_kb` 的 P95 < 100ms；**K3b** 检索**零文件系统访问**
    （用计数器数 `_kb_walk` 与 `Path.read_text` 的调用次数，不看耗时——
    耗时判据在慢机器上会抖，结构判据不会）。
  · **K4** 真实中文词能命中，且 `matched_field` 的优先级（title > headings >
    summary > body_excerpt）逐条对上。
  · **K5** 删文件 → `status='deleted'`、**行还在**、FTS 不再命中。
  · **K6** 非 UTF-8 文件 → `status='error'`、原因写明、**其余照常 active**。
  · 附加：敏感文件（`凭证.md`）永不入库且计数；`memory_fts` 行数不变（KB-10）；
    `kb_index_fresh` 刚建完 True / 改老 False；vault 根消失 → `offline` 而不是全删。

## 环境纪律

一律 tempfile 根 + 临时主库（`fish.connect` 的真实 schema + `fish.migrate`），
**不碰生产树**、不写真实 vault。真实 vault 那条只做 `read_text`。
"""
import datetime
import json
import math
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish                      # noqa: E402 —— 用真实 schema 建临时主库
import fish_common               # noqa: E402
import fish_kb                   # noqa: E402
import fish_kb_index             # noqa: E402

REAL_VAULT = Path("E:/Obsidian/安逸知识库")

#: 大 vault 的编号笔记篇数（K1 的规模）。
BIG_NOTE_COUNT = 300


def _big_vault_files() -> dict:
    """大 vault 的内容（**期望值写死在测试里**，不让被测实现自证）。

    设计要点：
      · `漫剧方法论.md`：探针词只在 H1 与正文里 → `matched_field='title'`；
      · `流程记录.md`：探针词**只在第二段**（summary 不含）→ `'body_excerpt'`；
      · `会议纪要.md`：探针词只在 H2 → 优先级应报 `'headings'`（不是 body_excerpt，
        因为 excerpt 也含标题行——这正是不按优先级判就会错的地方）；
      · `凭证.md`：敏感名单命中 → 必须**不入库**；
      · `.obsidian/ignored.md`：被默认忽略规则挡住 → 连遍历都不该看到。
    """
    files = {}
    for i in range(BIG_NOTE_COUNT):
        files[f"notes/n{i:03d}.md"] = (
            f"# 笔记 {i:03d}\n\n这是第 {i:03d} 篇笔记的正文，"
            f"用来把索引撑到真实规模。\n\n"
            f"第二段重复一次编号 {i:03d}，并放一些通用词：流程、记录、复盘。\n")
    files["漫剧方法论.md"] = (
        "# 漫剧方法论\n\n漫剧是把小说变成画面的工序。\n\n第二段补充说明。\n")
    files["流程记录.md"] = (
        "# 工作流程\n\n这里是第一段，讲流程的起因与结论，不含探针词。\n\n"
        "第二段才提到看板这个词，用来验证 matched_field 落到 body_excerpt。\n")
    files["会议纪要.md"] = (
        "# 会议纪要\n\n第一段只说会议的时间与地点。\n\n"
        "## 看板评审\n\n这一节的正文不重复那个词。\n")
    files["凭证.md"] = "# 凭证清单\n\n这里不该被索引：账号与令牌都在这一行。\n"
    files["skip.txt"] = "不是 md，不该进索引\n"
    files[".obsidian/ignored.md"] = "# 配置目录里的笔记\n\n被默认忽略规则挡住。\n"
    return files


def _write_files(root: Path, files: dict) -> Path:
    """按 `{相对路径: 文本}` 落盘（自动建父目录）。"""
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _make_db(tmp: Path) -> sqlite3.Connection:
    """临时主库（真实 schema）：`fish.connect` + `fish.SCHEMA` + `fish.migrate`。"""
    root = tmp / "data"
    root.mkdir(parents=True, exist_ok=True)
    conn = fish.connect(root)
    conn.executescript(fish.SCHEMA)
    fish.migrate(conn)
    return conn


def _p95(values) -> float:
    """P95（nearest-rank）：`ceil(0.95 * n)` 那一项（100 个样本 = 第 95 个）。"""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        raise ValueError("P95 需要至少一个样本")
    idx = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[idx]


class _Base(unittest.TestCase):
    """每个测试一个 tempfile 根 + 临时主库；tearDown 全清。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-kbidx-"))
        self.conn = _make_db(self.tmp)
        fish_kb_index.ensure_kb_index(self.conn)
        self._vault_seq = 0

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:                       # noqa: BLE001 —— 清理不该掩盖断言
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_vault(self, files, *, name=None, max_open_bytes=None):
        """造一个临时 vault 并**走真实 API 登记**（不手写 SQL），返回 `(vault, dir)`。"""
        self._vault_seq += 1
        vdir = self.tmp / f"vault{self._vault_seq}"
        _write_files(vdir, files)
        vault = fish_kb.kb_vault_add(
            self.conn, name=name or f"测试库{self._vault_seq}", path=str(vdir),
            mode="ro", max_open_bytes=max_open_bytes)
        return vault, vdir

    def rows(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def rebuild(self, **kwargs):
        return fish_kb_index.rebuild_kb_index(self.conn, None, **kwargs)

    def search(self, **kwargs):
        return fish_kb_index.search_kb(self.conn, **kwargs)


# ------------------------------------------------------------------ 建表


class EnsureIndexTest(unittest.TestCase):
    def test_ensure_creates_tables_and_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        try:
            first = fish_kb_index.ensure_kb_index(conn)
            self.assertTrue(first["created"], "首次调用必须真的建表")
            self.assertEqual(first["tables"], ["kb_fts", "kb_index"])
            second = fish_kb_index.ensure_kb_index(conn)
            self.assertFalse(second["created"], "第二次调用不该再建表（幂等）")
            self.assertEqual(second["tables"], ["kb_fts", "kb_index"])
            # 约束与索引都在（建表语句与 34 号 §1.2.2 一致）
            cols = [r[1] for r in conn.execute("PRAGMA table_info(kb_index)")]
            self.assertEqual(cols, ["vault_id", "rel_path", "mtime", "size",
                                    "content_hash", "title", "headings", "summary",
                                    "excerpt", "word_count", "indexed_at", "status",
                                    "error"])
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("ix_kb_index_mtime", names)
            self.assertIn("ix_kb_index_status", names)
            stats = fish_kb_index.kb_index_stats(conn)
            self.assertEqual(stats["rows"], 0)
            self.assertEqual(stats["active"], 0)
            self.assertGreaterEqual(stats["index_mb"], 0.0)
            fresh = fish_kb_index.kb_index_fresh(conn)
            self.assertFalse(fresh["fresh"], "没有行时不算新鲜")
            self.assertIsNone(fresh["last_indexed_at"])
        finally:
            conn.close()

    def test_read_only_path_survives_missing_tables(self):
        conn = sqlite3.connect(":memory:")
        try:
            res = fish_kb_index.search_kb(conn, query="漫剧")
            self.assertEqual(res["hits"], [])
            self.assertFalse(res["meta"]["indexed"])
            self.assertEqual(res["meta"]["returned"], 0)
            self.assertEqual(fish_kb_index.kb_index_stats(conn)["rows"], 0)
            self.assertFalse(fish_kb_index.kb_index_fresh(conn)["fresh"])
        finally:
            conn.close()


# ------------------------------------------------------------------ 抽取器


class ExtractTest(unittest.TestCase):
    def test_frontmatter_is_not_content(self):
        out = fish_kb_index.extract_kb_note(
            "---\ntitle: 假标题\ntags: [a]\n---\n# 真标题\n\n首段内容。\n")
        self.assertEqual(out["title"], "真标题")
        self.assertEqual(json.loads(out["headings"]), ["真标题"])
        self.assertTrue(out["summary"].startswith("真标题"))
        self.assertIn("首段内容", out["summary"])
        self.assertNotIn("假标题", out["excerpt"])
        self.assertTrue(out["excerpt"].startswith("# 真标题"))

    def test_title_falls_back_to_first_heading(self):
        out = fish_kb_index.extract_kb_note("## 只有二级\n\n正文。\n")
        self.assertEqual(out["title"], "只有二级")
        self.assertEqual(json.loads(out["headings"]), ["只有二级"])

    def test_headings_and_word_count(self):
        text = "# 甲\n\n## 乙\n\n正文 内容。\n"
        out = fish_kb_index.extract_kb_note(text)
        self.assertEqual(json.loads(out["headings"]), ["甲", "乙"])
        body = text
        self.assertEqual(out["word_count"], len("".join(body.split())))
        self.assertIn("正文", out["summary"])

    def test_closing_hashes_are_stripped(self):
        out = fish_kb_index.extract_kb_note("## 标题 ##\n\n正文\n")
        self.assertEqual(json.loads(out["headings"]), ["标题"])

    def test_code_fence_is_not_a_heading(self):
        """围栏里的 `# 注释` 不是标题（否则没有 H1 的笔记会拿注释当标题）。"""
        text = ("```bash\n# 安装依赖\npip install fish\n```\n\n"
                "正文首段在这里。\n\n## 真小标题\n\n后文。\n")
        out = fish_kb_index.extract_kb_note(text)
        self.assertEqual(json.loads(out["headings"]), ["真小标题"])
        self.assertEqual(out["title"], "真小标题")
        self.assertIn("正文首段", out["summary"])
        self.assertNotIn("安装依赖", out["summary"])

    def test_only_a_fence_has_no_title(self):
        out = fish_kb_index.extract_kb_note("```\n# 注释\n```\n")
        self.assertIsNone(out["title"])
        self.assertEqual(json.loads(out["headings"]), [])

    def test_tilde_fence_and_unclosed_fence(self):
        tilde = fish_kb_index.extract_kb_note(
            "# 标题\n\n~~~\n# 不是标题\n~~~\n\n正文。\n")
        self.assertEqual(json.loads(tilde["headings"]), ["标题"])
        unclosed = fish_kb_index.extract_kb_note("# 标题\n\n```\n# 不是标题\n")
        self.assertEqual(json.loads(unclosed["headings"]), ["标题"])

    def test_unclosed_frontmatter_keeps_everything(self):
        text = "---\ntitle: x\n# 标题\n\n正文\n"
        out = fish_kb_index.extract_kb_note(text)
        self.assertIn("# 标题", out["excerpt"])
        self.assertEqual(out["title"], "标题")

    def test_empty_note(self):
        out = fish_kb_index.extract_kb_note("")
        self.assertIsNone(out["title"])
        self.assertEqual(json.loads(out["headings"]), [])
        self.assertEqual(out["summary"], "")
        self.assertEqual(out["excerpt"], "")
        self.assertEqual(out["word_count"], 0)

    def test_excerpt_is_capped(self):
        out = fish_kb_index.extract_kb_note("# 长文\n\n" + "字" * 5000 + "\n")
        self.assertEqual(len(out["excerpt"]), fish_kb_index.KB_INDEX_EXCERPT_CHARS)
        self.assertLessEqual(len(out["summary"]),
                             fish_kb_index.KB_INDEX_SUMMARY_CHARS)


# ------------------------------------------------------------------ K1 / K3 / K4（大 vault）


class BigVaultTest(unittest.TestCase):
    """300 篇的规模判据：建一次，K1/K3/K4 共用（省时间，且测的就是那一份索引）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fish-kbidx-big-"))
        try:
            cls.conn = _make_db(cls.tmp)
            cls.vault_dir = _write_files(cls.tmp / "bigvault", _big_vault_files())
            cls.vault = fish_kb.kb_vault_add(
                cls.conn, name="大库", path=str(cls.vault_dir), mode="ro")
            fish_kb_index.ensure_kb_index(cls.conn)
            t0 = time.perf_counter()
            cls.report = fish_kb_index.rebuild_kb_index(
                cls.conn, None, mode="full")
            cls.build_seconds = time.perf_counter() - t0
            cls.stats = fish_kb_index.kb_index_stats(cls.conn)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            getattr(cls, "conn", None) and cls.conn.close()
        except Exception:                       # noqa: BLE001
            pass
        shutil.rmtree(getattr(cls, "tmp", Path(".")), ignore_errors=True)

    # ── K1
    def test_K1_full_rebuild_under_60s(self):
        report = self.report
        print(f"\n[K1] 全量建索引：{report['notes']} 篇 / {report['seconds']}s"
              f"（墙钟 {self.build_seconds:.2f}s）/ 库 {self.stats['index_mb']} MB")
        # 期望值独立写死：300 编号 + 3 探针 = 303 篇非敏感 md；
        # `凭证.md` 敏感 → 不计；`.obsidian/**` 被忽略 → 遍历都看不到；`skip.txt` 非 md。
        self.assertEqual(report["notes"], BIG_NOTE_COUNT + 3)
        self.assertEqual(report["added"], BIG_NOTE_COUNT + 3)
        self.assertEqual(report["sensitive_skipped"], 1)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["unchanged"], 0)
        self.assertEqual(report["deleted"], 0)
        self.assertEqual(report["offline"], 0)
        self.assertEqual(report["vaults"], 1)
        self.assertEqual(report["mode"], "full")
        # 304 篇 md（303 + 敏感那篇）+ 1 个 txt = 305 个文件条目
        self.assertEqual(report["scanned_files"], BIG_NOTE_COUNT + 5)
        self.assertLess(report["seconds"], 60.0, "K1：300 篇全量必须 < 60s")
        self.assertEqual(self.stats["rows"], BIG_NOTE_COUNT + 3)
        self.assertEqual(self.stats["active"], BIG_NOTE_COUNT + 3)
        self.assertEqual(self.stats["fts_rows"], BIG_NOTE_COUNT + 3)

    # ── K2（在同一份索引上跑第二次增量）
    def test_K2_incremental_no_change_under_2s(self):
        t0 = time.perf_counter()
        report = fish_kb_index.rebuild_kb_index(self.conn, None)
        elapsed = time.perf_counter() - t0
        print(f"\n[K2] 无变化增量：{report['unchanged']} 篇 unchanged / "
              f"{report['seconds']}s（墙钟 {elapsed:.2f}s）")
        self.assertEqual(report["mode"], "incremental")
        self.assertEqual(report["unchanged"], report["notes"])
        self.assertEqual(report["added"], 0)
        self.assertEqual(report["updated"], 0)
        self.assertEqual(report["deleted"], 0)
        self.assertEqual(report["errors"], 0)
        self.assertLess(elapsed, 2.0, "K2：无变化增量必须 < 2s")
        self.assertEqual(fish_kb_index.kb_index_stats(self.conn)["rows"],
                         BIG_NOTE_COUNT + 3)

    def test_K2b_changed_file_is_seen(self):
        """增量必须能**看见变化**（否则"增量"退化成"不更新"）。"""
        target = self.vault_dir / "notes" / "n007.md"
        original = target.read_text(encoding="utf-8")
        before = self.conn.execute(
            "SELECT excerpt FROM kb_index WHERE rel_path='notes/n007.md'").fetchone()[0]
        self.assertIn("复盘", before or "", "改之前那一行确实有旧词（判据才有意义）")
        try:
            target.write_text("# 笔记 007\n\n替换后的正文，新增探针词：蓝色鲸鱼。\n",
                              encoding="utf-8")
            report = fish_kb_index.rebuild_kb_index(self.conn, None)
            self.assertEqual(report["updated"], 1)
            self.assertEqual(report["added"], 0)
            self.assertEqual(report["unchanged"], report["notes"] - 1)
            hits = fish_kb_index.search_kb(self.conn, query="蓝色鲸鱼")["hits"]
            self.assertEqual([h["rel"] for h in hits], ["notes/n007.md"])
            after = self.conn.execute(
                "SELECT excerpt FROM kb_index WHERE rel_path='notes/n007.md'").fetchone()[0]
            self.assertIn("蓝色鲸鱼", after or "")
            self.assertNotIn("复盘", after or "", "旧内容必须被替换掉")
        finally:
            target.write_text(original, encoding="utf-8")
            fish_kb_index.rebuild_kb_index(self.conn, None)

    # ── K3
    def test_K3_search_p95_under_100ms(self):
        queries = ["漫剧", "看板", "流程", "笔记 001", "工作", "复盘", "记录",
                   "笔记 299", "方法论", "会议"]
        samples = []
        for i in range(100):
            query = queries[i % len(queries)]
            t0 = time.perf_counter()
            res = fish_kb_index.search_kb(self.conn, query=query)
            samples.append((time.perf_counter() - t0) * 1000.0)
            self.assertTrue(res["meta"]["indexed"])
        p95 = _p95(samples)
        print(f"\n[K3] 100 次 search_kb：P95={p95:.2f}ms / "
              f"max={max(samples):.2f}ms / avg={sum(samples) / len(samples):.2f}ms")
        self.assertLess(p95, 100.0, "K3：P95 必须 < 100ms")

    def test_K3b_search_never_touches_filesystem(self):
        """结构判据（比耗时硬）：检索路径不许调用 `_kb_walk` / `Path.read_text`。"""
        walked = []

        def _forbidden_walk(*args, **kwargs):
            walked.append(args)
            raise AssertionError("search_kb 不该 walk 文件系统")

        def _forbidden_read(*args, **kwargs):
            walked.append(args)
            raise AssertionError("search_kb 不该读文件正文")

        with mock.patch.object(fish_kb, "_kb_walk", _forbidden_walk), \
                mock.patch.object(Path, "read_text", _forbidden_read), \
                mock.patch.object(Path, "read_bytes", _forbidden_read):
            res = fish_kb_index.search_kb(self.conn, query="漫剧")
        self.assertEqual(walked, [], "K3b：检索走了文件系统（索引没被用上）")
        self.assertTrue(res["hits"], "K3b 的样本必须真有命中，否则判据是空转")

    # ── K4
    def test_K4_chinese_terms_and_matched_field(self):
        hits = fish_kb_index.search_kb(self.conn, query="漫剧", limit=10)["hits"]
        self.assertTrue(hits, "K4：中文词「漫剧」必须能命中")
        top = [h for h in hits if h["rel"] == "漫剧方法论.md"]
        self.assertTrue(top, "「漫剧方法论.md」必须在命中集里")
        print(f"\n[K4] 漫剧 → {[(h['rel'], h['matched_field'], round(h['score'], 3)) for h in hits[:3]]}")
        self.assertEqual(top[0]["matched_field"], "title")
        self.assertEqual(top[0]["score"], 1.0, "最相关的一条归一到 1.0")
        self.assertIn("漫剧", top[0]["snippet"])
        self.assertEqual(top[0]["vault_name"], "大库")
        self.assertGreater(top[0]["size"], 0)

        board = fish_kb_index.search_kb(self.conn, query="看板", limit=10)["hits"]
        by_rel = {h["rel"]: h for h in board}
        self.assertIn("流程记录.md", by_rel)
        self.assertIn("会议纪要.md", by_rel)
        self.assertEqual(by_rel["流程记录.md"]["matched_field"], "body_excerpt")
        self.assertEqual(by_rel["会议纪要.md"]["matched_field"], "headings",
                         "H2 命中必须报 headings（优先级高于 excerpt）")
        self.assertGreaterEqual(by_rel["流程记录.md"]["score"], 0.0)
        self.assertLessEqual(by_rel["流程记录.md"]["score"], 1.0)

        # 敏感文件的正文绝不可检索
        self.assertNotIn("凭证.md", [h["rel"] for h in
                                     fish_kb_index.search_kb(self.conn, query="令牌")["hits"]])
        # 忽略目录里的笔记也不可检索
        self.assertNotIn(".obsidian/ignored.md",
                         [h["rel"] for h in
                          fish_kb_index.search_kb(self.conn, query="配置目录")["hits"]])


# ------------------------------------------------------------------ K1b（真实 vault，只读）


@unittest.skipUnless(REAL_VAULT.is_dir(), f"真实 vault 不在：{REAL_VAULT}")
class RealVaultTest(unittest.TestCase):
    """真实 vault 的性能读数（K1 的第二个样本）。**只读**：不写、不删、不改 mtime。"""

    def test_K1b_real_vault_on_temp_db(self):
        tmp = Path(tempfile.mkdtemp(prefix="fish-kbidx-real-"))
        conn = None
        try:
            conn = _make_db(tmp)
            vault = fish_kb.kb_vault_add(conn, name="安逸知识库",
                                         path=str(REAL_VAULT), mode="ro")
            fish_kb_index.ensure_kb_index(conn)
            t0 = time.perf_counter()
            report = fish_kb_index.rebuild_kb_index(conn, None, mode="full")
            elapsed = time.perf_counter() - t0
            stats = fish_kb_index.kb_index_stats(conn)
            print(f"\n[K1b] 真实 vault「{vault['name']}」：notes={report['notes']} / "
                  f"scanned={report['scanned_files']} / added={report['added']} / "
                  f"errors={report['errors']} / sensitive={report['sensitive_skipped']} / "
                  f"{report['seconds']}s（墙钟 {elapsed:.2f}s）/ 库 {stats['index_mb']} MB")
            t1 = time.perf_counter()
            again = fish_kb_index.rebuild_kb_index(conn, None)
            inc = time.perf_counter() - t1
            print(f"[K1b] 真实 vault 增量：unchanged={again['unchanged']} / "
                  f"{again['seconds']}s（墙钟 {inc:.2f}s）")
            self.assertGreater(report["notes"], 200, "真实库应有 200 篇以上笔记")
            self.assertEqual(report["added"], report["notes"])
            self.assertEqual(again["unchanged"], again["notes"])
            self.assertLess(elapsed, 60.0, "K1b：真实库全量也必须 < 60s")
            self.assertLess(inc, 2.0, "K2：真实库无变化增量 < 2s")
            hit = fish_kb_index.search_kb(conn, query="漫剧", limit=5)
            self.assertTrue(hit["hits"], "真实库里「漫剧」必须有命中")
            fresh = fish_kb_index.kb_index_fresh(conn)
            self.assertTrue(fresh["fresh"])
            self.assertTrue(any(h["matched_field"] in
                                ("title", "headings", "summary", "body_excerpt")
                                for h in hit["hits"]))
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ K5 / K6 / 安全 / 不混装


class LifecycleTest(_Base):
    def test_K5_deleted_file_marked_not_removed(self):
        vault, vdir = self.make_vault({
            "a.md": "# 苹果笔记\n\n唯一标记：苹果香蕉。\n",
            "b.md": "# 乙\n\n内容乙。\n",
        })
        first = self.rebuild()
        self.assertEqual(first["added"], 2)
        (vdir / "a.md").unlink()
        second = self.rebuild()
        print(f"\n[K5] 删文件后增量：{second}")
        self.assertEqual(second["deleted"], 1)
        self.assertEqual(second["errors"], 0)
        row = self.rows("SELECT status, error, excerpt, title FROM kb_index "
                        "WHERE rel_path='a.md'")
        self.assertEqual(len(row), 1, "K5：行必须还在（不物理删）")
        status, error, excerpt, title = row[0]
        self.assertEqual(status, "deleted")
        self.assertIsNone(error)
        self.assertIn("苹果香蕉", excerpt or "", "K5：内容列必须保留（可追溯）")
        self.assertEqual(title, "苹果笔记")
        hits = self.search(query="苹果香蕉")["hits"]
        self.assertEqual([h["rel"] for h in hits], [], "K5：deleted 行不可再命中")
        third = self.rebuild()
        self.assertEqual(third["deleted"], 0, "K5：再次增量不该反复计数")
        self.assertEqual(third["unchanged"], 1)
        self.assertEqual(third["notes"], 1)

    def test_deleted_file_can_come_back(self):
        vault, vdir = self.make_vault({"a.md": "# 甲\n\n复活探针：绿色海豚。\n"})
        self.rebuild()
        text = (vdir / "a.md").read_text(encoding="utf-8")
        (vdir / "a.md").unlink()
        self.assertEqual(self.rebuild()["deleted"], 1)
        (vdir / "a.md").write_text(text, encoding="utf-8")
        back = self.rebuild()
        self.assertEqual(back["updated"], 1, "复活的行算 updated（不是 added）")
        self.assertEqual(back["notes"], 1)
        row = self.rows("SELECT status FROM kb_index WHERE rel_path='a.md'")
        self.assertEqual(row[0][0], "active")
        self.assertEqual([h["rel"] for h in self.search(query="绿色海豚")["hits"]],
                         ["a.md"])

    def test_K6_bad_encoding_error_does_not_interrupt(self):
        vault, vdir = self.make_vault({
            "good.md": "# 好文件\n\n正常内容：红色狐狸。\n",
            "坏编码.md": "# 坏文件\n\n正文含非法字节。\n",
        })
        (vdir / "坏编码.md").write_bytes(b"# bad\n\xff\xfe\x00\x01 not utf-8\n")
        report = self.rebuild()
        print(f"\n[K6] 坏文件：{report}")
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["added"], 1)
        self.assertEqual(report["notes"], 2)
        bad = self.rows("SELECT status, error, excerpt FROM kb_index "
                        "WHERE rel_path='坏编码.md'")[0]
        self.assertEqual(bad[0], "error")
        self.assertEqual(bad[1], "encoding")
        self.assertIsNone(bad[2], "错误行不该保留旧正文（避免搜到过期内容）")
        self.assertEqual(self.rows("SELECT status FROM kb_index "
                                   "WHERE rel_path='good.md'")[0][0], "active")
        self.assertEqual([h["rel"] for h in self.search(query="红色狐狸")["hits"]],
                         ["good.md"])
        # 坏文件不中断：第二次增量把它当 unchanged（mtime/size 没变）
        again = self.rebuild()
        self.assertEqual(again["unchanged"], 2)
        self.assertEqual(again["errors"], 0)
        self.assertEqual(again["added"], 0)

    def test_too_large_marked_error(self):
        vault, vdir = self.make_vault({"big.md": "# 大\n\n" + "字" * 500 + "\n"},
                                      max_open_bytes=64)
        report = self.rebuild()
        self.assertEqual(report["errors"], 1)
        row = self.rows("SELECT status, error FROM kb_index WHERE rel_path='big.md'")[0]
        self.assertEqual(row[0], "error")
        self.assertTrue(row[1].startswith("too_large:"), row[1])
        self.assertEqual(self.search(query="大")["hits"], [])

    def test_sensitive_note_never_indexed(self):
        vault, vdir = self.make_vault({
            "凭证.md": "# 凭证清单\n\n账号与令牌：金色狮子。\n",
            "普通.md": "# 普通笔记\n\n正常内容。\n",
        })
        report = self.rebuild()
        self.assertEqual(report["sensitive_skipped"], 1)
        self.assertEqual(report["notes"], 1)
        rels = [r[0] for r in self.rows("SELECT rel_path FROM kb_index")]
        self.assertEqual(rels, ["普通.md"], "敏感文件绝不可入库")
        self.assertEqual(self.search(query="金色狮子")["hits"], [])

    def test_becoming_sensitive_purges_old_row(self):
        """升级后敏感名单变了/文件被判定敏感 → 旧行必须失效（不能留后门）。"""
        vault, vdir = self.make_vault({"旧笔记.md": "# 旧笔记\n\n后门探针：紫色孔雀。\n"})
        self.rebuild()
        self.assertEqual(len(self.search(query="紫色孔雀")["hits"]), 1)
        real = fish_kb._kb_is_sensitive_name
        with mock.patch.object(fish_kb, "_kb_is_sensitive_name",
                               lambda rel: True if rel == "旧笔记.md" else real(rel)):
            report = self.rebuild()
        self.assertEqual(report["sensitive_skipped"], 1)
        self.assertEqual(report["deleted"], 1)
        status = self.rows("SELECT status FROM kb_index WHERE rel_path='旧笔记.md'")[0][0]
        self.assertEqual(status, "deleted")
        self.assertEqual(self.search(query="紫色孔雀")["hits"], [])

    def test_memory_fts_is_untouched(self):
        """KB-10：知识库索引**独立成表**，绝不混进记忆检索。"""
        title = fish_common.fts_text("记忆标题")
        body = fish_common.fts_text("记忆正文唯一标记")
        self.conn.execute("INSERT INTO memory_fts (code, title_idx, body_idx) "
                          "VALUES ('TEST-KB10', ?, ?)", (title, body))
        self.conn.commit()
        before = self.rows("SELECT COUNT(*) FROM memory_fts")[0][0]
        vault, vdir = self.make_vault({"x.md": "# 六边形\n\n独立探针：蓝色水母。\n"})
        self.rebuild()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM memory_fts")[0][0], before)
        self.assertEqual(self.rows("SELECT title_idx, body_idx FROM memory_fts "
                                   "WHERE code='TEST-KB10'"), [(title, body)])
        self.assertGreater(self.rows("SELECT COUNT(*) FROM kb_fts")[0][0], 0)
        self.assertEqual(self.search(query="蓝色水母")["meta"]["ranker"], "bm25+field")


class OfflineAndFreshTest(_Base):
    def test_offline_vault_is_skipped_not_wiped(self):
        vault, vdir = self.make_vault({"a.md": "# 甲\n\n离线探针。\n",
                                       "b.md": "# 乙\n\n离线探针二。\n"})
        self.assertEqual(self.rebuild()["added"], 2)
        shutil.rmtree(vdir)
        report = self.rebuild()
        print(f"\n[offline] 根不存在：{report}")
        self.assertEqual(report["offline"], 1)
        self.assertEqual(report["deleted"], 0, "根不存在 ≠ 全库删除")
        self.assertEqual(report["notes"], 0)
        self.assertEqual(fish_kb_index.kb_index_stats(self.conn)["active"], 2)
        self.assertTrue(self.search(query="离线探针")["hits"])

    def test_fresh_then_stale(self):
        vault, vdir = self.make_vault({"a.md": "# 甲\n\n新鲜度探针。\n"})
        self.rebuild()
        fresh = fish_kb_index.kb_index_fresh(self.conn)
        self.assertTrue(fresh["fresh"])
        self.assertEqual(fresh["active_rows"], 1)
        self.assertIsNotNone(fresh["last_indexed_at"])
        self.assertLess(fresh["age_hours"], 1.0)
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute("UPDATE kb_index SET indexed_at=?", (old,))
        self.conn.commit()
        stale = fish_kb_index.kb_index_fresh(self.conn)
        self.assertFalse(stale["fresh"], "8 天前的索引必须判陈旧")
        self.assertGreater(stale["age_hours"], 168.0)
        self.assertEqual(stale["last_indexed_at"], old)
        self.assertTrue(fish_kb_index.kb_index_fresh(self.conn,
                                                     max_age_hours=24 * 30)["fresh"])
        with self.assertRaises(fish_kb.KBError):
            fish_kb_index.kb_index_fresh(self.conn, max_age_hours=0)

    def test_stats_counts(self):
        vault, vdir = self.make_vault({"a.md": "# 甲\n\n一。\n", "b.md": "# 乙\n\n二。\n"})
        self.rebuild()
        stats = fish_kb_index.kb_index_stats(self.conn)
        print(f"\n[stats] {stats}")
        self.assertEqual(stats["rows"], 2)
        self.assertEqual(stats["active"], 2)
        self.assertEqual(stats["deleted"], 0)
        self.assertEqual(stats["error"], 0)
        self.assertEqual(stats["fts_rows"], 2)
        self.assertIsNotNone(stats["last_indexed_at"])
        self.assertGreater(stats["index_mb"], 0.0)


class SearchContractTest(_Base):
    """在线接口的边界（空词 / limit / exclude / vault_id / project）。"""

    def setUp(self):
        super().setUp()
        self.vault_a, self.dir_a = self.make_vault({
            "a.md": "# 甲库笔记\n\n共享词：绿色火箭。\n"},
            name="甲库")
        self.vault_b, self.dir_b = self.make_vault({
            "b.md": "# 乙库笔记\n\n共享词：绿色火箭。\n",
            "c.md": "# 丙库笔记\n\n第一段只说别的事情。\n\n另一探针：黄色潜艇。\n"},
            name="乙库")
        self.rebuild()

    def test_empty_and_blank_query(self):
        for query in ("", "   ", None, "@@@"):
            res = self.search(query=query)
            self.assertEqual(res["hits"], [])
            self.assertEqual(res["meta"]["total_matched"], 0)
            self.assertTrue(res["meta"]["indexed"], "空词也要说明索引是就绪的")

    def test_limit_zero_and_negative(self):
        self.assertEqual(self.search(query="绿色火箭", limit=0)["hits"], [])
        self.assertEqual(self.search(query="绿色火箭", limit=-3)["hits"], [])
        self.assertEqual(len(self.search(query="绿色火箭", limit=1)["hits"]), 1)

    def test_exclude_accepts_code_and_bare_rel(self):
        res = self.search(query="绿色火箭")
        self.assertEqual(len(res["hits"]), 2)
        code = f"kb:{self.vault_a['name']}:a.md"
        one = self.search(query="绿色火箭", exclude=[code])
        self.assertEqual([h["rel"] for h in one["hits"]], ["b.md"],
                         "kb:<vault>:<rel> 形态必须能排除")
        two = self.search(query="绿色火箭", exclude=["a.md", "b.md"])
        self.assertEqual(two["hits"], [], "裸 rel 形态也要能排除")

    def test_vault_id_filter(self):
        res = self.search(query="绿色火箭", vault_id=self.vault_b["vault_id"])
        self.assertEqual([h["rel"] for h in res["hits"]], ["b.md"])
        self.assertEqual(res["hits"][0]["vault_name"], "乙库")
        missing = self.search(query="绿色火箭", vault_id="不存在的库")
        self.assertEqual(missing["hits"], [])
        self.assertEqual(missing["meta"]["total_matched"], 0)

    def test_project_is_accepted_but_not_filtering(self):
        self.assertEqual(len(self.search(query="绿色火箭", project="任意项目")["hits"]), 2)

    def test_meta_shape(self):
        meta = self.search(query="绿色火箭")["meta"]
        for key in ("total_matched", "returned", "indexed", "query_tokens",
                    "as_of", "ranker"):
            self.assertIn(key, meta)
        self.assertEqual(meta["returned"], 2)
        self.assertEqual(meta["total_matched"], 2)
        self.assertIn("绿色", meta["query_tokens"])
        self.assertEqual(meta["ranker"], "bm25+field")

    def test_hit_shape(self):
        hit = self.search(query="黄色潜艇")["hits"][0]
        for key in ("vault_id", "vault_name", "rel", "title", "snippet", "score",
                    "matched_field", "mtime", "size"):
            self.assertIn(key, hit)
        self.assertEqual(hit["rel"], "c.md")
        self.assertEqual(hit["title"], "丙库笔记")
        self.assertEqual(hit["matched_field"], "body_excerpt")
        self.assertEqual(hit["score"], 1.0)
        self.assertLessEqual(len(hit["snippet"]), 160)

    def test_vault_scoped_rebuild(self):
        report = fish_kb_index.rebuild_kb_index(
            self.conn, None, vault_id=self.vault_b["vault_id"], mode="full")
        self.assertEqual(report["vaults"], 1, "只重建指定的库")
        self.assertEqual(report["notes"], 2)
        self.assertEqual(report["unchanged"], 0, "full 模式不产生 unchanged")
        self.assertEqual(report["added"], 0, "行已存在 → 不是新增")
        self.assertEqual(report["updated"], 2, "full 模式全部重写")


if __name__ == "__main__":
    unittest.main(verbosity=2)
