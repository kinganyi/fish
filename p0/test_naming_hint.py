#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命名：中文占位名 + 自动名可识别 + 起名引导（2026-11-22 · 用户三条需求）。

## 背景：一个 100% 漏判的死机制

用户要求「给自动成员的 AI 加提示让它们改名」。查下去发现**提示早就有**——
`fish_board._unnamed_hint` 会输出一段"去起名"的可执行提示，而且接进了注入包。

但它**从来没触发过**。判据是：

    if name and not fish_naming.is_auto_name(name): return ""

而 `is_auto_name` 用正则 `^(dsh|codebuddy|workbuddy|cli)-[0-9a-z]{6,12}` 认名字，
生成器 `auto_session_display_name` 产出的是 `dsh-session-88f9` —— **尾巴里有 `-`**，
不在 `[0-9a-z]` 里，匹配不上。

实测：库里 **173 个**由生成器造出来的名字，**`is_auto_name` 认出来 0 个（100% 漏判）**。
于是机制在、接线在，只有判定坏了，等于死代码。

根因是**生成器与检测器分居两个文件**：生成器在 `fish.py`，检测器在 `fish_naming.py`，
靠"照着印象写正则"维持同步。所以修法不只是改正则——**把生成器搬到检测器旁边**，
让它能直接算一遍做精确比对。

## 三条需求 → 七条判据

    需求①  自动成员要有改名提示        → N5（提示真的出）+ N2（认得出才谈得上提示）
    需求②  起名时给中文名引导          → N6（回执里有）+ N7（文案唯一一份）
    需求③  自动名不要英文              → N1（占位名是中文）

外加两条守边界的：
    N3  旧格式的历史名字仍被认出来（老库不因为改格式而失忆）
    N4  人工起的名字不被误判（不能把 `czhy-开发-1` 当自动名劝它改名）
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import fish_board    # noqa: E402
import fish_naming   # noqa: E402

SK = "dsh:session-88f9b6a5-65c5-49f5-8b65-c0ea381f96c3"
HOST = "dsh"


