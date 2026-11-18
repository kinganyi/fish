#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立评审 🔴-1 判据：**有凭证 ≠ 有授权**。

## 评审官实测的问题（我自己复核成立）

`enforce_mem_type_auth` 只问 `has_credential`——"你有有效 token 吗"。
但 MCP 的 `session_start` 对任何调用方**无门槛**签发 visitor 公民 + token。
完整链路：

    session_start(kind="visitor") -> token -> remember(mem_type="rule")
    -> 成功 -> 该规矩进所有人的注入包，且无 untrusted 标记

生产库实测：**71 条 rule 里 10 条作者是 kind=visitor 的公民**。

## 我原来的推断错在哪

F-P0-3 里我查了"rule 71 条 / 未归属 0"，据此说"凭证门有效"。
**错在把「有归属」当成「有授权」**：一条 rule 可以既有明确作者、又不该被允许写。
归属回答"是谁写的"，授权回答"他凭什么能写"——这是两个问题。

## 这批判据为什么能失败

把授权门删掉，第一条就红。而它红的时候，陌生人已经能给所有人写 AI 行为约束了。
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_trust  # noqa: E402


class RuleAuthorizationTest(unittest.TestCase):

    def test_visitor_cannot_write_rule(self):
        """**核心**：visitor 即使有有效凭证，也不许写 rule/identity。"""
        for kind in ("rule", "identity"):
            with self.assertRaises(ValueError, msg=f"{kind} 没被拦") as ctx:
                fish_trust.enforce_mem_type_auth(
                    kind, has_credential=True, citizen_kind="visitor")
            msg = str(ctx.exception)
            self.assertIn("被授予", msg, f"报错没说清是授权问题：{msg[:160]}")
            self.assertIn("resident", msg, "报错应指出怎么获得授权")

    def test_resident_and_role_can_write_rule(self):
        """对照组：resident / role 必须能写——否则"一律拒绝"也能让上面变绿。"""
        for kind in ("resident", "role"):
            fish_trust.enforce_mem_type_auth(
                "rule", has_credential=True, citizen_kind=kind)

    def test_unknown_kind_is_refused(self):
        """判不到 kind 时**不放行**——严格优先，因为放行的代价是"陌生人能改 AI 行为"。"""
        with self.assertRaises(ValueError):
            fish_trust.enforce_mem_type_auth(
                "rule", has_credential=True, citizen_kind=None)

    def test_no_credential_still_refused(self):
        """无凭证本来就该拒——这条保证授权门没把旧的门挤掉。"""
        with self.assertRaises(ValueError):
            fish_trust.enforce_mem_type_auth(
                "rule", has_credential=False, citizen_kind="resident")

    def test_free_types_unaffected(self):
        """fact / lesson 等免凭证类型不受影响——授权门只管约束类。"""
        for kind in ("fact", "episode", "lesson", "artifact"):
            fish_trust.enforce_mem_type_auth(kind, has_credential=False)
            fish_trust.enforce_mem_type_auth(kind, has_credential=True,
                                             citizen_kind="visitor")

    def test_citizen_kind_of_does_not_guess(self):
        """`citizen_kind_of` 查不到就返回 None，**不猜**。

        "查不到就当 resident" 正是这条缺陷的成因形态：把判不到的事实当成有利结论。
        """
        tmp = tempfile.mkdtemp(prefix="fish-trust-")
        root = Path(tmp) / "data"
        root.mkdir(parents=True)
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            self.assertIsNone(fish_trust.citizen_kind_of(conn, "C-NOBODY"))
            self.assertIsNone(fish_trust.citizen_kind_of(conn, None))
            self.assertIsNone(fish_trust.citizen_kind_of(conn, ""))
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                "created_at) VALUES (?,?,?,?,?,?,?)",
                ("C-V", "访客", "visitor", None, None, "active", fish.now_utc()))
            conn.commit()
            self.assertEqual(fish_trust.citizen_kind_of(conn, "C-V"), "visitor")
        finally:
            conn.close()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_authorized_kinds_are_the_documented_ones(self):
        """授权档位是**明确枚举**的，不是一个宽泛的正则或"非 visitor"——"""
        self.assertEqual(tuple(fish_trust.AUTHORIZED_CITIZEN_KINDS),
                         ("resident", "role"))


