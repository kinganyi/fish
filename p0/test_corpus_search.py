#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C-P0-5 / C-P0-3 的检索侧判据（T03 · 设计 §6/§10）。

这份文件回答三个问题：

1. **存进去的能搜到吗**（C-P0-5 绿）：中文按 bigram 切、查询侧同一套切法；
2. **过滤与门牌有没有假绿**（C-P0-5 红①②③）：project 过滤不能漏别项目、
   源文件删掉后 `read` 仍回正文并声明 `source.missing`、不存在的词必须空结果；
3. **换算法不改调用面**（D07）：`RANKERS` 注册表 + 未知 ranker 响亮失败。

变异体（每条都能让本文件红）：
| 变异 | 会被哪条判据抓到 |
|---|---|
| M10 索引侧 3-gram / 查询侧 2-gram | `test_zh_bigram_survives_both_sides`（「协作」查不到） |
| M17 `read` 源缺失时回填摘要 | `SourceMissingTest`（正文必须与被采的原话全等） |

纪律：所有库在 `tempfile` 根下，**绝不碰 `p0/data/`**。
"""
import datetime
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import zstandard

P0 = Path(__file__).resolve().parent
if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))
import fish_common  # noqa: E402
import fish_corpus  # noqa: E402
import fish_corpus_rank as rank_mod  # noqa: E402
import fish_corpus_store as store  # noqa: E402

MS = 1790918381967
LONG = ("撞车检测靠占用面重叠判定：先比目录前缀，再比文件后缀；同一份声明窗口内"
        "出现两条就算撞车。这条判据的唯一真值是 overlap，不是谁先签到。")
PROJECT = "anyi-studio"


def frame(payload) -> bytes:
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return zstandard.ZstdCompressor().compress(blob + b"\n")


def ev_session(sid, cwd="I:/app/anyi-studio", origin=None, parent=None, depth=None):
    data = {"type": "session", "version": 4, "id": sid, "createdAt": MS, "cwd": cwd}
    if origin:
        data["origin"] = origin
    if parent:
        data["parentSession"] = parent
    if depth is not None:
        data["delegationDepth"] = depth
    return data


def ev_user(seq, text):
    return {"type": "user/message", "seq": seq, "time": MS + seq * 10,
            "data": {"content": [{"type": "text", "text": text}],
                     "source": {"kind": "user"}, "role": "user", "id": "u%d" % seq}}


def ev_assistant(seq, texts):
    content = [{"type": "reasoning", "text": "不该入库"}]
    content.extend({"type": "text", "text": t} for t in texts)
    return {"type": "assistant/message", "seq": seq, "time": MS + seq * 10,
            "data": {"turn": 1, "step": 1,
                     "message": {"role": "assistant", "content": content,
                                 "id": "a%d" % seq}}}


class CorpusCase(unittest.TestCase):
    """公共夹具：临时根 + 会话文件 + 已采集的库（永不碰生产 data/）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fishcorpus-search-"))
        self.root = self.tmp / "data"
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir(parents=True)
        self.conn = store.open_db(self.root)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def write_session(self, sid, events, ws="--I-app-anyi-studio--"):
        folder = self.sessions / ws / sid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "session.v4.jsonl.zstd"
        path.write_bytes(b"".join(frame(ev) for ev in events))
        return path

    def ingest(self, *, project_of=lambda cwd: PROJECT, citizen_of=lambda key: "cit-1"):
        return fish_corpus.ingest(
            ("dsh",), root=self.root, conn=self.conn, roots={"dsh": [self.sessions]},
            use_lock=False, project_of=project_of, citizen_of=citizen_of)

    def one_session(self, sid="3931d23f-17c9-4de8-87b0-d7b782e70cf0", **kw):
        events = [ev_session(sid, **kw),
                  ev_user(9, "撞车检测怎么做的"),
                  ev_assistant(10, [LONG])]
        self.write_session(sid, events)
        self.ingest()
        return sid


