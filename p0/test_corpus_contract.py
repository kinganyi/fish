#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语料层契约判据（原始对话语料层 · T01 · 设计 §2/§10 的 C-P0-9 M01 一线）。

这份文件只回答一个问题：**"语料层没有污染主库、没有第二套切词/脱敏、没有反向依赖"
这三句话有没有可验证的证据**。它照 `test_split_contract.py` 的 J2 法（静态 AST +
干净子进程 `sys.modules` 双查），不看实现细节，只锁住对外的四条承诺：

- **J1 依赖单向**：`fish → fish_corpus* → fish_common`。任何 `fish_corpus*` 文件都不许
  import `fish`（`fish` 反过来要 import `fish_corpus`，反向依赖就是循环导入，
  症状是"某一侧拿到半初始化的模块"——属性缺失会在随机位置以 `AttributeError` 炸）。

- **J2 切词单源**（D08/D09）：`fish.index_tokens` / `fish.fts_text` /
  `fish.fts_match_expr` 与 `fish_common.*` 必须是**同一个对象**。分家之后索引侧与
  查询侧会慢慢漂开，症状是"存进去搜不到"，而两边单测都绿。

- **J3 主库零改动**（D01）：`fish.SCHEMA` 里不许出现 `corpus` 字样。主库加一张语料
  表是**不需要报错**就能犯的错，只有把判据钉在源码上才拦得住（M01 变异体）。

- **J4 脱敏规则是超集**（C-P0-7）：`fish.CREDENTIAL_PATTERNS` 每一条，语料侧都必须
  有同样命中的规则。语料侧可以更宽，**不许更窄**——更窄就是"记忆库拦得住、语料库
  漏出去"。

