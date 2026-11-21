#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆↔提交的关联层（fish_git_link）+ 归属判定入口（2026-11-20）。

## 这份测试补的是什么

`git_link` 的四个函数此前**一条判据都没有**——上一轮我在临时脚本里手工验证过
行为，但那不留下任何会自动变红的东西。手工验证过 ≠ 有判据；下次改动没人拦得住。

`set_ownership` 同理：`ownership` 字段建了很久，但没有任何代码会写它。
现在补上入口，也补上判据。

## 变异体（去掉 → 必红）

· `link_remove` 改成物理删除（`DELETE FROM`）→ 软删除判据红（行没了就没法追溯）
· `link_restore` 不实现 → 回滚判据红
· `set_ownership` 去掉值校验 → 非法值判据红
· `set_ownership` 不写 `ownership_evidence` → 证据判据红
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402
import fish_git_link  # noqa: E402
import fish_git_store  # noqa: E402


class _DbCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-gitlink-"))
        # 2026-11-21：把 data 目录也存下来——`record_memory` 需要它，
        # 而「记忆正文落在哪」正是 `code_index.file_path` 的基准。
        self.data = self.tmp / "db"
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


class LinkSoftDeleteTest(_DbCase):
    """J10：关联的软删除可回滚。"""

    def test_create_then_query(self):
        lid = fish_git_link.link_create(
            self.conn, src_kind="memory", src_id="ANYI-261120-01",
            dst_kind="commit", dst_id="abc123", rel="implemented_by",
            evidence="手工关联")
        self.conn.commit()
        rows = fish_git_link.links_of(self.conn, src_id="ANYI-261120-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], lid)
        self.assertEqual(rows[0]["rel"], "implemented_by")

    def test_remove_is_soft_not_physical(self):
        """**J10 的核心**：软删之后行必须还在（否则「撤销」无从追溯）。

        变异体：把 `UPDATE ... SET removed_at` 改成 `DELETE FROM` → 本判据红。
        """
        lid = fish_git_link.link_create(
            self.conn, src_kind="memory", src_id="m1",
            dst_kind="commit", dst_id="c1", rel="implemented_by")
        self.conn.commit()
        self.assertTrue(fish_git_link.link_remove(self.conn, lid), "软删没命中")
        self.conn.commit()

        self.assertEqual(fish_git_link.links_of(self.conn, src_id="m1"), [],
                         "默认查询不该返回已删的")
        kept = fish_git_link.links_of(self.conn, src_id="m1", include_removed=True)
        self.assertEqual(len(kept), 1, "行被物理删掉了——撤销已无从追溯")
        self.assertIsNotNone(kept[0]["removed_at"], "removed_at 没记时间戳")

    def test_restore_rolls_back(self):
        lid = fish_git_link.link_create(
            self.conn, src_kind="memory", src_id="m2",
            dst_kind="commit", dst_id="c2", rel="produced")
        self.conn.commit()
        fish_git_link.link_remove(self.conn, lid)
        self.conn.commit()
        self.assertTrue(fish_git_link.link_restore(self.conn, lid), "回滚没命中")
        self.conn.commit()
        rows = fish_git_link.links_of(self.conn, src_id="m2")
        self.assertEqual(len(rows), 1, "回滚后应当重新可见")
        self.assertIsNone(rows[0]["removed_at"])

    def test_duplicate_create_returns_same_id(self):
        """重复建立返回已有 id —— 调用方不需要「先查再建」（并发下会变两行）。"""
        a = fish_git_link.link_create(self.conn, src_kind="memory", src_id="m3",
                                      dst_kind="commit", dst_id="c3", rel="revised_in")
        b = fish_git_link.link_create(self.conn, src_kind="memory", src_id="m3",
                                      dst_kind="commit", dst_id="c3", rel="revised_in")
        self.conn.commit()
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM git_link").fetchone()[0], 1)

    def test_create_after_remove_resurrects(self):
        """被软删后再建立一次 = 恢复（语义是「它又成立了」）。"""
        lid = fish_git_link.link_create(self.conn, src_kind="memory", src_id="m4",
                                        dst_kind="commit", dst_id="c4",
                                        rel="discussed_in")
        self.conn.commit()
        fish_git_link.link_remove(self.conn, lid)
        self.conn.commit()
        again = fish_git_link.link_create(self.conn, src_kind="memory", src_id="m4",
                                          dst_kind="commit", dst_id="c4",
                                          rel="discussed_in")
        self.conn.commit()
        self.assertEqual(again, lid)
        self.assertEqual(len(fish_git_link.links_of(self.conn, src_id="m4")), 1,
                         "重新建立后应当可见")

    def test_query_by_dst(self):
        """反方向也要能查（「这个提交实现了哪些记忆」）。"""
        fish_git_link.link_create(self.conn, src_kind="memory", src_id="m5",
                                  dst_kind="commit", dst_id="c5", rel="implemented_by")
        fish_git_link.link_create(self.conn, src_kind="memory", src_id="m6",
                                  dst_kind="commit", dst_id="c5", rel="produced")
        self.conn.commit()
        rows = fish_git_link.links_of(self.conn, dst_kind="commit", dst_id="c5")
        self.assertEqual(len(rows), 2)


class OwnershipTest(_DbCase):
    """`ownership` 的显式赋值入口。"""

    def _mk_repo(self):
        rid = fish_git_store.ensure_repo(
            self.conn, identity="github.com/o/r", identity_kind="remote",
            root=str(self.tmp / "r"))
        self.conn.commit()
        return rid

    def test_default_is_unknown(self):
        """**保守默认**：新登记的仓库必须是 unknown，不能是 owned。

        变异体：把 DDL 默认改成 'owned' → 本判据红。这条守着方案 P10
        （发现器一跑，第三方仓库全被标成「自己的」→ 只读红线形同虚设）。
        """
        rid = self._mk_repo()
        row = self.conn.execute(
            "SELECT ownership, ownership_evidence FROM git_repo WHERE repo_id=?",
            (rid,)).fetchone()
        self.assertEqual(row[0], "unknown")
        self.assertIsNone(row[1])

    def test_set_ownership_with_evidence(self):
        rid = self._mk_repo()
        self.assertTrue(fish_git_store.set_ownership(
            self.conn, rid, "owned", evidence="远端 owner=kinganyi 是主人 GitHub 账号"))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT ownership, ownership_evidence FROM git_repo WHERE repo_id=?",
            (rid,)).fetchone()
        self.assertEqual(row[0], "owned")
        self.assertIn("kinganyi", row[1], "ownership_evidence 没写进去")

    def test_rejects_invalid_value(self):
        """非法值必须**响亮失败**，不能静默存进去。

        变异体：去掉值校验 → 本判据红。（CHECK 约束也会拦，但那会抛 sqlite3 错误，
        对调用方不友好；这里要求 ValueError 带可用的取值表。）
        """
        rid = self._mk_repo()
        with self.assertRaises(ValueError):
            fish_git_store.set_ownership(self.conn, rid, "mine")

    def test_missing_repo_returns_false(self):
        self.assertFalse(fish_git_store.set_ownership(self.conn, 999999, "owned"))