class SearchBaselineTest(CorpusCase):
    """C-P0-5 绿：中文 bigram 两侧同法、命中能读回、字段形状冻结。"""

    def test_zh_bigram_survives_both_sides(self):
        """M10：索引侧 2-gram 与查询侧 2-gram 必须一致（「协作」查得到）。"""
        sid = "aaaaaaaa-0000-0000-0000-000000000001"
        self.write_session(sid, [ev_session(sid), ev_user(1, "协作约定怎么写"),
                                 ev_assistant(2, [LONG])])
        self.ingest()
        self.assertIn("协作", fish_common.fts_text("协作约定").split())
        res = fish_corpus.search("协作约定", root=self.root)
        self.assertGreaterEqual(res["meta"]["total_matched"], 1)
        self.assertTrue(any("协作" in hit["snippet"] for hit in res["hits"]))

    def test_hits_shape_is_frozen(self):
        """`hits[]` 字段按 PRD §8.1 冻结：多一个少一个都是契约变更。"""
        self.one_session()
        res = fish_corpus.search("撞车检测", root=self.root)
        self.assertTrue(res["hits"])
        expected = {"pointer", "host", "session_id", "session_title", "role",
                    "occurred_at", "project", "workspace", "citizen_id", "origin",
                    "shadowed", "snippet", "chars", "score", "ranker", "why"}
        self.assertEqual(set(res["hits"][0]), expected)
        self.assertEqual(set(res["meta"]),
                         {"total_matched", "returned", "ranker", "filters_echo",
                          "degraded", "as_of", "truncated", "limit", "budget_tokens"})
        self.assertEqual(res["hits"][0]["ranker"], "default")
        self.assertTrue(res["hits"][0]["why"])

    def test_pointer_is_canonical_and_readable(self):
        sid = self.one_session()
        hit = fish_corpus.search("撞车检测", root=self.root)["hits"][0]
        self.assertEqual(hit["pointer"], "log:dsh:%s#seq=9" % sid)
        read = fish_corpus.read(hit["pointer"], root=self.root)
        self.assertIn("撞车检测", read["text"])
        self.assertEqual(read["code"], "conv:dsh:3931d23f#seq=9")

    def test_nonexistent_word_is_empty_not_error(self):
        """C-P0-5 红①：不存在的词 → `hits=[]` + `total_matched=0`（不是异常也不是假命中）。"""
        self.one_session()
        res = fish_corpus.search("根本不存在的词组xyz", root=self.root)
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["meta"]["total_matched"], 0)

    def test_terms_given_are_used_as_is(self):
        """`terms` 给了就**只用给的**：用不相干的 q + terms 仍要命中。"""
        self.one_session()
        res = fish_corpus.search("完全不相干的句子", terms=["撞车"], root=self.root)
        self.assertGreaterEqual(res["meta"]["total_matched"], 1)

    def test_limit_is_clamped_and_declared(self):
        self.one_session()
        res = fish_corpus.search("撞车", limit=999, root=self.root)
        self.assertEqual(res["meta"]["limit"], fish_corpus.SEARCH_MAX_LIMIT)
        self.assertLessEqual(res["meta"]["returned"], fish_corpus.SEARCH_MAX_LIMIT)

    def test_budget_truncates_and_says_so(self):
        self.one_session()
        full = fish_corpus.search("撞车", root=self.root)
        self.assertGreaterEqual(len(full["hits"]), 2)
        tight = fish_corpus.search("撞车", budget_tokens=40, root=self.root)
        self.assertEqual(len(tight["hits"]), 1)
        self.assertTrue(tight["meta"]["truncated"])

    def test_missing_db_is_declared_not_silent(self):
        """库不存在 → 空 hits + `degraded=[db_missing]`（"没采过"和"没命中"要分得开）。"""
        res = fish_corpus.stats(root=self.root)
        self.assertFalse(res.get("db_missing"))
        empty_root = self.tmp / "empty"
        res = fish_corpus.search("任意", root=empty_root)
        self.assertEqual(res["hits"], [])
        self.assertEqual([item["reason"] for item in res["meta"]["degraded"]],
                         ["db_missing"])