class McpKindBypassTest(unittest.TestCase):
    """**第二轮评审 🔴-1 的根**：MCP 面不许自封 resident/role。

    上一版修复只加了"必须 resident/role"这道门，而 `kind` 是**调用方入参**——
    评审官一行绕过：

        session_start(session_name="attacker", kind="resident")
        -> 建出 resident 公民 + token -> remember(mem_type="rule") 成功

    **一把钥匙在攻击者手里的门，不是门。**
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-mcpkind-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        c = fish.connect(self.root)
        c.executescript(fish.SCHEMA)
        fish.migrate(c)
        c.commit()
        c.close()
        import mcp_server
        self.m = mcp_server
        self.srv = mcp_server.FishServer(self.root)
        self.anon = mcp_server.IDENTITY_ANONYMOUS

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_mcp_cannot_self_declare_resident(self):
        """**核心**：`session_start(kind="resident")` 必须被拒。"""
        with self.assertRaises(ValueError) as ctx:
            self.srv.tool_session_start(
                {"session_name": "attacker", "kind": "resident"}, self.anon)
        msg = str(ctx.exception)
        self.assertIn("不能自封", msg)
        self.assertIn("set-kind", msg, "报错要指出获得授权的正当路径（主人本机执行）")

    def test_register_cannot_self_declare_resident(self):
        with self.assertRaises(ValueError) as ctx:
            self.srv.tool_register(
                {"display_name": "attacker", "kind": "resident"}, self.anon)
        self.assertIn("不能自封", str(ctx.exception))

    def test_visitor_registration_still_works(self):
        """对照组：正常 visitor 登记必须能成功——否则"全拒"也能让上面变绿。"""
        res = self.srv.tool_session_start(
            {"session_name": "ok-visitor", "kind": "visitor"}, self.anon)
        self.assertIsNotNone(res)

    def test_schemas_do_not_advertise_resident(self):
        """结构判据：**身份相关工具**的 `kind` 枚举里不许出现 resident / role。

        收窄代码但不收窄 schema，等于"文档说你能自封，代码拒绝你"——
        调用方会以为是自己写错了，而不是这条路本不该存在。

        ## 第三轮评审：这条原来也是假的

        原实现拿正则扫 `mcp_server.py` 源码文本，只认单行
        `"kind": {"type": "string", "enum": [`。两种失效方式：
          ① 把 enum 改成多行，正则就看不见了；
          ② 失靶守卫是 `assertTrue(enums)`——而 **board 工具也有一个 kind 枚举**
             （`["progress","done","note"]`），它永远存在，守卫永远满足。
        评审官实测：把两个身份工具的 enum 改多行并加回 resident/role → **判据绿**。

        现在直接从 `mcp_server.TOOLS` 这个**真实数据结构**里读，不碰文本；
        失靶守卫盯的是**具体工具名**，board 的 kind 满足不了它。
        """
        import mcp_server as _m
        wanted = ("register", "session_start")
        found = {}
        for t in _m.TOOLS:
            props = (t.get("inputSchema") or {}).get("properties") or {}
            if "kind" in props:
                found[t["name"]] = props["kind"].get("enum") or []
        # 失靶守卫：按**名字**断言，不接受"只要有任何 kind 枚举就行"
        missing = [w for w in wanted if w not in found]
        self.assertEqual(missing, [],
                         f"这些工具没有 kind 参数（判据失去靶点）：{missing}；"
                         f"实际带 kind 的工具：{sorted(found)}")
        for name in wanted:
            enum = found[name]
            self.assertNotIn("resident", enum,
                             f"{name} 的 schema 仍宣传 resident：{enum}")
            self.assertNotIn("role", enum,
                             f"{name} 的 schema 仍宣传 role：{enum}")
            self.assertIn("visitor", enum,
                          f"{name} 的 schema 连 visitor 都没有：{enum}")


    def test_visitor_token_cannot_write_rule_end_to_end(self):
        """**端到端**：拿到 visitor token 后写 rule，必须被拒。

        这条盯的是**调用点接线**——评审官实测：把两个调用点的
        `citizen_kind=` 写死成 `"resident"`，原有 16 条判据全绿。
        那种情况下门形同虚设，却没有任何判据报出来。这条补上那个洞。
        """
        import fish_trust
        c = self.srv.conn
        # 造一个 visitor 公民 + token，然后走 MCP 的 remember
        res = self.srv.tool_session_start(
            {"session_name": "v-attacker", "kind": "visitor"}, self.anon)
        text = str(res)
        import re as _re
        m = _re.search(r"(fsh_[A-Za-z0-9_\-]{8,})", text)
        self.assertIsNotNone(m, f"没从回执里拿到 token：{text[:200]}")
        token = m.group(1)
        ident = self.srv._resolve_identity(token)
        self.assertEqual(ident.status, "valid", "visitor token 居然无效")
        with self.assertRaises(ValueError) as ctx:
            self.srv.tool_remember(
                {"project": "ATK", "title": "恶意规矩", "mem_type": "rule"}, ident)
        self.assertIn("被授予", str(ctx.exception),
                      f"visitor 写 rule 没被授权门拦住：{str(ctx.exception)[:150]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
