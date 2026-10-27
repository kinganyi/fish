#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T01 判据 · 共享原语（`fish_surface` / `fish_naming`）。

## 这一层为什么必须有判据

`fish_surface` 是**占用面判定的唯一实现**：看板重叠、注入包、撞车 ask、
采集聚合全部用它。它错一次，四个功能同时错，而且错得**静默**
（"看着有判定，其实永远不命中"或"到处误报"）——两种都实测发生过：

  · `surface_hit` 只做字符串前缀 → `a/b` 命中 `a/bc`（误报）；
  · 只做同基准比较 → 绝对路径 vs 相对路径永远不命中（漏报，`EVAL-261025-09`）。

## 判据清单

  A1 `normalize_surface` / `surface_hit` 与 fish 旧版**逐字兼容**（24 处调用不改）
  A2 `normalize_path`：反斜杠/重复斜杠/`.`/`..`/盘符根/尾斜杠
  A3 `path_hit`：绝对 vs 相对能命中；`≥2 段` 门槛；不做字符串包含
  A3b `path_hit` 的**相对目录面**按分量链包含命中更深的目标（评审 L2），
      文件面仍走后缀语义；1 段面仍被门槛挡住
  A4 `longest_prefix`：最长优先 + 分量边界 + 无匹配返回 None
  A5 `relativize` / `dir_of` / `make_abs`：展示与机器两套基准不互相冒充
  A6 `fish_naming`：不吐 ID / 重名消歧 / 自动名识别 / 项目名
  A7 变异自证：拆掉 `path_hit` 的「≥2 段」条件 → A3 必须红
  A8 变异自证：把 `base_label` 改成吐短 id → A6 必须红
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_naming  # noqa: E402
import fish_surface  # noqa: E402


class SurfaceCompatTest(unittest.TestCase):
    """A1：现有 24 处调用与既有测试依赖这两个函数，语义必须逐字不变。"""

    def test_normalize_surface_rules(self):
        self.assertEqual(fish_surface.normalize_surface("P0\\Fish.py , p0/fish.py"),
                         ["p0/fish.py"], "反斜杠/大小写/重复项都要归一")
        self.assertEqual(fish_surface.normalize_surface(" a/b/ ， c//d ;; "),
                         ["a/b", "c/d"], "尾斜杠/中文标点/重复斜杠都要处理")
        self.assertEqual(fish_surface.normalize_surface(""), [])
        self.assertEqual(fish_surface.normalize_surface(None), [])
        self.assertEqual(fish_surface.normalize_surface("b,a"), ["a", "b"], "必须排序")

    def test_surface_hit_rules(self):
        self.assertEqual(fish_surface.surface_hit("p0", "p0/fish.py"), "p0")
        self.assertEqual(fish_surface.surface_hit("p0/fish.py", "p0"), "p0")
        self.assertEqual(fish_surface.surface_hit("a/b", "a/b"), "a/b")
        self.assertIsNone(fish_surface.surface_hit("a/b", "a/bc"),
                          "分量边界：a/b 不该命中 a/bc")
        self.assertIsNone(fish_surface.surface_hit("", "a"))

    def test_fish_reexports_are_same_objects(self):
        """`fish.surface_hit` 必须是同一份实现（不是复制一份）。"""
        self.assertIs(fish.surface_hit, fish_surface.surface_hit)
        self.assertIs(fish.normalize_surface, fish_surface.normalize_surface)