class FilterTest(CorpusCase):
    """C-P0-5 红②：过滤必须真的过滤；`project=['']` 是"只要未归属"。"""

    def setUp(self):
        super().setUp()
        mine = "b1b0097b-0000-0000-0000-000000000001"
        other = "c2c2c2c2-0000-0000-0000-000000000002"
        self.write_session(mine, [ev_session(mine), ev_user(1, "撞车判据在 overlap"),
                                  ev_assistant(2, [LONG])])
        self.write_session(other, [ev_session(other, cwd="I:/app/other-proj"),
                                   ev_user(1, "撞车判据也在别的项目"),
                                   ev_assistant(2, [LONG.replace("撞车", "碰撞")])],
                           ws="--I-app-other-proj--")
        fish_corpus.ingest(("dsh",), root=self.root, conn=self.conn,
                           roots={"dsh": [self.sessions]}, use_lock=False,
                           project_of=lambda cwd: ("anyi-studio" if "anyi" in cwd.lower()
                                                   else "other-proj"),
                           citizen_of=lambda key: "cit-1")

    def test_project_filter_excludes_other_projects(self):
        res = fish_corpus.search("撞车", filters={"project": ["anyi-studio"]},
                                 root=self.root)
        self.assertTrue(res["hits"])
        self.assertEqual({hit["project"] for hit in res["hits"]}, {"anyi-studio"})

    def test_empty_project_means_unattributed_only(self):
        self.write_session("d3d3d3d3-0000-0000-0000-000000000003",
                           [ev_session("d3d3d3d3-0000-0000-0000-000000000003",
                                       cwd="I:/app/unmapped"),
                            ev_assistant(2, [LONG])], ws="--I-app-unmapped--")
        fish_corpus.ingest(("dsh",), root=self.root, conn=self.conn,
                           roots={"dsh": [self.sessions]}, use_lock=False,
                           project_of=lambda cwd: None, citizen_of=lambda key: "cit-1")
        res = fish_corpus.search("撞车", filters={"project": [""]}, root=self.root)
        self.assertTrue(res["hits"])
        self.assertEqual({hit["project"] for hit in res["hits"]}, {""})

    def test_unknown_filter_key_is_loud(self):
        with self.assertRaises(fish_corpus.CorpusStoreError):
            fish_corpus.search("撞车", filters={"projcet": ["x"]}, root=self.root)

    def test_local_time_is_rejected(self):
        """本地时间形态必须报错：静默换算会差 8 小时，那是隐形错误。"""
        with self.assertRaises(fish_corpus.CorpusStoreError):
            fish_corpus.search("撞车", time_from="2026-10-01 00:00:00", root=self.root)

    def test_shadowed_excluded_by_default(self):
        sid = "b1b0097b-0000-0000-0000-000000000001"
        self.conn.execute("UPDATE corpus_segment SET shadowed=1 WHERE session_id=?", (sid,))
        self.conn.commit()
        hidden = fish_corpus.search("撞车", filters={"role": ["user"]}, root=self.root)
        self.assertEqual([hit for hit in hidden["hits"] if hit["shadowed"]], [])
        shown = fish_corpus.search("撞车", filters={"role": ["user"]},
                                   include_shadowed=True, root=self.root)
        self.assertTrue(any(hit["shadowed"] for hit in shown["hits"]))