纪律：本文件不建真库、不写生产目录（需要库时一律 `:memory:`）。
"""
import ast
import contextlib
import importlib.util
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

P0 = Path(__file__).resolve().parent
FISH = P0 / "fish.py"

spec = importlib.util.spec_from_file_location("fish", str(FISH))
fish = importlib.util.module_from_spec(spec)
sys.modules.setdefault("fish", fish)
spec.loader.exec_module(fish)

if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))
import fish_common  # noqa: E402
import fish_corpus  # noqa: E402
import fish_corpus_dsh  # noqa: E402
import fish_corpus_redact as redact_mod  # noqa: E402
import fish_corpus_store as store  # noqa: E402
import fish_corpus_tag  # noqa: E402
import fish_corpus_wb  # noqa: E402

CORPUS_FILES = (
    P0 / "fish_corpus.py",
    P0 / "fish_corpus_store.py",
    P0 / "fish_corpus_redact.py",
    P0 / "fish_corpus_tag.py",
    P0 / "fish_corpus_dsh.py",
    P0 / "fish_corpus_wb.py",
    # T03/T05 新增的两个模块同样受 J1 约束（"新文件是不是偷偷 import 了 fish"）。
    P0 / "fish_corpus_rank.py",
    P0 / "fish_corpus_gov.py",
)

# 每个模块**允许**依赖的本项目模块（设计 §2 的"允许依赖"列）。
ALLOWED_INTERNAL = {
    "fish_corpus.py": {"fish_corpus_dsh", "fish_corpus_wb", "fish_corpus_store",
                       "fish_corpus_tag", "fish_corpus_rank", "fish_corpus_gov",
                       "fish_corpus_redact", "fish_common"},
    "fish_corpus_store.py": {"fish_common"},
    "fish_corpus_redact.py": set(),
    # 打标层要多一份 `redact`：`cwd`/`workspace`/`project_note` 是**可回显列**
    # （`stats.unmapped_cwds` 会把它们回给 AI），写入前必须走同一份规则表
    # （2026-10-27 复审 安全新🟠）。两个适配器本来就允许 import redact。
    "fish_corpus_tag.py": {"fish_corpus_store", "fish_corpus_redact", "fish_surface",
                           "fish_activity"},
    "fish_corpus_dsh.py": {"fish_corpus_store", "fish_corpus_redact", "fish_corpus_tag"},
    "fish_corpus_wb.py": {"fish_corpus_store", "fish_corpus_redact", "fish_corpus_tag"},
    # 排序只依赖"切词单源"（D08/D09），不认识库结构（设计 §2：rank → store/common，
    # 实测只需 common：候选已是过滤好的行，排序不该再摸库）。
    "fish_corpus_rank.py": {"fish_common"},
    # 治理只做编排：台账/删除走 store，脱敏规则走 redact；**不 import 门面**
    # （否则 fish_corpus → gov → fish_corpus 是环，而环的症状是随机 AttributeError）。
    "fish_corpus_gov.py": {"fish_corpus_store", "fish_corpus_redact"},
}
# 第三方白名单：只有 zstandard（已登记在 docs/依赖说明-corpus.md）
ALLOWED_THIRD_PARTY = {"zstandard"}


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _imported_modules(tree: ast.Module) -> set:
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    return mods


class CorpusBoundaryTest(unittest.TestCase):
    """J1：依赖方向与外部依赖面。"""

    def test_01_no_corpus_module_imports_fish(self):
        """J1（静态）：八个模型文件都不许 import `fish`（含 `from fish import …`）。"""
        for path in CORPUS_FILES:
            mods = _imported_modules(ast.parse(_read(path)))
            self.assertNotIn("fish", mods, "%s 反向依赖了 fish（会造成循环导入）" % path.name)

    def test_02_internal_dependencies_match_design(self):
        """J1：内部依赖只能是设计允许的那几条（多一条就是"顺手 import 一下"。）"""
        seen = {}
        for path in CORPUS_FILES:
            mods = _imported_modules(ast.parse(_read(path)))
            internal = {m for m in mods if m.startswith("fish") and m != "fish"}
            seen[path.name] = internal
            allowed = ALLOWED_INTERNAL[path.name]
            extra = sorted(internal - allowed)
            self.assertEqual(extra, [], "%s 依赖了设计之外的模块：%s" % (path.name, extra))

    def test_03_no_third_party_beyond_zstandard(self):
        """J1：第三方只许 `zstandard`（铁律⑰：新依赖必须登记，这条判据是登记口）。"""
        stdlib = set(sys.stdlib_module_names)
        for path in CORPUS_FILES:
            mods = _imported_modules(ast.parse(_read(path)))
            third = {m for m in mods if m not in stdlib
                     and not m.startswith("fish") and m != "__future__"}
            extra = sorted(third - ALLOWED_THIRD_PARTY)
            self.assertEqual(extra, [], "%s 引入了未登记依赖：%s" % (path.name, extra))

    def test_04_runtime_import_does_not_pull_fish(self):
        """J1（运行时）：干净子进程里 `import fish_corpus` **不得**把 `fish` 带进来。

        为什么要子进程：本进程里 `fish` 早已在 `sys.modules`，"带没带进来"就测不出来
        ——那是测试环境的假象，不是代码性质（同 `test_split_contract` J2 理由）。
        """
        probe = (
            "import sys\n"
            "import fish_corpus, fish_corpus_store, fish_corpus_dsh, fish_corpus_wb\n"
            "assert 'fish' not in sys.modules, 'import fish_corpus 把 fish 带进来了'\n"
            "assert 'fish_common' in sys.modules, '语料层应当依赖 fish_common（切词单源）'\n"
            "print('OK-CORPUS-CLEAN')\n"
        )
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", probe], cwd=str(P0),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK-CORPUS-CLEAN", proc.stdout)

    def test_05_no_module_identity_usage(self):
        """J1：语料层 0 处 `__file__` / `__name__`（与 J5 同纪律：不靠"我在哪"定位）。"""
        for path in CORPUS_FILES:
            tree = ast.parse(_read(path))
            hits = [node.lineno for node in ast.walk(tree)
                    if isinstance(node, ast.Name) and node.id in ("__file__", "__name__")]
            self.assertEqual(hits, [], "%s 用了模块自身身份：行 %s" % (path.name, hits))

    def test_06_adapter_registry_has_both_hosts(self):
        """J1：两个宿主都在注册表里（C-P2-1 的扩展位就是这个 dict）。"""
        self.assertEqual(sorted(fish_corpus.ADAPTERS), ["dsh", "workbuddy"])
        self.assertEqual(fish_corpus.adapter_for("dsh").host, "dsh")
        self.assertEqual(fish_corpus.adapter_for("WorkBuddy").host, "workbuddy")
        with self.assertRaises(fish_corpus.CorpusError):
            fish_corpus.adapter_for("codex")       # 未实现宿主必须响亮失败


class TokenizerSingleSourceTest(unittest.TestCase):
    """J2：切词只有一份实现（D08/D09）。"""

    NAMES = ("index_tokens", "fts_text", "fts_match_expr")

    def test_10_tokenizer_is_the_same_object(self):
        for name in self.NAMES:
            self.assertTrue(hasattr(fish_common, name), "fish_common 缺 %s" % name)
            self.assertTrue(hasattr(fish, name), "fish 侧缺 %s" % name)
            self.assertIs(getattr(fish, name), getattr(fish_common, name),
                          "%s 在 fish 与 fish_common 里不是同一个对象" % name)

    def test_11_fish_no_longer_defines_them(self):
        """`fish.py` 里不许再有这三个函数的**定义**（搬走只留再导出）。"""
        tree = ast.parse(_read(FISH))
        defined = {node.name for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        overlap = sorted(defined & set(self.NAMES))
        self.assertEqual(overlap, [], "fish.py 里还有定义：%s（会与 fish_common 分家）" % overlap)

    def test_12_corpus_uses_common_tokenizer(self):
        """语料侧写 FTS 必须走 `fish_common.fts_text`（不是自己拼一份）。"""
        src = _read(P0 / "fish_corpus_store.py")
        self.assertIn("fish_common.fts_text", src,
                      "语料库写索引没走 fish_common.fts_text（两套切法会漂）")
        # 索引侧与查询侧同法：bigram 的 token 能被查询表达式表达出来
        expr = fish_common.fts_match_expr("协作约定")
        self.assertIsNotNone(expr)
        self.assertIn('"协作"', expr)          # OR/AND 都行，词必须加引号
        self.assertIn('"作约"', expr)


class MainSchemaUntouchedTest(unittest.TestCase):
    """J3：主库零新表零新列（D01 / M01 变异体）。"""

    def test_20_schema_has_no_corpus_word(self):
        """`fish.SCHEMA` 的**实际 SQL** 里不许出现 corpus，且建出来的表名也不许有。

        为什么不走 AST 求值：`SCHEMA` 是 `_PUSH_SCHEMA + _IDENTITY_SCHEMA + …` 拼出来的，
        其中还有 `"\\n\\n".join(...)`，静态求全几乎等于重写一遍求值器。判据的目的是
        "主库不长出语料表"，直接拿运行时那份 SQL 与真实建表结果查更硬、更不会误判。
        """
        schema = getattr(fish, "SCHEMA", None)
        self.assertIsInstance(schema, str)
        self.assertNotIn("corpus", schema.lower(),
                         "主库 SCHEMA 里出现了 corpus：语料必须独立成库（D01）")
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            conn.executescript(schema)
            names = [row[0].lower() for row in conn.execute(
                "SELECT name FROM sqlite_master").fetchall()]
        finally:
            conn.close()
        bad = sorted(n for n in names if "corpus" in n)
        self.assertEqual(bad, [], "主库建出了语料对象：%s" % bad)

    def test_20b_schema_sources_are_plain_literals(self):
        """静态侧：`fish.py` 里所有 `_*_SCHEMA` 字面量都不许提 corpus。

        与上一条互补：上一条查"最终跑出来的 SQL"，这一条查"源码里有没有人偷偷把
        corpus 的 DDL 拼进某个分片常量"（那种改法在运行时才看得出来）。
        """
        tree = ast.parse(_read(FISH))
        offenders = []
        scanned = 0
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not any("SCHEMA" in n.upper() for n in names):
                continue
            scanned += 1
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                        and "corpus" in sub.value.lower():
                    offenders.append(names[0])
                    break
        self.assertEqual(offenders, [], "这些 SCHEMA 分片的字面量里有 corpus：%s" % offenders)
        self.assertGreater(scanned, 3, "扫到的 SCHEMA 分片太少，判据可能已失明")

    def test_21_corpus_ddl_is_self_contained(self):
        """语料库的 DDL 必须自成一体（在 `:memory:` 上可完整建出来）。"""
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            counts = store.migrate(conn)
            for table in store.TABLES:
                self.assertIn(table, counts, "语料库缺表 %s" % table)
            row = conn.execute("SELECT v FROM corpus_meta WHERE k='schema_version'").fetchone()
            self.assertEqual(row[0], store.SCHEMA_VERSION)
            # 幂等：再跑一次不报错、不重复建
            store.migrate(conn)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM corpus_meta WHERE k='schema_version'")
                .fetchone()[0], 1)
        finally:
            conn.close()

    def test_22_segment_unique_key_is_enforced(self):
        """幂等键必须由**数据库**保证（PRIMARY KEY/UNIQUE），不是靠 Python 记得查。

        M20 变异体：把 UNIQUE 去掉 → 重跑段数翻倍。判据直接查索引定义。
        """
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            store.migrate(conn)
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND tbl_name='corpus_segment'").fetchall()]
            # SQLite 把表内 UNIQUE 约束实现成自动索引 `sqlite_autoindex_corpus_segment_N`
            self.assertTrue(
                any(n.startswith("sqlite_autoindex_corpus_segment") for n in names),
                "corpus_segment 的 UNIQUE(host,session_id,loc,part) 不见了：%s" % names)
            fts = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='corpus_fts'").fetchone()
            self.assertIn("fts5", (fts[0] or "").lower())
            # 无触发器：bigram 预切只能在 Python 侧做（设计 §3）
            trig = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]
            self.assertEqual(trig, 0, "语料库不该有触发器（切词只在 Python 侧）")
        finally:
            conn.close()


class RedactSingleSourceTest(unittest.TestCase):
    """J4：脱敏规则是主库规则的超集（C-P0-7）。"""

    def test_30_every_fish_pattern_still_hits(self):
        """`fish.CREDENTIAL_PATTERNS` 每一条，语料侧都必须有同样命中的规则。"""
        for pattern in fish.CREDENTIAL_PATTERNS:
            probe = self._probe_for(pattern)
            self.assertIsNotNone(probe, "没有为 %r 准备探针样例（判据自己先漏了）" % pattern)
            hits = redact_mod.scan(probe)
            self.assertNotEqual(hits, [],
                                "主库正则 %r 能命中 %r，语料侧没命中（语料侧更窄=漏）"
                                % (pattern, probe))
            # 主库正则自己也必须命中（自证探针有效）
            self.assertTrue(re.search(pattern, probe))

    @staticmethod
    def _probe_for(pattern: str):
        """给每条主库正则准备一个**最小**触发样例（不是凭记忆，按规则造）。"""
        if "fsh_" in pattern:
            return "会话凭证 fsh_" + "A" * 24
        if "token" in pattern:
            return "export FISH_TOKEN; token = abc"
        if "PRIVATE KEY" in pattern:
            return "-----BEGIN RSA PRIVATE KEY-----"
        return None

    def test_31_new_families_covered(self):
        """新增族（PRD §5.2）：sk-/ghp_/AKIA/Bearer/password 必须命中。"""
        cases = [
            ("sk-" + "a" * 24, "openai_key"),
            ("ghp_" + "b" * 24, "github_token"),
            ("AKIA" + "C" * 16, "aws_access_key"),
            ("Authorization: Bearer " + "d" * 24, "bearer_token"),
            ("password=" + "hunter2", "password"),
            ("密码：abcd1234", "password"),
        ]
        for text, expected in cases:
            names = {name for name, _, _ in redact_mod.scan(text)}
            self.assertIn(expected, names, "%r 没被 %s 命中" % (text, expected))

    def test_32_redact_is_idempotent_and_keeps_length(self):
        """脱敏不许把段删成空串（C-P0-7 红③），且二次脱敏不再计数（出口那道）。"""
        raw = "开始 fsh_" + "E" * 24 + " 结束，密码：abcd1234"
        once, count, hits = redact_mod.redact(raw)
        self.assertGreater(count, 0)
        self.assertIn("开始", once)
        self.assertIn("结束", once)
        self.assertNotIn("fsh_", once)
        self.assertIn("已脱敏", once)
        twice, count2, _ = redact_mod.redact(once)
        self.assertEqual(twice, once, "二次脱敏改变了文本（占位符不该被再次命中）")
        self.assertEqual(count2, 0)
        self.assertEqual(sum(hits.values()), count)

    def test_33_disabled_switch_is_explicit(self):
        """开关只认明确的真/假值；乱写的值必须报告，而不是猜。"""
        import os
        old = os.environ.get(redact_mod.ENV_DISABLE)
        try:
            os.environ[redact_mod.ENV_DISABLE] = "1"
            self.assertTrue(redact_mod.disabled())
            raw = "fsh_" + "F" * 24
            out, count, _ = redact_mod.redact(raw)
            self.assertEqual(out, raw, "关掉脱敏后必须原样返回（C-P0-7 红①的造红前提）")
            self.assertEqual(count, 0)
            os.environ[redact_mod.ENV_DISABLE] = "banana"
            with self.assertRaises(redact_mod.RedactError):
                redact_mod.disabled()
        finally:
            if old is None:
                os.environ.pop(redact_mod.ENV_DISABLE, None)
            else:
                os.environ[redact_mod.ENV_DISABLE] = old


class PointerAndTagContractTest(unittest.TestCase):
    """门牌形态（D04）与打标四档（C-P0-3）的纯函数侧面。"""

    def test_40_pointer_shapes(self):
        sid = "session-3931d23f-17c9-4de8-87b0-d7b782e70cf0"
        self.assertEqual(store.sid8(sid), "3931d23f")
        self.assertEqual(store.sid8("b1b0097b-447b-417a"), "b1b0097b")
        self.assertEqual(store.canonical_pointer("dsh", sid, "seq=1234", 1),
                         "log:dsh:%s#seq=1234.1" % sid)
        self.assertEqual(store.push_pointer("dsh", sid, "seq=1234"),
                         "conv:dsh:3931d23f#seq=1234")

    def test_41_tag_sources_are_explicit(self):
        """`project_source` 四档都要可达，且**缺解析器不等于没命中**要写清说明。"""
        tagger = fish_corpus_tag.Tagger()
        no_cwd = tagger.tag_session({"session_id": "s1"}, "dsh")
        self.assertEqual((no_cwd["project"], no_cwd["project_source"]), ("", "no_cwd"))

        unmapped = tagger.tag_session({"session_id": "s2", "cwd": "I:/x/learn"}, "dsh")
        self.assertEqual(unmapped["project_source"], "unmapped")
        self.assertIn("未注入", unmapped["project_note"] or "")

        mapped = fish_corpus_tag.Tagger(project_of=lambda cwd: "anyi-studio") \
            .tag_session({"session_id": "s3", "cwd": "I:/app/anyi-studio"}, "dsh")
        self.assertEqual((mapped["project"], mapped["project_source"]),
                         ("anyi-studio", "mapped"))

        def boom(_cwd):
            raise RuntimeError("映射表读不了")
        conflict = fish_corpus_tag.Tagger(project_of=boom) \
            .tag_session({"session_id": "s4", "cwd": "I:/app/x"}, "dsh")
        self.assertEqual((conflict["project"], conflict["project_source"]), ("", "conflict"))

    def test_42_subagent_without_parent_is_red(self):
        """C-P0-3 ③：子代理缺 parent/delegation_depth 必须**抛错**，不许当主会话。"""
        tagger = fish_corpus_tag.Tagger()
        with self.assertRaises(fish_corpus_tag.TagError):
            tagger.tag_session({"session_id": "s5", "origin": "subagent",
                                "delegation_depth": 1}, "dsh")
        with self.assertRaises(fish_corpus_tag.TagError):
            tagger.tag_session({"session_id": "s6", "origin": "subagent",
                                "parent_session": "p"}, "dsh")
        ok = tagger.tag_session({"session_id": "s7", "origin": "subagent",
                                 "parent_session": "p", "delegation_depth": 2}, "dsh")
        self.assertEqual((ok["origin"], ok["delegation_depth"]), ("subagent", 2))

    def test_43_parent_without_origin_is_recorded(self):
        """实测 14 个会话"有 parent 无 origin"：按 main+header 处理，但把矛盾记账。"""
        row = fish_corpus_tag.Tagger().tag_session(
            {"session_id": "s8", "parent_session": "p", "delegation_depth": 0}, "dsh")
        self.assertEqual((row["origin"], row["origin_basis"]), ("main", "header"))
        self.assertIn("parent_without_origin", row["extra"])

    def test_44_retag_requires_resolver(self):
        """retag 缺解析器必须拒绝（否则会把已映射会话改回 unmapped）。"""
        with self.assertRaises(fish_corpus_tag.TagError):
            fish_corpus_tag.Tagger().retag(None)

    def test_45_capability_errors_are_explicit(self):
        """T03/T05 交付后：能力都在位，但**参数缺失/门牌形态错仍要响亮失败**。

        这条判据 2026-10-27 换过一次口径（交付前它查 `CorpusNotReady`）。
        换的理由不是"测试碍事"，而是它保护的对象变了：从"别假装功能已存在"
        变成"别把错误参数静默成空结果"——后者才是现在真实存在的风险。
        """
        with self.assertRaises(fish_corpus.CorpusError):
            fish_corpus.search("x")                       # 没有 root/conn → 不许猜库在哪
        with self.assertRaises(fish_corpus.CorpusError):
            fish_corpus.read("log:dsh:s#seq=1")           # 同上
        with self.assertRaises(fish_corpus.CorpusError):
            fish_corpus.doctor()                          # 体检必须指名库
        with self.assertRaises(ValueError):
            fish_corpus.search("x", ranker="no-such")     # 未登记的 ranker 不许回落
        # 门牌解析是纯函数：形态错 → 带 code 的 PointerError
        with self.assertRaises(store.PointerError) as ctx:
            store.parse_pointer("conv:dsh:abc")           # 缺 #loc
        self.assertEqual(ctx.exception.code, "bad_pointer")


class DailyCorpusGateTest(unittest.TestCase):
    """复审 🟠-1（2026-10-27）：日报里的语料采集**默认不执行**。

    现场：`daily_report.main()` **无条件**调 `corpus_ingest_step()`，而它默认扫的根是
    真实日志目录（实测 515 个 DSH 会话 + 4GB WorkBuddy）——于是每条日报用例都变成
    十分钟级，整轮全量跑不完。这是"代码默认值"的缺陷，不是测试的锅。

    判据三档（每条都是"坏输入必须红"）：
      ① 默认（无配置 / 无环境变量）→ `ingest` 一次都不许被调用，步本体 < 1 s；
      ② 配置 `corpus.daily_enabled=1` → 必须调用，且用的是同一个 root；
      ③ `FISH_CORPUS_DISABLE=1` 压过配置；CLI `--corpus/--no-corpus` 压过两者。
    """

    def setUp(self):
        import daily_report
        self.dr = daily_report
        self.base = Path(tempfile.mkdtemp(prefix="fish-daily-gate-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.data = self.base / "data"
        self.data.mkdir(parents=True)
        conn = fish.connect(self.data)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
        finally:
            conn.close()
        # 只改常量（与日报既有判据同一手法）：备份/报告/日志一个字节都不碰生产 `p0/data`。
        self._saved = (self.dr.DATA, self.dr.DB, self.dr.BACKUPS, self.dr.LOG)
        self.dr.DATA = self.data
        self.dr.DB = self.data / "index.sqlite"
        self.dr.BACKUPS = self.data / "_backups"
        self.dr.LOG = self.data / "_reports" / "_run.log"
        self._env = os.environ.get(self.dr.ENV_CORPUS_DISABLE)
        os.environ.pop(self.dr.ENV_CORPUS_DISABLE, None)

    def tearDown(self):
        (self.dr.DATA, self.dr.DB, self.dr.BACKUPS, self.dr.LOG) = self._saved
        if self._env is None:
            os.environ.pop(self.dr.ENV_CORPUS_DISABLE, None)
        else:
            os.environ[self.dr.ENV_CORPUS_DISABLE] = self._env

    # ------------------------------------------------------------------ 夹具
    def _set_config(self, value):
        conn = fish.connect(self.data)
        try:
            fish.set_config(conn, self.dr.CORPUS_CFG_KEY, value)
        finally:
            conn.close()

    FAKE = {"busy": False, "sessions_seen": 1, "segments_new": 2, "degraded": [],
            "orphan": [], "lag_sec": 10, "purged_by_segment_tombstone": 0,
            "warnings": []}

    # ------------------------------------------------------------------ 判据
    def test_default_step_does_not_ingest_and_stays_under_a_second(self):
        """默认 → 不扫真实日志，并且这一步本身要快（旧实现是十分钟级）。"""
        with mock.patch.object(fish_corpus, "ingest",
                               side_effect=AssertionError("默认就去扫真实日志了")):
            started = time.monotonic()
            line = self.dr.corpus_ingest_step(self.data)
            elapsed = time.monotonic() - started
        self.assertIn("跳过", line, line)
        self.assertIn(self.dr.CORPUS_CFG_KEY, line, "跳过时必须写清怎么开启（不能静默）")
        self.assertLess(elapsed, 1.0, "默认闸门还在干活：%.2fs" % elapsed)

    def test_default_main_does_not_scan_sources(self):
        """走 `main()` 的接线也要判（闸门只在 step 里加、main 没接上一样是缺陷）。"""
        with mock.patch.object(fish_corpus, "ingest",
                               side_effect=AssertionError("main() 默认扫了语料")):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self.dr.main(["--skip-report"])
        self.assertEqual(rc, 0)

    def test_config_enables_ingest(self):
        self._set_config(1)
        with mock.patch.object(fish_corpus, "ingest",
                               return_value=dict(self.FAKE)) as patched:
            line = self.dr.corpus_ingest_step(self.data)
        self.assertTrue(patched.called, "配置开启后 ingest 没被调用")
        self.assertEqual(Path(patched.call_args.kwargs["root"]), self.data,
                         "采错目录了（不许落到真实日志根）")
        self.assertIn("新增 2 段", line)

    def test_env_disable_beats_config(self):
        self._set_config(1)
        os.environ[self.dr.ENV_CORPUS_DISABLE] = "1"
        with mock.patch.object(fish_corpus, "ingest",
                               side_effect=AssertionError("环境变量关断失效")):
            line = self.dr.corpus_ingest_step(self.data)
        self.assertIn("跳过", line, line)

    def test_env_value_must_be_explainable(self):
        """`FISH_CORPUS_DISABLE=banana` 不许被猜成开或关（两种猜法都会做错决定）。

        这里也把 `ingest` 钉住（不许真跑）：坏值的正确处置是"不采 + 说清原因"，
        而这条判据不能让"猜成开"那条路去扫真实日志（那正是本批要修的那个坑）。
        """
        os.environ[self.dr.ENV_CORPUS_DISABLE] = "banana"
        with mock.patch.object(fish_corpus, "ingest",
                               side_effect=AssertionError("坏 env 值被猜成开启了")):
            line = self.dr.corpus_ingest_step(self.data)
        self.assertIn("无法解释", line, line)

    def test_cli_flags_override_config_and_env(self):
        with mock.patch.object(fish_corpus, "ingest",
                               return_value=dict(self.FAKE)) as patched:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self.dr.main(["--skip-report", "--corpus"])
        self.assertEqual(rc, 0)
        self.assertTrue(patched.called, "--corpus 没打开采集")
        self._set_config(1)
        with mock.patch.object(fish_corpus, "ingest",
                               side_effect=AssertionError("--no-corpus 失效")):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self.dr.main(["--skip-report", "--no-corpus"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
