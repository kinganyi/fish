#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W6 判据：压缩接缝 `deliver()` + 三条不变量的边界守卫。

## 为什么 W6 是"留接缝"而不是"接压缩器"

2026-11-18 实测（headroom 0.44.0，独立 venv）：

    JSON 元数据 ×60（kb_asset 形态）  14,160 → 8,038 字符   省 43.2%
    中文 markdown（fish 记忆形态）     1,620 → 1,620 字符    省  0.0%
    Python 源码                       7,660 → 7,660 字符    省  0.0%

fish 自己的负载几乎无收益。所以默认关闭、不拉生产依赖，只留注入点。

## 这批判据真正守的是什么

不是"压缩能省多少"，是**压缩不许越界**：

  **I1 原文不可变**   —— `deliver` 不写库、不改存储
  **I2 写路径不压缩** —— `remember` / `kb_write` 里不许出现压缩
  **I4 身份不可压**   —— 身份/画像/铁律/编码命中即原样返回

后三条用 grep 守。这类越界**不会让任何东西变红**——压缩悄悄作用在画像上，
包看起来更短了，而没人知道人格设定被改过。
"""
import inspect
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish          # noqa: E402
import fish_kb       # noqa: E402
import fish_profile  # noqa: E402
import fish_dataplane           # noqa: E402
import fish_dataplane as DP     # noqa: E402


def _conn():
    import sqlite3
    import fish_schema
    c = sqlite3.connect(":memory:")
    c.executescript(fish_schema.SCHEMA)
    return c


class DeliverTest(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        DP.set_compressor(None)

    def tearDown(self):
        DP.set_compressor(None)

    def test_default_off_returns_text_unchanged(self):
        """默认关闭：原样返回，但回执仍要诚实（original_len / saved / handle 齐全）。"""
        text = "这是一段记忆正文。" * 50
        d = DP.deliver(self.conn, text, kind="prose", source="ANYI-261022-10")
        self.assertEqual(d.text, text, "默认关闭却改了文本")
        self.assertEqual(d["original_len"], len(text))
        self.assertEqual(d["saved"], 0)
        self.assertEqual(d["compressor"], "none")
        self.assertTrue(d["handle"], "可逆性契约：handle 不能为空")

    def test_protected_kinds_are_never_touched(self):
        """**I4**：身份/画像/铁律/编码命中即原样返回——连尝试都不尝试。

        用"一定会被调用到"的假压缩器证明它确实没被调用（不是碰巧没变）。
        """
        calls = []

        def fake(t, kind=None):
            calls.append(kind)
            return "压过了"

        DP.set_compressor(fake)
        self.conn.execute("INSERT OR REPLACE INTO config (key,value,type,updated_at)"
                          " VALUES ('dataplane.compress_enabled','1','int','x')")
        self.conn.commit()
        for kind in ("identity", "profile", "rule", "code"):
            d = DP.deliver(self.conn, "画像：名字=徐安", kind=kind)
            self.assertEqual(d.text, "画像：名字=徐安", f"{kind} 被压了")
            self.assertTrue(d["compressor"].startswith("skipped:"),
                            f"{kind} 的 compressor 标记不对：{d['compressor']}")
        self.assertEqual(calls, [], f"保护类型仍调用了压缩器：{calls}")

    def test_compressor_error_falls_back_to_original(self):
        """压缩失败必须回退原文——绝不"压一半"。"""
        def boom(t, kind=None):
            raise RuntimeError("坏了")

        DP.set_compressor(boom)
        self.conn.execute("INSERT OR REPLACE INTO config (key,value,type,updated_at)"
                          " VALUES ('dataplane.compress_enabled','1','int','x')")
        self.conn.commit()
        d = DP.deliver(self.conn, "原文内容", kind="prose")
        self.assertEqual(d.text, "原文内容")
        self.assertIn("error:", d["compressor"])

    def test_no_win_result_is_rejected(self):
        """压完不比原来短 → 不要（这类"压缩"只浪费预算）。"""
        DP.set_compressor(lambda t, kind=None: t + "变长了")
        self.conn.execute("INSERT OR REPLACE INTO config (key,value,type,updated_at)"
                          " VALUES ('dataplane.compress_enabled','1','int','x')")
        self.conn.commit()
        d = DP.deliver(self.conn, "原文", kind="prose")
        self.assertEqual(d.text, "原文")
        self.assertEqual(d["compressor"], "no_win")

    def test_successful_compression_reports_saved_and_handle(self):
        """压缩成功时，`saved` 与 `handle` 都要有——否则读者不知道少了什么、去哪拿。"""
        DP.set_compressor(lambda t, kind=None: t[:len(t) // 2])
        self.conn.execute("INSERT OR REPLACE INTO config (key,value,type,updated_at)"
                          " VALUES ('dataplane.compress_enabled','1','int','x')")
        self.conn.commit()
        d = DP.deliver(self.conn, "x" * 100, kind="json", handle_hint="`kb open a.json`")
        self.assertEqual(len(d.text), 50)
        self.assertEqual(d["saved"], 50)
        self.assertEqual(d["handle"], "`kb open a.json`")


class InvariantGrepTest(unittest.TestCase):
    """**I1 / I2 / I4 的边界守卫**——这类越界不会让别的东西变红。"""

    def _src(self, fn):
        try:
            return inspect.getsource(fn)
        except (OSError, TypeError):
            return ""

    def test_write_paths_never_compress(self):
        """**I2**：写路径上不许出现压缩。

        写进去的东西必须是原文；压了就是数据损坏，而且事后无法区分
        "原文如此"与"压过"。
        """
        targets = [
            ("fish_kb.kb_write", fish_kb.kb_write),
            ("fish_kb._kb_write_impl", getattr(fish_kb, "_kb_write_impl", None)),
            ("fish.record_memory", getattr(fish, "record_memory", None)),
        ]
        offenders = []
        for name, fn in targets:
            if fn is None:
                continue
            src = self._src(fn)
            for token in ("compress", "deliver("):
                if token in src:
                    offenders.append(f"{name} 含 {token!r}")
        self.assertEqual(offenders, [], f"写路径被污染：{offenders}")

    def test_identity_renderer_never_compresses(self):
        """**I4**：画像/身份渲染器不许碰压缩器。"""
        offenders = []
        for name, fn in (("fish_profile.render_profile_block",
                          fish_profile.render_profile_block),
                         ("fish_profile.render_questions_block",
                          fish_profile.render_questions_block)):
            src = self._src(fn)
            if "compress" in src or "deliver(" in src:
                offenders.append(name)
        self.assertEqual(offenders, [], f"身份渲染器被污染：{offenders}")

    def test_compressor_is_injectable_and_not_a_hard_dependency(self):
        """**设计意图**：headroom 装在独立 venv，不许成为 fish 的生产依赖。

        所以 `fish_dataplane` 里只能有**注入点**，不能有 `import headroom`。
        """
        src = Path(fish_dataplane.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import headroom", src,
                         "headroom 被拉成硬依赖了——它在独立 venv 里")
        self.assertIn("def set_compressor", src, "注入点没了")


    def test_deliver_seam_is_declared(self):
        """**F-P2-4**：`deliver` 没有生产调用方——这件事必须**写在代码里**。

        审查方给的选项是"删除或接真实调用+handle 判据"。核实后：
        **8 处测试、0 处生产调用**。删掉会删掉已验证的代码，接上要先有压缩器。

        所以取中间那条：**登记例外**。项目的 I3 原则不许空接缝——
        例外可以存在，但必须是**被登记的**例外，不能是"没人注意到它空着"。

        这条判据守的是"登记"本身：docstring 里必须写着它没有生产调用方，
        以及让它活起来需要什么。**判据不管它有没有被调用**（那是产品决定），
        只管**读代码的人会不会被误导**。
        """
        import fish_dataplane as D
        doc = D.deliver.__doc__ or ""
        self.assertIn("没有生产调用方", doc,
                      "deliver 的 docstring 没写明它没有生产调用方——"
                      "读代码的人会以为它在用")
        self.assertIn("set_compressor", doc,
                      "没写让它活起来需要什么（压缩器注入）")
        # 原文被换行与加粗拆开了，查短的片段即可（长串在 docstring 里会跨行）
        self.assertIn("0 处生产调用", doc,
                      "没给出核实后的数量——那句话是这条登记的凭据")


if __name__ == "__main__":
    unittest.main(verbosity=2)