class PathPrimitivesTest(unittest.TestCase):
    """A2：路径归一只有一份规则（与 workspace_map 的存法同义）。"""

    def test_normalize_path(self):
        norm = fish_surface.normalize_path
        self.assertEqual(norm("I:\\App\\X\\"), "i:/app/x")
        self.assertEqual(norm("i:/app//x///y"), "i:/app/x/y")
        self.assertEqual(norm("a/b/../bc"), "a/bc", "`.`/`..` 要归一（否则误判前缀）")
        self.assertEqual(norm("C:"), "c:/", "盘符根保留最小形态，不塌成空串")
        self.assertEqual(norm("  "), "")
        self.assertEqual(norm(None), "")

    def test_relative_absolute_mix(self):
        self.assertFalse(fish_surface.is_absolute("p0/fish.py"))
        self.assertTrue(fish_surface.is_absolute("/tmp/x"))
        self.assertTrue(fish_surface.is_absolute("I:/app/x"))
        self.assertEqual(fish_surface.make_abs("p0/fish.py", "I:/proj"),
                         "i:/proj/p0/fish.py")
        self.assertEqual(fish_surface.make_abs("I:/x/y", "I:/proj"), "i:/x/y",
                         "已经是绝对路径就不该再拼基准")
        self.assertEqual(fish_surface.make_abs("p0/x.py", None), "p0/x.py",
                         "没有基准时**不猜** cwd")

    def test_relativize_and_dir_of(self):
        self.assertEqual(fish_surface.relativize("i:/proj/p0/fish.py", "i:/proj"),
                         "p0/fish.py")
        self.assertEqual(fish_surface.relativize("i:/other/a.py", "i:/proj"),
                         "i:/other/a.py", "不在基准之下就原样（不编相对路径）")
        self.assertEqual(fish_surface.dir_of("p0/fish.py"), "p0")
        self.assertEqual(fish_surface.dir_of("fish.py"), ".",
                         "根目录下的文件是 `.` 而不是空串（聚合键不能有空洞）")
        self.assertEqual(fish_surface.dir_of("i:/x/y.py"), "i:/x")


class PathHitTest(unittest.TestCase):
    """A3：绝对 vs 相对混用时的保守命中。生产 `presence.surface` 里真有绝对路径。"""

    def test_absolute_vs_relative_hits(self):
        self.assertEqual(
            fish_surface.path_hit("p0/fish.py", "i:/app/x/p0/fish.py"), "p0/fish.py")
        self.assertEqual(
            fish_surface.path_hit("i:/app/x/data/x.json", "data/x.json"), "data/x.json",
            "反过来也要命中（谁长谁短不重要）")
        self.assertIsNone(
            fish_surface.path_hit("i:/app/x/p0", "p0/fish.py"),
            "**目录 vs 未知 cwd 的相对文件** 不该猜：`i:/app/x/p0` 的后缀是 `x/p0`，"
            "而 `p0/fish.py` 的后缀是它自己——只靠词法无法证明它们是同一处")

    def test_min_segments_guard(self):
        """1 段的面到处同名：`fish.py` 不该命中 `i:/a/b/fish.py`。"""
        self.assertIsNone(fish_surface.path_hit("fish.py", "i:/a/b/fish.py"),
                          "1 段后缀不算命中（否则全机同名文件全撞车）")
        self.assertEqual(fish_surface.path_hit("data/x", "i:/a/data/x"), "data/x")

    def test_no_string_containment(self):
        self.assertIsNone(fish_surface.path_hit("fish.py", "i:/a/myfish.py"),
                          "按路径分量，不做字符串包含")
        self.assertIsNone(fish_surface.path_hit("a/b", "i:/x/a/bc"))

    def test_same_basis_unchanged(self):
        """同基准时结果与 `surface_hit` 完全一致（不引入第二套语义）。"""
        for a, b in (("p0", "p0/fish.py"), ("p0/x", "p0/y"), ("a/b", "a/bc")):
            self.assertEqual(fish_surface.path_hit(a, b), fish_surface.surface_hit(a, b))

    # ------------------------------------------------------------ 评审 L2
    #
    # 旧实现只做「短侧是长侧后缀」→ **相对目录面**（不带盘符）对更深的目标永远不命中：
    # `path_hit('p0/data', 'i:/…/p0/data/x.db') → None`。那不是"保守"，是静默漏判
    # （`find_collisions` 拿它判撞车，等于这一类的声明面根本不起作用）。

    def test_relative_directory_hits_deeper_target(self):
        """L2 正例：目录面命中它下面的目标（分量链包含）。"""
        self.assertEqual(fish_surface.path_hit("p0/data", "i:/app/x/p0/data/x.db"),
                         "p0/data")
        self.assertEqual(fish_surface.path_hit("p0/data", "i:/proj/p0/data"), "p0/data")
        self.assertIsNone(fish_surface.path_hit("p0", "i:/app/x/p0/data/x.db"),
                          "1 段相对目录仍被 min_segments 挡住（到处同名，误报比漏报更伤）")

    def test_relative_directory_component_boundary(self):
        """L2 反例：分量边界——`src/a` 不该命中 `…/src/ab/x`（字符串包含会误报）。"""
        self.assertIsNone(fish_surface.path_hit("src/a", "i:/x/src/ab/y"))
        self.assertIsNone(fish_surface.path_hit("p0/data", "i:/x/p0/database/x.db"))

    def test_file_form_keeps_suffix_semantics(self):
        """L2 边界：文件面保持后缀语义——一个文件不能"包含"更深的路径。"""
        self.assertTrue(fish_surface.looks_like_file("p0/fish.py"))
        self.assertFalse(fish_surface.looks_like_file("p0/data"))
        self.assertEqual(fish_surface.path_hit("p0/fish.py", "i:/app/x/p0/fish.py"),
                         "p0/fish.py")
        self.assertIsNone(
            fish_surface.path_hit("p0/fish.py", "i:/app/x/p0/fish.py/sub"),
            "文件面不做分量链包含（否则 `x.py/sub` 也会被当成同一处）")

    def test_version_dir_example_not_reversed(self):
        """评审 F7：`looks_like_file` 只看**最后一段**，旧注释的例子写反了。

        真正会退化成"文件面"的是 `data/v1.2`（最后一段 `v1.2` 含点），
        而 `v1.2/data` 的最后一段是 `data`、不带点——它仍是**目录面**，
        走分量链包含（旧注释说它"会被当文件面"，实测不成立）。
        """
        self.assertTrue(fish_surface.looks_like_file("data/v1.2"),
                        "最后一段带点的目录会被当文件面（这是已知代价）")
        self.assertFalse(fish_surface.looks_like_file("v1.2/data"),
                         "带点目录的**子目录**不受影响：只看最后一段")
        self.assertEqual(fish_surface.path_hit("v1.2/data", "i:/app/x/v1.2/data/x.db"),
                         "v1.2/data", "目录面（分量链）命中更深的目标")
        self.assertIsNone(fish_surface.path_hit("data/v1.2", "i:/app/x/data/v1.2/x.db"),
                          "文件面只做后缀匹配 → 漏判（不误判，与 min_segments 取向一致）")