class MentionLinkTest(_DbCase):
    """2026-11-21（用户裁定「接上」）：`link_commit_mentions` = 唯一的零猜测自动关联。

    ## 为什么只接这一种

    `rel` 的四种关系里，`implemented_by` / `produced` / `revised_in` 都要**语义判断**
    （这条记忆描述的工作是不是被这个提交做完了？），机械推断必然错。
    只有 `discussed_in` 可以零猜测：**提交 message 里写没写这个编码，是确定的事实。**

    ## 变异体（去掉 → 必红）

    · 去掉 `collect_git` 里对 `link_commit_mentions` 的调用 → `test_wired_into_collect_git` 红
    · 去掉 `_drop_shadowed` → `test_prefix_shadowed` 红
    · `status IN ('active','superseded')` 改成只查 active → 作废记忆的关联判据红
    · 编码匹配从"扫库内编码"改成"用正则切文本" → `test_no_false_prefix` 红
    """

    def _mk_memory(self, title, code_prefix="T"):
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix, namespace, status) "
            "VALUES (?,?, 'active')", (code_prefix, code_prefix.lower()))
        self.conn.commit()
        code, _rel = fish.record_memory(self.conn, self.data, project=code_prefix,
                                        title=title, body="x")
        self.conn.commit()
        return code

    def test_mention_creates_link(self):
        """提到编码 → 建立 `discussed_in` 关联。"""
        code = self._mk_memory("被提到的")
        res = fish_git_link.link_commit_mentions(
            self.conn, sha="deadbeef", text=f"实现 {code} 描述的功能")
        self.conn.commit()
        self.assertIn(code, res["linked"], f"没建立关联：{res}")
        rows = fish_git_link.links_of(self.conn, src_id=code)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rel"], "discussed_in")
        self.assertEqual(rows[0]["source"], "inferred")

    def test_no_mention_no_link(self):
        """没提到 → 不建立（**负对照**，没有它等于"给所有记忆都挂关联"也能过）。"""
        a = self._mk_memory("甲")
        b = self._mk_memory("乙")
        fish_git_link.link_commit_mentions(self.conn, sha="c1", text=f"实现 {a} 的功能")
        self.conn.commit()
        self.assertTrue(fish_git_link.links_of(self.conn, src_id=a))
        self.assertFalse(fish_git_link.links_of(self.conn, src_id=b), "没提到的也被关联了")

    def test_prefix_shadowed(self):
        """`T-261121-0` 与 `T-261121-01` 同时存在时，只有长的被关联。

        变异体：去掉 `_drop_shadowed` → 短编码也被挂上 → 红。
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix, namespace, status) "
            "VALUES ('T','t','active')")
        long_code = "T-261121-011"
        short_code = "T-261121-01"
        for c in (long_code, short_code):
            self.conn.execute(
                "INSERT OR IGNORE INTO code_index (code, title, project, mem_type, "
                "status, file_path) VALUES (?,?,?,?,?,?)",
                (c, "t", "T", "fact", "active", f"projects/T/{c}.md"))
        self.conn.commit()
        res = fish_git_link.link_commit_mentions(
            self.conn, sha="c2", text=f"修了 {long_code}")
        self.conn.commit()
        self.assertIn(long_code, res["linked"])
        self.assertNotIn(short_code, res["linked"],
                         "短编码被长编码遮蔽了却仍建立关联——会给错的记忆挂边")

    def test_retired_memory_still_linkable(self):
        """已作废（withdrawn）的记忆也要能挂关联：提交可能正是在修它。"""
        code = self._mk_memory("被作废的")
        self.conn.execute("UPDATE code_index SET status='withdrawn' WHERE code=?", (code,))
        self.conn.commit()
        res = fish_git_link.link_commit_mentions(self.conn, sha="c3", text=f"清理 {code}")
        self.conn.commit()
        self.assertIn(code, res["linked"])

    def test_idempotent(self):
        """重复调用不重复建行（`link_create` 幂等 + UNIQUE 约束）。"""
        code = self._mk_memory("幂等")
        for _ in range(3):
            fish_git_link.link_commit_mentions(self.conn, sha="c4", text=f"见 {code}")
        self.conn.commit()
        self.assertEqual(len(fish_git_link.links_of(self.conn, src_id=code)), 1)

    def test_wired_into_collect_git(self):
        """**接线判据**：`collect_git` 采到提交后必须真的调它。

        这是 `fish_git_link` 从「有实现无调用」变成「已接线」的唯一证明。
        变异体：删掉 `fish_git_hook.py` 里对 `link_commit_mentions` 的那次调用 → 红。
        """
        import os
        import subprocess
        import fish_git_hook
        code = self._mk_memory("接线")
        repo = self.tmp / "repo"
        repo.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        (repo / "a.txt").write_text("hi", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", f"实现 {code}"],
                       cwd=repo, check=True, env=env)
        res = fish_git_hook.collect_git(
            self.conn, cwd=str(repo), session_key="dsh:test",
            tool_name="Bash", tool_input={"command": "git commit"},
            is_commit_cmd=lambda _c: True, dbg=lambda _m: None)
        self.assertIsNone(res["error"], f"collect_git 报错了：{res['error']}")
        self.assertIn("linked", res, "collect_git 没有调用关联层——能力又变成死代码了")
        self.conn.commit()
        self.assertTrue(fish_git_link.links_of(self.conn, src_id=code),
                        f"端到端没建立关联：{res}")


class GitLinkToolTest(_DbCase):
    """`git_link` MCP 工具：显式建立那三种「不能自动推断」的关系。

    ## 为什么需要这个工具

    `fish_git_link` 原本 4 个函数、21 条判据、**生产侧 0 个调用点**。
    `link_commit_mentions` 补上了 `discussed_in` 的自动推断，但另外三种
    （`implemented_by` / `produced` / `revised_in`）**只能由人断言**——
    而「只能显式建立」若无入口，就等于「建不了」。

    ## 变异体（去掉 → 必红）

    · 删掉 `git_link` 的 `action` 分支 → 对应判据红
    · 去掉「提交必须已采集」的前置检查 → `test_rejects_unknown_sha` 红
    · 去掉「discussed_in 不许手动建」的守卫 → 自动与显式会造两条意义不同的边
    """

    def setUp(self):
        super().setUp()
        import mcp_server
        self.mcp = mcp_server
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix, namespace, status) "
            "VALUES ('T','t','active')")
        self.conn.commit()
        self.code, _ = fish.record_memory(self.conn, self.data, project="T",
                                          title="测试", body="x")
        TS = "2026-11-20T00:00:00Z"
        self.conn.execute(
            "INSERT INTO git_repo (repo_id, identity, identity_kind, identity_source, "
            "display_name, ownership, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (1, "t", "remote", "inferred", "t", "unknown", TS, TS))
        self.conn.execute(
            "INSERT INTO git_commit (sha, repo_id, authored_at, committed_at, is_merge, "
            "subject) VALUES (?,?,?,?,?,?)",
            (self.SHA, 1, TS, TS, 0, "某个提交"))
        self.conn.commit()
        self.srv = object.__new__(mcp_server.FishServer)
        self.srv.conn = self.conn
        self.srv.root = self.data

        class _Id:
            status = "valid"
            citizen_id = "C1"
            evidence = "machine"
            session_key = "dsh:t"

        self.ident = _Id()

    SHA = "abc123def4567890"

    def _call(self, **kw):
        return self.srv.tool_git_link(kw, self.ident)

    def test_link_then_list(self):
        """建立 implemented_by 并按编码查回来。"""
        out = self._call(action="link", code=self.code, sha=self.SHA,
                         rel="implemented_by", evidence="断言")
        self.assertIn("已建立关联", out, out)
        rows = fish_git_link.links_of(self.conn, src_id=self.code)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rel"], "implemented_by")
        self.assertEqual(rows[0]["source"], "explicit")
        self.assertIn(self.code, self._call(action="list", code=self.code))

    def test_rejects_unknown_sha(self):
        """未采集的提交不许建关联（否则边指向不存在的对象）。

        变异体：去掉 `git_commit` 存在性检查 → 红。
        """
        out = self._call(action="link", code=self.code, sha="deadbeef",
                         rel="produced")
        self.assertIn("还没采集到", out, out)
        self.assertFalse(fish_git_link.links_of(self.conn, src_id=self.code))

    def test_rejects_bad_rel(self):
        out = self._call(action="link", code=self.code, sha=self.SHA, rel="nonsense")
        self.assertIn("rel 只能是", out, out)

    def test_rejects_manual_discussed_in(self):
        """`discussed_in` 是自动推断的，不许手动断言（否则同一关系两条来源不同口径的边）。"""
        out = self._call(action="link", code=self.code, sha=self.SHA,
                         rel="discussed_in")
        self.assertIn("自动推断", out, out)
        self.assertFalse(fish_git_link.links_of(self.conn, src_id=self.code))

    def test_list_needs_direction(self):
        """list 不给方向 → 明说该给什么，而不是返回全库 dump。"""
        out = self._call(action="list")
        self.assertIn("至少要给一个方向", out, out)

    def test_unlink_is_soft(self):
        """撤销是软删：行还在（「建立过又被否掉」和「从没建立过」不是一回事）。"""
        self._call(action="link", code=self.code, sha=self.SHA, rel="produced")
        lid = fish_git_link.links_of(self.conn, src_id=self.code)[0]["id"]
        out = self._call(action="unlink", link_id=lid)
        self.assertIn("已撤销", out, out)
        self.assertEqual(len(fish_git_link.links_of(self.conn, src_id=self.code)), 0)
        self.assertEqual(
            len(fish_git_link.links_of(self.conn, src_id=self.code,
                                       include_removed=True)), 1)


if __name__ == "__main__":
    unittest.main()