class AutoNameTest(unittest.TestCase):
    """需求③：自动名要中文 + 可识别。"""

    # ------------------------------------------------------------------ N1
    def test_N1_placeholder_name_is_chinese(self):
        """自动占位名必须是中文前缀 `未命名·`，不是 `dsh-session-88f9` 这种英文形状。

        变异：把 `UNNAMED_PREFIX` 改回 `""`（或把生成器改回 `<host>-<tail[:12]>`）
        → 本条变红。
        """
        got = fish_naming.auto_session_display_name(HOST, SK)
        self.assertTrue(got.startswith(fish_naming.UNNAMED_PREFIX),
                        f"占位名不是中文前缀：{got!r}")
        self.assertIn("未命名", got)
        # 仍保留可追溯信息（host + 短码），否则换成纯中文就丢了追溯
        self.assertIn("dsh", got)
        self.assertIn("#", got)

    def test_N1c_generator_always_uses_the_prefix(self):
        """生成器的输出**必须**带 `未命名·` 前缀 —— 这是前缀判定的前提。

        为什么需要这条：`is_auto_name` 认新格式靠前缀。如果哪天有人改了生成器的
        格式（比如去掉前缀），前缀判定就会**静默失效**——而那正是这次 bug 的
        形状：**生成器改了形状，检测器没跟上，两边各自都对，合起来 100% 漏判**。

        这条把两者**焊在一起**：改生成器格式，它立刻红，逼人同时看检测器。

        变异：M6（生成器退回英文 `<host>-<tail[:12]>`）→ 本条与 N1 一起红。
        """
        for host, sk in ((HOST, SK), ("codebuddy", "codebuddy:abc123456789"),
                         ("", "")):
            for role in (None, "reviewer"):
                got = fish_naming.auto_session_display_name(host, sk, role)
                self.assertTrue(
                    got.startswith(fish_naming.UNNAMED_PREFIX),
                    f"生成器产出 {got!r} 不带前缀 —— 前缀判定会静默失效")

    def test_N1b_placeholder_without_session_key(self):
        """没有会话键时退回 `未命名·<host>`，不炸、不给空串。"""
        got = fish_naming.auto_session_display_name(HOST, "")
        self.assertTrue(got.startswith(fish_naming.UNNAMED_PREFIX))
        self.assertIn("dsh", got)

    # ------------------------------------------------------------------ N2
    def test_N2_detector_recognises_generator_output(self):
        """**根因判据**：生成器产出的名字，检测器必须认出来。

        这是那条 100% 漏判的直接探针。改格式之前，本判据在真实库上的命中率是 0/173。

        变异：把 `is_auto_name` 里的"精确比对"与"前缀判定"都删掉 → 只剩旧正则
        → 本条变红（新格式认不出）。
        """
        for host, sk in (("dsh", SK),
                         ("codebuddy", "codebuddy:c48fd810-f235-49df-a306-ae6df53d4542"),
                         ("workbuddy", "workbuddy:00433b7e-413c-41c0-b88c-0574ef41e5f9")):
            name = fish_naming.auto_session_display_name(host, sk)
            self.assertTrue(
                fish_naming.is_auto_name(name, host=host, session_key=sk),
                f"生成器产出 {name!r}，检测器不认")

    def test_N2b_detector_works_without_context_via_prefix(self):
        """只给名字（没上下文）也要认得出新格式 —— 靠 `未命名·` 前缀。

        变异：删掉 `is_auto_name` 里的前缀分支 → 本条变红。
        """
        self.assertTrue(fish_naming.is_auto_name("未命名·dsh#88f9"))
        self.assertTrue(fish_naming.is_auto_name("未命名·codebuddy#c48f:reviewer"))

    # ------------------------------------------------------------------ N3
    def test_N3_legacy_names_still_recognised(self):
        """**老库不能因为改格式而失忆**：旧格式的历史名字仍要被认出来。

        旧格式 = `<host>-<会话码前 12 位>`（`dsh-session-88f9`），尾巴含 `-`，
        单靠旧正则 `[0-9a-z]{6,12}` 是匹配不上的 —— 必须靠上下文做精确比对。

        变异：删掉 `is_auto_name` 里那段旧格式精确比对 → 本条变红。
        """
        self.assertTrue(
            fish_naming.is_auto_name("dsh-session-88f9", host=HOST, session_key=SK),
            "旧格式的历史名字没被认出来（老库会静默失忆）")
        # 旧正则覆盖的那批（纯字母数字尾巴，无上下文）也必须还认
        self.assertTrue(fish_naming.is_auto_name("dsh-8a83d218"))
        self.assertTrue(fish_naming.is_auto_name("codebuddy-abc123:reviewer"))

    # ------------------------------------------------------------------ N4
    def test_N4_human_names_are_not_flagged(self):
        """人工起的名字**不许**被当成自动名（否则会反复劝人改名）。

        变异：把 `is_auto_name` 改成"以已知宿主开头的都算" → 本条变红。
        """
        for name, host, sk in (
                ("czhy-开发-1", "codebuddy", "codebuddy:abc123456789"),
                ("fish-主AI-3", "dsh", SK),
                ("anyi-studio-主AI", "workbuddy", "workbuddy:xyz987654321"),
                ("漫剧-剪辑", "dsh", SK)):
            self.assertFalse(
                fish_naming.is_auto_name(name, host=host, session_key=sk),
                f"人工名 {name!r} 被误判成自动名")
        self.assertFalse(fish_naming.is_auto_name(""))
        self.assertFalse(fish_naming.is_auto_name(None))