class LongestPrefixTest(unittest.TestCase):
    """A4：workspace_map 解析（`fish.resolve_project_by_cwd` 也走它）。"""

    PAIRS = [("/a", "P1"), ("/a/b", "P2"), ("/a/bc", "P3")]

    def test_longest_wins(self):
        self.assertEqual(fish_surface.longest_prefix("/a/b/c/file.py", self.PAIRS),
                         ("/a/b", "P2"))

    def test_component_boundary(self):
        self.assertEqual(fish_surface.longest_prefix("/a/bc/file.py", self.PAIRS),
                         ("/a/bc", "P3"), "`/a/b` 不该覆盖 `/a/bc`（分量边界）")

    def test_no_match_returns_none(self):
        self.assertIsNone(fish_surface.longest_prefix("/z/x", self.PAIRS),
                          "没有匹配就 None，绝不回退到'猜一个'")
        self.assertIsNone(fish_surface.longest_prefix("", self.PAIRS))

    def test_fish_resolver_same_answer(self):
        """`fish.resolve_project_by_cwd` 与本函数给出同一个答案（行为不变）。

        ⚠️ 评审 F6：旧版把真库建在 `p0/_audit/_tmp_resolver/` 且**不清理**——
        测试跑一次就在源码树里留一个 300KB 的 `index.sqlite`，还会让下一次跑
        读到上一次的脏数据。现在用 `tempfile.mkdtemp` + `addCleanup`（进程崩了
        也不会留垃圾）。
        """
        tmp = Path(tempfile.mkdtemp(prefix="fish-resolver-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        conn = fish.connect(tmp / "data")
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            conn.execute("DELETE FROM workspace_map")
            for path, proj in self.PAIRS:
                fish.set_workspace_mapping(conn, path, proj)
            for target, want in (("/a/b/c/x.py", "P2"), ("/a/bc/x.py", "P3"),
                                 ("/a/x.py", "P1"), ("/zz/x.py", None)):
                self.assertEqual(fish.resolve_project_by_cwd(conn, target), want)
        finally:
            conn.close()


class NamingTest(unittest.TestCase):
    """A6：可读名（用户拍板「不要 ULID」）。"""

    def test_base_label_never_returns_id(self):
        self.assertEqual(fish_naming.base_label("anyi-studio-主AI", "dsh", "ABC"), "anyi-studio-主AI")
        self.assertEqual(fish_naming.base_label("", "dsh", "ABC"), "未命名·dsh")
        self.assertEqual(fish_naming.base_label("", "", "ABC"), "未命名")
        for label in (fish_naming.base_label("", "dsh", "0123456789ABCDEF"),
                      fish_naming.base_label("", "", "0123456789ABCDEF")):
            self.assertNotIn("0123456", label, "绝不把 citizen_id 片段当名字")

    def test_disambiguate_rules(self):
        rows = [
            {"citizen_id": "C1", "display_name": "甲", "host": "dsh", "created_at": "1"},
            {"citizen_id": "C2", "display_name": "甲", "host": "dsh", "created_at": "2"},
            {"citizen_id": "C3", "display_name": "甲", "host": "wb", "created_at": "3"},
            {"citizen_id": "C4", "display_name": "乙", "host": "dsh", "created_at": "4"},
        ]
        labels = fish_naming.disambiguate(rows)
        self.assertEqual(labels["C1"], "甲·dsh")
        self.assertEqual(labels["C2"], "甲·dsh#2")
        self.assertEqual(labels["C3"], "甲·wb")
        self.assertEqual(labels["C4"], "乙")

    def test_disambiguate_deterministic_without_created_at(self):
        rows = [{"citizen_id": "CB", "display_name": "甲", "host": "dsh"},
                {"citizen_id": "CA", "display_name": "甲", "host": "dsh"}]
        labels = fish_naming.disambiguate(rows)
        self.assertEqual(labels["CA"], "甲·dsh", "缺 created_at 时按 citizen_id 稳定排序")
        self.assertEqual(labels, fish_naming.disambiguate(list(reversed(rows))),
                         "输入顺序不该改变结果")

    def test_auto_name(self):
        self.assertTrue(fish_naming.is_auto_name("dsh-8a83d218"))
        self.assertTrue(fish_naming.is_auto_name("codebuddy-abc123:reviewer"))
        self.assertFalse(fish_naming.is_auto_name("anyi-studio-主AI"))
        self.assertFalse(fish_naming.is_auto_name(""))

    def test_project_label(self):
        self.assertEqual(fish_naming.project_label("大肥鱼生命系统"), "大肥鱼生命系统")
        self.assertEqual(fish_naming.project_label("大肥鱼生命系统", "anyi-studio"),
                         "大肥鱼生命系统（anyi-studio）")
        self.assertEqual(fish_naming.project_label("ANYI", "anyi"), "ANYI",
                         "namespace 与 scope_key 相同就不重复印")
        self.assertEqual(fish_naming.project_label(""), "（根）")


class MutationTest(unittest.TestCase):
    """A7/A8：判据必须能变红（拆掉关键条件 → 同一组断言必须失败）。"""

    def test_A7_drop_min_segments_goes_red(self):
        """拆掉「≥2 段」条件 → 「1 段面不命中」这条判据必须红。"""
        original = fish_surface.path_hit
        try:
            def loose(surface, target, *, min_segments=1):     # 变异：门槛降到 1
                return original(surface, target, min_segments=1)
            fish_surface.path_hit = loose
            self.assertIsNotNone(
                fish_surface.path_hit("fish.py", "i:/a/b/fish.py"),
                "变异后应当误命中——若仍为 None，说明 A3 的判据没在测门槛")
        finally:
            fish_surface.path_hit = original

    def test_A8_label_back_to_short_id_goes_red(self):
        """把 `base_label` 改回吐短 id → 「不含 ID」这条判据必须红。"""
        original = fish_naming.base_label
        try:
            fish_naming.base_label = lambda name, host, cid: name or (cid or "?")[:8]
            label = fish_naming.base_label("", "dsh", "0123456789ABCDEF")
            self.assertIn("0123456", label,
                          "变异后应当出现短 id——若没出现，说明 A6 没在测'不吐 ID'")
        finally:
            fish_naming.base_label = original


class InstallerTest(unittest.TestCase):
    """A9：hooks 安装器的三条硬校验（设计 T01 要点 4 / T05 要点 4）。

    为什么安装器也要判据：配置写错**不会报错**，只会"某个事件不再触发"——
    症状是"某个功能悄悄没了"。所以校验必须能红。
    """

    def setUp(self):
        import install_hooks
        self.ih = install_hooks
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-inst-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_two_hosts_four_events_and_one_collector(self):
        for host in ("dsh", "workbuddy"):
            hooks = self.ih.desired_hooks(host)
            self.assertEqual(set(hooks), set(self.ih.REQUIRED_EVENTS))
            self.assertEqual(len(self.ih.collectors(hooks)), 1,
                             f"{host} 必须只有一个采集器")
            self.assertEqual(self.ih.validate_hooks(host, hooks), [])

    def test_two_collectors_rejected(self):
        """两个采集器 = 每次工具调用跑两个进程、写两份行。校验必须挡住。"""
        hooks = self.ih.desired_hooks("dsh")
        extra = {"hooks": [{"type": "command",
                            "command": 'python "%s"' % (HERE / "fish_posttool_hook.py")}]}
        hooks["PostToolUse"].append(extra)
        problems = self.ih.validate_hooks("dsh", hooks)
        self.assertTrue(any("采集器数量" in p for p in problems), problems)

    def test_matcher_case_coverage(self):
        """只写 `Write|Edit` 而宿主工具名是小写 → 永远不触发（设计 §2.2 实测坑）。

        ⚠️ 评审 F1 更正：旧版这里把 `(?i)^(write|edit|multiedit)$` 钉成**合法**，
        而那一串在 DSH 的 JS RegExp 里直接 `Invalid group` → **整份配置被拒**
        （四个事件一个都不注册）。现在它以"形态非法"落地 → 必须 False。
        """
        self.assertFalse(self.ih.matcher_covers_write_tools(
            r"(?i)^(write|edit|multiedit)$"),
            "内联 flag 在宿主方言里非法（F1 的肇事串），不许再被判成合法")
        self.assertTrue(self.ih.matcher_covers_write_tools("Write|write|Edit|edit"))
        self.assertFalse(self.ih.matcher_covers_write_tools("Write|Edit"))
        self.assertFalse(self.ih.matcher_covers_write_tools(""))

    # ------------------------------------------------------------ 评审 F1
    #
    # `(?i)^(write|edit|…)$` 在 DSH 侧被当正则编译 → JS `Invalid group` →
    # `parseClaudeCodeConfig` 抛错 → `apply()` catch 后 return → **一个事件都不注册**，
    # 而 `--check` 只查"子串齐备"照样 exit 0。修法：合法性 = **形态**，且由
    # `--check` 契约校验兜住；生成侧改用纯字面备选（宿主根本不编译正则）。

    def test_matcher_dialect_contract(self):
        """唯一合法形态 = **纯字面备选**；一切正则形态（含锚定）都必须非法（N1）。

        Round 5/N1：旧判据还认"锚定正则"档，而那一档只查**字符集**、不查**可编译性**。
        `^(Write|write|Edit|edit$` 与 `^(Write|write|Edit|edit))$` 的字符集完全合法，
        但宿主 parser 会 `SyntaxError` → 整份配置被拒（和 F1 同一个失效模式）。
        """
        ih = self.ih
        # 合法：纯字面备选（宿主走字面分支，不编译正则）
        self.assertEqual(ih.matcher_dialect("Write|write|Edit|edit")[0], "literal")
        self.assertEqual(ih.matcher_dialect("write")[0], "literal")
        # 非法：空值只判空（原因里不必提"字面"）
        for empty in ("", None, "   "):
            kind, reason = ih.matcher_dialect(empty)
            self.assertEqual(kind, "", f"{empty!r} 必须判非法（{reason}）")
            self.assertTrue(reason, "非法时必须给出**原因**（--check 要打印它）")
        # 非法：锚定 / 括号 / 内联 flag / 元字符 / 前导尾随空白——一律拒绝
        for bad in (r"^(write|Write|edit|Edit)$", r"^(Write|write|Edit|edit$",
                    r"^(Write|write|Edit|edit))$", r"(?i)^(write|edit)$",
                    r"(?i:write)", r"^(write|edit)\b$", "writ.*",
                    "^(write)$|^x$", " Write|write", "Write|write ",
                    "Write | write", "write\n"):
            kind, reason = ih.matcher_dialect(bad)
            self.assertEqual(kind, "", f"{bad!r} 必须判非法（{reason}）")
            self.assertTrue(reason, "非法时必须给出**原因**（--check 要打印它）")
            self.assertIn("字面", reason, "原因要指向唯一合法形态（纯字面备选）")
        # 空白类给出更具体的原因：宿主按**原串**解释，不会替你 strip
        kind, reason = ih.matcher_dialect(" Write|write")
        self.assertEqual(kind, "")
        self.assertIn("空白", reason)
        # 正则形态的原因要告诉人「怎么改」——人工确认宿主方言后再手改配置
        kind, reason = ih.matcher_dialect(r"^(Write|write|Edit|edit)$")
        self.assertEqual(kind, "")
        self.assertIn("人工确认宿主方言", reason)

    def test_generated_matcher_is_literal_form(self):
        """生成侧只用字面备选：两宿主（含 WorkBuddy 的 PostToolUse）都过契约。"""
        ih = self.ih
        for host in ("dsh", "workbuddy"):
            hooks = ih.desired_hooks(host)
            for event, groups in hooks.items():
                for group in groups:
                    if "matcher" not in group:
                        continue
                    kind, reason = ih.matcher_dialect(group["matcher"])
                    self.assertEqual(kind, "literal",
                                     f"{host}/{event} 的 matcher 必须是字面备选：{reason}")
        self.assertEqual(ih.validate_hooks("dsh", ih.desired_hooks("dsh")), [],
                         "生成出来的配置必须自校验通过")

    def test_illegal_matcher_makes_check_red(self):
        """**变异自证（F1）**：把 matcher 换回 `(?i)` 形态 → `--check` 必须 exit 2。

        这里故意用**大小写都写全**的 `(?i)^(write|Write|edit|Edit)$`：
        「覆盖」这一门它是满足的，所以红只能来自**形态门**——判据不掺假。
        """
        target = self.tmp / "hooks.json"
        self.ih.apply_host("dsh", target)
        doc = json.loads(target.read_text(encoding="utf-8"))
        doc["hooks"]["PreToolUse"][0]["matcher"] = r"(?i)^(write|Write|edit|Edit)$"
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = self.ih.cmd_check(["dsh"], str(target))
        self.assertEqual(code, 2, "非法 matcher 必须让 --check 红（旧版恒绿 = F1）")
        self.assertIn("方言契约", buf.getvalue(), "要打印原因，不能只说'不一致'")

    def test_malformed_anchored_matcher_makes_check_red(self):
        """**变异自证（N1）**：字符集合法但括号不平衡的锚定串 → `--check` 必须红。

        旧判据只查字符集，对这一串恒绿；而宿主 parser 会 `SyntaxError` →
        整份配置被拒（0 事件注册）。这是 N1 的原始反例。

        ⚠️ 这里把 `desired_hooks` 钉成"与现状同一份"，排除"与目标不一致"这条红因
        （N3 也会 exit 2）——红只能来自**形态门**，判据不掺假。
        """
        target = self.tmp / "hooks.json"
        self.ih.apply_host("dsh", target)
        doc = json.loads(target.read_text(encoding="utf-8"))
        doc["hooks"]["PreToolUse"][0]["matcher"] = r"^(Write|write|Edit|edit$"
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="")
        buf = io.StringIO()
        with mock.patch.object(self.ih, "desired_hooks",
                               return_value=self.ih.hooks_of(doc)):
            with mock.patch("sys.stdout", buf):
                code = self.ih.cmd_check(["dsh"], str(target))
        text = buf.getvalue()
        self.assertEqual(code, 2, "括号不平衡的锚定串必须让 --check 红（N1）")
        self.assertIn("方言契约", text, "要打印原因")
        self.assertIn("请人工确认宿主方言", text,
                      "拒绝正则时要告诉人怎么改（人工确认方言后再手改）")

    def test_leading_space_matcher_makes_check_red(self):
        """N1：判据用**原串**——前导空格宿主不会替你 strip，字面比较永不命中。"""
        target = self.tmp / "hooks.json"
        self.ih.apply_host("dsh", target)
        doc = json.loads(target.read_text(encoding="utf-8"))
        doc["hooks"]["PreToolUse"][0]["matcher"] = (
            " " + self.ih.desired_hooks("dsh")["PreToolUse"][0]["matcher"])
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="")
        buf = io.StringIO()
        with mock.patch.object(self.ih, "desired_hooks",
                               return_value=self.ih.hooks_of(doc)):
            with mock.patch("sys.stdout", buf):
                code = self.ih.cmd_check(["dsh"], str(target))
        self.assertEqual(code, 2, "先 strip 再判 = 判据与宿主行为不是同一件事")
        self.assertIn("空白", buf.getvalue())

    def test_illegal_posttool_matcher_also_red(self):
        """WorkBuddy 的 `PostToolUse` 也挂 matcher——同样要过契约（扫全表，不只看 PreToolUse）。"""
        hooks = self.ih.desired_hooks("workbuddy")
        hooks["PostToolUse"][0]["matcher"] = r"(?i)^(read|bash)$"
        problems = self.ih.validate_hooks("workbuddy", hooks)
        self.assertTrue(any("PostToolUse" in p and "方言契约" in p for p in problems), problems)

    def test_apply_restore_roundtrip_and_preserves_other_keys(self):
        """`--apply` 只改 `hooks` 这一层；`--restore latest` 必须**字节还原**。"""
        target = self.tmp / "settings.json"
        original = json.dumps({"sandbox": {"mode": "off"}, "hooks": {"SessionStart": []}},
                              ensure_ascii=False, indent=2) + "\n"
        target.write_text(original, encoding="utf-8", newline="")
        before = target.read_bytes()
        res = self.ih.apply_host("workbuddy", target)
        self.assertTrue(res["written"])
        self.assertTrue(res["backup"])
        after = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(after["sandbox"], {"mode": "off"}, "别的键一个都不许动")
        self.assertEqual(set(after["hooks"]), set(self.ih.REQUIRED_EVENTS))
        self.assertNotEqual(target.read_bytes(), before)
        self.ih.restore_host(target, "latest")
        self.assertEqual(target.read_bytes(), before, "回滚必须字节一致（含行尾）")

    def test_check_exit_codes(self):
        """`--check` 的退出码是契约：一致 → 0，不一致 → 2（供脚本/CI 判断）。"""
        good = self.tmp / "hooks.json"
        self.ih.apply_host("dsh", good)
        # 捕获 stdout：`cmd_check` 会打 ✓/✗，而 unittest 的 stdout 在 GBK 控制台下
        # 会抛 UnicodeEncodeError（那是**测试环境**的限制，不是产品问题）
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(self.ih.cmd_check(["dsh"], str(good)), 0)
        # 删掉一个事件 → check 必须 exit 2
        doc = json.loads(good.read_text(encoding="utf-8"))
        doc["hooks"].pop("UserPromptSubmit")
        good.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="")
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(self.ih.cmd_check(["dsh"], str(good)), 2)

    def test_legal_but_different_config_exits_nonzero(self):
        """N3：**合法但与目标不一致**也必须 exit 2，且文案说「不一致」。

        旧版：diff 打 ✗「会被改写」，`problems` 为空 → exit 0、结论还说"都与目标一致"。
        这里给 matcher 多加一个 `read`（形态合法、覆盖门也过），红只能来自「不一致」。
        """
        target = self.tmp / "hooks.json"
        self.ih.apply_host("dsh", target)
        doc = json.loads(target.read_text(encoding="utf-8"))
        matcher = doc["hooks"]["PreToolUse"][0]["matcher"] + "|read"
        doc["hooks"]["PreToolUse"][0]["matcher"] = matcher
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="")
        self.assertTrue(self.ih.matcher_covers_write_tools(matcher),
                        "这份差异本身必须合法（红只能来自'不一致'）")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = self.ih.cmd_check(["dsh"], str(target))
        text = buf.getvalue()
        self.assertEqual(code, 2, "形态合法但内容不一致也必须 exit 2（N3）")
        self.assertIn("与目标不一致", text)
        self.assertIn("会被改写", text)
        self.assertNotIn("都与目标一致", text, "文案不许再说'一致'")

    def test_restore_latest_warns_when_backup_is_invalid(self):
        """N2：`--restore latest` 可能还原到坏版本——必须响亮告知并返回非 0。

        构造方式（真实时间序）：好配置 → 手改成坏 matcher → `--apply`（坏版本进备份）
        → `--restore latest` = 真的把坏版本还原回来。
        """
        target = self.tmp / "hooks.json"
        self.ih.apply_host("dsh", target)
        doc = json.loads(target.read_text(encoding="utf-8"))
        doc["hooks"]["PreToolUse"][0]["matcher"] = r"^(Write|write|Edit|edit$"
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="")
        self.ih.apply_host("dsh", target)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = self.ih.cmd_restore(["dsh"], "latest", str(target))
        text = buf.getvalue()
        self.assertNotEqual(code, 0, "还原到坏版本不许静默成功（N2）")
        self.assertIn("会被宿主整份拒绝", text)
        self.assertIn("请手工确认", text)

    def test_restore_latest_success_path_reports_contract_ok(self):
        """N2 正例：还原到**合法**备份 → 0，且明确说校验通过（不是只说"已还原"）。"""
        target = self.tmp / "hooks.json"
        self.ih.apply_host("dsh", target)
        doc = json.loads(target.read_text(encoding="utf-8"))
        # 合法但非目标的改动（多覆盖一个 read）→ 下一次 apply 会备份这份合法配置
        doc["hooks"]["PreToolUse"][0]["matcher"] += "|read"
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="")
        self.ih.apply_host("dsh", target)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = self.ih.cmd_restore(["dsh"], "latest", str(target))
        text = buf.getvalue()
        self.assertEqual(code, 0, f"合法备份不该被拦：{text}")
        self.assertIn("校验通过", text)
        self.assertIn("重启宿主", text)

    # ------------------------------------------------------------ 评审 L4
    #
    # `settings.json` 是**共用文件**：别的插件/别的工具也往同一个 `hooks` 层挂事件。
    # 旧实现 `document["hooks"] = desired` 整层替换 → 静默删掉 `Stop`/`Notification`，
    # 而 `--check` 还写着"其它事件保持不变"——文案与实际不符，人根本发现不了。

    OTHERS = {
        "Stop": [{"hooks": [{"type": "command", "command": "python other_stop.py"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "python notify.py"}]}],
    }

    def test_apply_merges_and_keeps_other_events(self):
        """缺省合并：其它事件与其它顶层键一个字节不动，`--check` 文案与实际一致。"""
        target = self.tmp / "settings.json"
        payload = {"sandbox": {"mode": "off", "plugin": {"x": 1}},
                   "hooks": dict(self.OTHERS, SessionStart=[])}
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="")
        before = target.read_bytes()
        res = self.ih.apply_host("workbuddy", target)
        self.assertTrue(res["written"])
        self.assertEqual(res["dropped"], [], "缺省模式不许删任何事件")
        self.assertEqual(res["preserved"], ["Notification", "Stop"])
        after = json.loads(target.read_text(encoding="utf-8"))
        for event, groups in self.OTHERS.items():
            self.assertEqual(after["hooks"][event], groups, f"{event} 必须原样保留")
        self.assertEqual(after["sandbox"], payload["sandbox"], "其它顶层键也不许动")
        self.assertEqual(set(after["hooks"]) - set(self.ih.REQUIRED_EVENTS),
                         set(self.OTHERS), "只允许多出别人的事件，不许少")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = self.ih.cmd_check(["workbuddy"], str(target))
        text = out.getvalue()
        self.assertEqual(code, 0, f"--check 应通过（四个事件已就位）：{text}")
        self.assertIn("其它事件保持不变", text)
        self.assertIn("Stop", text)
        self.assertNotIn("将被**删除**", text, "非 force 时不许出现'删除'字样")
        self.ih.restore_host(target, "latest")
        self.assertEqual(target.read_bytes(), before, "回滚仍要字节级还原")

    def test_force_replaces_and_lists_dropped_events(self):
        """`--force` 才整层替换，且必须把**将删除的事件**列出来（不许悄悄删）。"""
        target = self.tmp / "hooks.json"
        target.write_text(json.dumps({"hooks": dict(self.OTHERS)}, ensure_ascii=False,
                                     indent=2) + "\n", encoding="utf-8", newline="")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = self.ih.cmd_check(["dsh"], str(target), force=True)
        text = out.getvalue()
        self.assertEqual(code, 2, "缺四个事件时 check 仍应 exit 2")
        self.assertIn("将被**删除**", text, f"force 下必须提示会删事件：{text}")
        self.assertIn("Stop", text)
        res = self.ih.apply_host("dsh", target, force=True)
        self.assertEqual(res["dropped"], ["Notification", "Stop"])
        after = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(set(after["hooks"]), set(self.ih.REQUIRED_EVENTS),
                         "--force 才允许出现'只剩四个事件'的结果")

    def test_bare_event_table_shape_kept(self):
        """裸事件表形态（顶层就是事件名）也要合并，不许凭空多出一层 `hooks`。"""
        target = self.tmp / "bare.json"
        target.write_text(json.dumps({"Stop": [{"hooks": []}]}, indent=2) + "\n",
                          encoding="utf-8", newline="")
        res = self.ih.apply_host("dsh", target)
        self.assertEqual(res["shape"], "bare")
        doc = json.loads(target.read_text(encoding="utf-8"))
        self.assertNotIn("hooks", doc, "裸形态不许被改成 {hooks:{…}}")
        self.assertIn("Stop", doc, "别人的事件仍在")
        self.assertEqual(set(doc) - {"Stop"}, set(self.ih.REQUIRED_EVENTS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