class ReadTest(CorpusCase):
    """C-P0-5 红③ / M17：门牌三档与"源缺失仍回正文"。"""

    def setUp(self):
        super().setUp()
        self.sid = self.one_session()

    def test_conv_pointer_is_completed_to_log(self):
        res = fish_corpus.read("conv:dsh:3931d23f#seq=10", root=self.root)
        self.assertEqual(res["pointer"], "log:dsh:%s#seq=10" % self.sid)
        self.assertEqual(res["sid8"], "3931d23f")
        self.assertIn("撞车检测", res["text"])

    def test_bad_pointer_is_typed(self):
        for bad in ("", "seq=1", "log:dsh:abc", "log:dsh:abc#7"):
            with self.assertRaises(fish_corpus.PointerError) as ctx:
                fish_corpus.read(bad, root=self.root)
            self.assertEqual(ctx.exception.code, "bad_pointer", bad)

    def test_not_found_is_typed(self):
        with self.assertRaises(fish_corpus.PointerError) as ctx:
            fish_corpus.read("log:dsh:%s#seq=999" % self.sid, root=self.root)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_ambiguous_sid8_returns_candidates(self):
        twin = "3931d23f-9999-4de8-87b0-000000000000"
        self.write_session(twin, [ev_session(twin), ev_user(1, "撞车 另一个会话"),
                                  ev_assistant(2, [LONG])])
        self.ingest()
        with self.assertRaises(fish_corpus.PointerError) as ctx:
            fish_corpus.read("conv:dsh:3931d23f#seq=1", root=self.root)
        self.assertEqual(ctx.exception.code, "ambiguous_pointer")
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_source_missing_still_returns_projection(self):
        """M17：源文件删掉后 `read` 必须**回正文**并声明 missing（不许摘要顶替/报错）。"""
        stored = self.conn.execute(
            "SELECT text FROM corpus_segment WHERE session_id=? AND loc='seq=10'",
            (self.sid,)).fetchone()[0]
        for path in self.sessions.rglob("session.v4.jsonl.zstd"):
            path.unlink()
        res = fish_corpus.read("log:dsh:%s#seq=10" % self.sid, root=self.root)
        self.assertTrue(res["source"]["missing"])
        self.assertEqual(res["text"], stored)
        self.assertIn("投影", res["source"]["note"])
        self.assertNotIn("summary", res)

    def test_max_chars_truncates_and_declares(self):
        res = fish_corpus.read("log:dsh:%s#seq=10" % self.sid, max_chars=10,
                              root=self.root)
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["text"]), 10)

    def test_access_log_never_stores_query_text(self):
        secret = "撞车检测唯一探针串-zqx"
        fish_corpus.search(secret, root=self.root)
        rows = store.list_access_log(self.conn, limit=10)
        self.assertTrue(rows)
        blob = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn(secret, blob)
        self.assertTrue(rows[0]["query_hash"])