class RenameHintTest(unittest.TestCase):
    """需求①：自动成员要真的收到改名提示。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()

    def _add(self, cid, name, host, sk):
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, host, host_session_key,"
            " kind, status, created_at, is_owner) VALUES (?,?,?,?,?,?,?,'0')",
            (cid, name, host, sk, "role", "active", fish.now_utc()))
        self.conn.commit()

    # ------------------------------------------------------------------ N5
    def test_N5_hint_fires_for_auto_named_citizen(self):
        """自动命名的公民 → `_unnamed_hint` 必须输出提示。

        **这是"死机制"的直接探针**：改之前它对 173 个自动名全部返回空串。

        变异：把 `_unnamed_hint` 里传给 `is_auto_name` 的 `host` / `session_key`
        去掉 → 旧格式那条会退回正则、认不出 → 本条变红。
        """
        cid = "01NAMINGAUTO000000000001"
        self._add(cid, "dsh-session-88f9", HOST, SK)
        hint = fish_board._unnamed_hint(self.conn, cid)
        self.assertTrue(hint, "自动命名的公民没有收到改名提示（机制又死了）")
        self.assertIn("rename_citizen", hint, "提示必须是可执行的（给出具体调用）")
        self.assertIn("中文名", hint, "提示必须说清要起中文名")

    def test_N5b_hint_is_silent_for_human_named_citizen(self):
        """人工命名的公民 → 不输出提示（不能每轮都劝人改名）。

        变异：把 `_unnamed_hint` 的判定改成恒真 → 本条变红。
        """
        cid = "01NAMINGHUMAN00000000001"
        self._add(cid, "czhy-开发-1", "codebuddy", "codebuddy:abc123456789")
        self.assertEqual(fish_board._unnamed_hint(self.conn, cid), "")

    def test_N5c_hint_explains_why_it_matters(self):
        """提示要讲清"为什么值得改"——用户要求让 AI 有收获感，不只是一句命令。

        变异：把文案里"为什么值得改"那段删掉 → 本条变红。
        """
        cid = "01NAMINGWHY0000000000001"
        self._add(cid, "未命名·dsh#88f9", HOST, SK)
        hint = fish_board._unnamed_hint(self.conn, cid)
        self.assertIn("为什么值得改", hint)
        self.assertIn("看一眼", hint, "要给出具体收益，不是空话")


class GuidanceSingleSourceTest(unittest.TestCase):
    """需求②：起名引导要出现在两处，且**只能有一份文案**。"""

    # ------------------------------------------------------------------ N6
    def test_N6_session_start_receipt_carries_the_guidance(self):
        """`session start` 回执对自动名必须带起名引导。

        变异：把 `_print_session_start` 里那段 `if is_auto_name(...)` 删掉 → 本条变红。
        """
        import contextlib
        import io
        root = Path(tempfile.mkdtemp()) / "data"
        conn = fish.connect(root)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        try:
            # 走真实路径：名字由 `start_session` 算好再传进来（这个函数不负责取名）
            auto = fish.auto_session_display_name(HOST, SK)
            res = fish.find_or_create_session_citizen(
                conn, session_key=SK, evidence="machine", host=HOST,
                display_name=auto, kind="visitor")
            # 组一个够 `_print_session_start` 用的最小结果
            payload = {
                "token": "fsh_test", "citizen_id": res["citizen_id"],
                "session_key": SK, "evidence": "machine", "projects": [],
                "channel": "cli", "host": HOST,
                "display_name": res["display_name"],
                "account_id": None, "issued_at": fish.now_utc(),
                "expires_at": None, "created": True, "rotated": 0,
            }
            self.assertTrue(fish_naming.is_auto_name(
                payload["display_name"], host=HOST, session_key=SK),
                "前置：新注册的公民应当是自动占位名")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                fish._print_session_start(payload)
            out = buf.getvalue()
            self.assertIn("rename_citizen", out, "回执里没有起名引导")
            self.assertIn("中文名", out, "回执里没说要起中文名")
        finally:
            conn.close()

    # ------------------------------------------------------------------ N7
    def test_N7_guidance_lives_in_exactly_one_place(self):
        """引导文案只有一个源头：`fish_naming.rename_guidance()`。

        两处各自复制一份是最难被抓住的漂移（两处都对，只是说得不一样）。

        变异：在 `fish_board` 里再内联一份文案字面量 → 本条变红。
        """
        g = fish_naming.rename_guidance()
        self.assertIn("中文名", g)
        self.assertIn("为什么值得改", g)
        # 两个调用点都必须引用同一个函数，而不是各自写死
        import inspect
        for mod in (fish_board, fish):
            src = inspect.getsource(mod)
            self.assertIn("rename_guidance", src,
                          f"{mod.__name__} 没有引用共用文案")
        # 反向守：文案里的特征句不该在别处被复制成字面量
        literal_dup = sum(
            1 for mod in (fish_board, fish)
            if inspect.getsource(mod).count("如果你的用户没给你起名") > 1)
        self.assertEqual(literal_dup, 0,
                         "有人把引导文案复制成字面量了，应该调 rename_guidance()")


if __name__ == "__main__":
    unittest.main(verbosity=2)