class RankerTest(unittest.TestCase):
    """D07：注册表可换、未知响亮失败、默认公式（0.6/0.2/0.2）可核。"""

    def test_unknown_ranker_is_loud(self):
        with self.assertRaises(ValueError):
            rank_mod.get_ranker("no-such-ranker")

    def test_duplicate_registration_is_loud(self):
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.register_ranker("default", rank_mod.DefaultRanker)

    def test_default_formula_components(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = (now - datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cands = [
            {"pointer": "log:dsh:s#seq=1", "bm25": -1.0, "text": "协作 约定",
             "occurred_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "project": "PJ"},
            {"pointer": "log:dsh:s#seq=2", "bm25": -5.0, "text": "无关正文",
             "occurred_at": old, "project": "OTHER"},
        ]
        ranked = rank_mod.DefaultRanker().rank(
            cands, "协作", {"terms": ["协作"], "project": "PJ", "now": now})
        scores = {pointer: score for pointer, score, _why in ranked}
        self.assertGreater(scores["log:dsh:s#seq=2"], scores["log:dsh:s#seq=1"],
                           "BM25 更强的那条必须排在前面（权重 0.6）")
        why = dict((pointer, why) for pointer, _score, why in ranked)
        self.assertIn("命中 1/1 个词", why["log:dsh:s#seq=1"])
        self.assertIn("同项目", why["log:dsh:s#seq=1"])

    def test_single_candidate_gets_full_rank_norm(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        cands = [{"pointer": "log:dsh:s#seq=1", "bm25": -3.0, "text": "协作",
                  "occurred_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "project": ""}]
        (pointer, score, _why), = rank_mod.DefaultRanker().rank(cands, "协作", {"terms": ["协作"]})
        self.assertEqual(pointer, "log:dsh:s#seq=1")
        self.assertGreater(score, 0.99)     # 0.6+0.2+0.2（单候选归一为 1.0）

    def test_registered_ranker_is_used(self):
        class OnlyOne:
            name = "custom-once"

            def rank(self, cands, q, ctx):
                return [(cands[0]["pointer"], 1.0, ["自定义"])] if cands else []

        if "custom-once" not in rank_mod.RANKERS:
            rank_mod.register_ranker("custom-once", OnlyOne)
        self.assertEqual(rank_mod.get_ranker("custom-once").name, "custom-once")

    def test_weights_must_sum_to_one(self):
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.DefaultRanker(w_rank=0.9)


class TaggingTest(CorpusCase):
    """C-P0-3：打标四档会在检索结果里如实体现（R1/R2 不许猜）。"""

    def test_mapped_and_unmapped_projects_are_distinct(self):
        mapped = "e1e1e1e1-0000-0000-0000-000000000001"
        unmapped = "e2e2e2e2-0000-0000-0000-000000000002"
        self.write_session(mapped, [ev_session(mapped), ev_assistant(2, [LONG])])
        self.write_session(unmapped, [ev_session(unmapped, cwd="I:/app/DSH-Home/workspace/learn"),
                                      ev_assistant(3, [LONG])], ws="--I-app-learn--")
        fish_corpus.ingest(("dsh",), root=self.root, conn=self.conn,
                           roots={"dsh": [self.sessions]}, use_lock=False,
                           project_of=lambda cwd: (PROJECT if "anyi" in cwd.lower() else None),
                           citizen_of=lambda key: "cit-1")
        rows = {row["session_id"]: row for row in self.conn.execute(
            "SELECT * FROM corpus_session").fetchall()}
        self.assertEqual(rows[mapped]["project"], PROJECT)
        self.assertEqual(rows[mapped]["project_source"], "mapped")
        self.assertEqual(rows[unmapped]["project"], "")
        self.assertEqual(rows[unmapped]["project_source"], "unmapped")

    def test_directory_name_never_decides_project(self):
        """R1：目录名 `--I-app-anyi-studio--` 不参与判项目。"""
        sid = "e3e3e3e3-0000-0000-0000-000000000003"
        self.write_session(sid, [ev_session(sid, cwd="I:/elsewhere"),
                                 ev_assistant(2, [LONG])], ws="--I-app-anyi-studio--")
        self.ingest(project_of=lambda cwd: None)
        row = store.get_session(self.conn, "dsh", sid)
        self.assertEqual(row["project"], "")


class CliThinShellTest(CorpusCase):
    """CLI 薄壳：逻辑在 `run_cli`，输出行必须能被人读懂（含门牌与为什么）。"""

    def test_search_and_read_lines(self):
        self.one_session()
        rc, lines, accept = fish_corpus.run_cli("search", root=self.root, target="撞车检测")
        self.assertEqual(rc, 0)
        self.assertTrue(any("命中" in line for line in lines))
        self.assertTrue(any(line.startswith("- log:dsh:") for line in lines))
        rc, lines, accept = fish_corpus.run_cli(
            "read", root=self.root, target="conv:dsh:3931d23f#seq=10")
        self.assertEqual(rc, 0)
        self.assertEqual(accept, ["conv:dsh:3931d23f#seq=10"])
        self.assertTrue(any("撞车检测" in line for line in lines))

    def test_bad_pointer_returns_exit_2(self):
        self.one_session()
        rc, lines, _accept = fish_corpus.run_cli("read", root=self.root, target="nonsense")
        self.assertEqual(rc, 2)
        self.assertIn("bad_pointer", lines[0])

    def test_doctor_ok_on_clean_corpus(self):
        self.one_session()
        rc, lines, _accept = fish_corpus.run_cli("doctor", root=self.root)
        self.assertEqual(rc, 0, lines)
        self.assertIn("OK", lines[0])


class McpSurfaceTest(unittest.TestCase):
    """工具数 18 → 19；旧工具的名字与顺序一字不动；corpus 只有三个只读动作。"""

    OLD_TOOLS = ["remember", "recall", "neighbors", "resolve_code", "read_body",
                 "add_project", "supersede", "whoami", "register", "rename_citizen",
                 "close_project", "reopen_project", "inject", "healthcheck",
                 "session_start", "board", "kb", "kb_write"]

    def test_tool_count_and_order(self):
        import mcp_server
        names = [tool["name"] for tool in mcp_server.TOOLS]
        self.assertEqual(len(names), 19)
        self.assertEqual(names[:-1], self.OLD_TOOLS)
        self.assertEqual(names[-1], "corpus")

    def test_corpus_tool_is_read_only_and_has_token(self):
        import mcp_server
        tool = [t for t in mcp_server.TOOLS if t["name"] == "corpus"][0]
        schema = tool["inputSchema"]
        self.assertEqual(tuple(schema["properties"]["action"]["enum"]),
                         mcp_server.CORPUS_TOOL_ACTIONS)
        self.assertEqual(mcp_server.CORPUS_TOOL_ACTIONS, ("search", "read", "stats"))
        self.assertEqual(schema["required"], ["action"])
        self.assertIn("token", schema["properties"])       # 由 _attach_token 统一补

    # 2026-10-27 复审 🟡：这里原本有一条 `test_old_tool_schemas_unchanged`，只锁了三个
    # 工具的 `required`——改一句描述、挪一个参数都不会红，属于**弱锁**。逐字强锁已经
    # 在 `McpCorpusSurfaceTest.test_old_tool_schemas_are_locked_verbatim`（golden JSON，
    # 18 个旧工具 × description + inputSchema），所以弱锁删除，只留那一份。


class McpCorpusSurfaceTest(unittest.TestCase):
    """MCP 层的两条硬要求（M2 / 🟡-17）。

    ① `project=""` 是**有效过滤值**（只要未归属），不是"不过滤"——门面正确，
       门面之上漏判了一次，症状是"我要只看未归属，结果拿到了全部"；
    ② 18 个旧工具的 description + parameters **逐字**锁死（旧版只锁了 required，
       改一句描述、挪一个参数都不会红）。
    """

    OLD_TOOLS = ["remember", "recall", "neighbors", "resolve_code", "read_body",
                 "add_project", "supersede", "whoami", "register", "rename_citizen",
                 "close_project", "reopen_project", "inject", "healthcheck",
                 "session_start", "board", "kb", "kb_write"]

    def test_old_tool_schemas_are_locked_verbatim(self):
        import mcp_server
        golden_path = P0 / "tools" / "golden_mcp_tools.json"
        self.assertTrue(golden_path.exists(),
                        "golden 缺失：先跑 `python tools/golden_mcp_tools.py`（见该文件头）")
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(golden), sorted(self.OLD_TOOLS),
                         "golden 名单与 18 个旧工具不一致（判据自己漂了）")
        by_name = {tool["name"]: tool for tool in mcp_server.TOOLS}
        for name, expected in golden.items():
            actual = {"description": by_name[name]["description"],
                      "inputSchema": by_name[name]["inputSchema"]}
            self.assertEqual(actual, expected,
                             "旧工具 %s 的 description/parameters 变了（逐字锁）" % name)


class McpProjectFilterTest(unittest.TestCase):
    """M2：MCP 的 `project=""` 必须真的只返回未归属（走真 MCP 协议，不直调函数）。"""

    UNMAPPED_TEXT = "未归属会话里的独有词 孤舟蓑笠翁"
    MAPPED_TEXT = "映射会话里的独有词 孤舟蓑笠翁"

    def setUp(self):
        import fish
        self.tmp = Path(tempfile.mkdtemp(prefix="fishcorpus-mcp-"))
        self.root = self.tmp / "data"
        self.root.mkdir(parents=True)
        conn = fish.connect(self.root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                "created_at) VALUES (?,?,?,?,?,?,?)",
                ("01CIT00000000000000000001", "语料测试公民", "resident", "owner", None,
                 "active", fish.now_utc()))
            conn.commit()
        finally:
            conn.close()
        db = store.open_db(self.root)
        try:
            db.execute(
                "INSERT INTO corpus_session (host, session_id, sid8, project, project_source,"
                " project_note, title, segments, shadowed) VALUES ('dsh','sid-mapped','sid-mapp',"
                "'PJ','mapped',NULL,'映射会话',1,0)")
            db.execute(
                "INSERT INTO corpus_session (host, session_id, sid8, project, project_source,"
                " project_note, title, segments, shadowed) VALUES ('dsh','sid-unmap','sid-unma',"
                "'','unmapped',NULL,'未归属会话',1,0)")
            store.upsert_segment(db, {
                "host": "dsh", "session_id": "sid-mapped", "loc": "seq=1", "part": 0,
                "role": "assistant", "occurred_at": "2026-10-27T00:00:00Z",
                "text": self.MAPPED_TEXT, "chars": len(self.MAPPED_TEXT), "redacted_count": 0,
                "raw_sha256": store.text_sha256(self.MAPPED_TEXT), "byte_from": 0,
                "byte_to": 1, "offset_space": "decompressed", "shadowed": 0})
            store.upsert_segment(db, {
                "host": "dsh", "session_id": "sid-unmap", "loc": "seq=1", "part": 0,
                "role": "assistant", "occurred_at": "2026-10-27T00:00:00Z",
                "text": self.UNMAPPED_TEXT, "chars": len(self.UNMAPPED_TEXT),
                "redacted_count": 0, "raw_sha256": store.text_sha256(self.UNMAPPED_TEXT),
                "byte_from": 0, "byte_to": 1, "offset_space": "decompressed", "shadowed": 0})
            db.commit()
        finally:
            db.close()
        from test_mcp import MCPClient
        self.c = MCPClient(str(self.root))
        self.c.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "corpus-mcp", "version": "0"}})

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _search(self, **args):
        return self.c.request("tools/call", {"name": "corpus",
                                             "arguments": dict(action="search", **args)})

    @staticmethod
    def _text(resp):
        return resp["result"]["content"][0]["text"]

    def test_empty_project_means_unattributed_only(self):
        text = self._text(self._search(query="孤舟蓑笠翁", project=""))
        self.assertIn("sid-unma", text)
        self.assertNotIn("sid-mapp", text,
                         "project='' 被当成「不过滤」：未归属过滤变成了看全部（M2）")
        self.assertIn("hits=1", text)

    def test_project_value_still_filters(self):
        text = self._text(self._search(query="孤舟蓑笠翁", project="PJ"))
        self.assertIn("sid-mapp", text)
        self.assertNotIn("sid-unma", text)

    def test_missing_project_means_no_filter(self):
        text = self._text(self._search(query="孤舟蓑笠翁"))
        self.assertIn("hits=2", text, "不传 project 时应当不过滤")

    def test_tool_description_declares_no_isolation(self):
        """M7：语料面不做项目隔离这件事必须**写在工具描述里**（待拍板项也一样）。"""
        import mcp_server
        tool = [t for t in mcp_server.TOOLS if t["name"] == "corpus"][0]
        self.assertIn("单用户", tool["description"])
        self.assertIn("不做项目/身份隔离", tool["description"])
        self.assertIn("待拍板", tool["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
