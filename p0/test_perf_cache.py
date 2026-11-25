#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-4 判据 · 性能缓存（2026-11-26 外部审查 W2-b）。

## 判据清单

  P1  `GET /api/hosts` 连续两次（TTL 内）只探测一次
  P2  不同 host 过滤参数不共用缓存槽（key 含过滤值）
  P3  `POST /api/hosts/<id>/refresh` 清 `hosts` 档 → 下一次列表重探
  P4  `GET /api/push/overview` 连续两次只调一次数据面；写配置后失效
  P5  `_CACHE` 有上限（塞 300 个 key → 长度 ≤ CACHE_MAX_ENTRIES，且最旧被淘汰）
  P6  MCP `corpus search` 默认 `count=False`，显式 `count=true` 才统计
  P7  CLI（面板的文本面）显式 `count=True`（要显示「命中 N 条」）

每条都有对应变异体（`mutation_perf_validity.py`）：删 TTL / 去掉 status join /
缓存不淘汰 → 本文件判据必须红。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish_corpus  # noqa: E402
import fish_workbench_api as wb  # noqa: E402
import mcp_server  # noqa: E402


def _host_item(host_id="dsh", *, installed=True):
    """最小 HostStatus 形状：`api_hosts` 只读 lastSeenAt，`api_host` 读 configs/lastSeenSources。"""
    return {
        "id": host_id,
        "display": host_id,
        "installed": installed,
        "lastSeenAt": "2026-11-26T00:00:00Z",
        "configs": [],
        "lastSeenSources": [],
    }


class CacheBase(unittest.TestCase):
    def setUp(self):
        wb._CACHE.clear()

    def tearDown(self):
        wb._CACHE.clear()


class HostsCacheTest(CacheBase):
    def test_p1_two_reads_probe_once_within_ttl(self):
        # 先锁口径：2–5 秒（需求写的区间）；下面用 1 秒前的读数证明它确实还在 TTL 内。
        self.assertGreaterEqual(wb.CACHE_TTL_S["hosts"], 2.0)
        self.assertLessEqual(wb.CACHE_TTL_S["hosts"], 5.0)
        calls = []

        def fake_installed(*, db_path=None):
            calls.append(db_path)
            return [_host_item()]

        with mock.patch.object(wb.fish_hosts, "installed_statuses", fake_installed), \
                mock.patch.object(wb.fish_hosts, "consistency",
                                  lambda: {"ok": True, "warnings": [], "checked": {}}):
            first = wb.api_hosts("C:/tmp/fish-perf-none.db")
            # 把首读时间改成 1 秒前：正常 3 秒 TTL 下仍是命中；若 TTL 被删成 0 则必重探。
            key = (os.path.abspath("C:/tmp/fish-perf-none.db"), "")
            ts, value = wb._CACHE[("hosts", key)]
            wb._CACHE[("hosts", key)] = (time.time() - 1.0, value)
            second = wb.api_hosts("C:/tmp/fish-perf-none.db")

        self.assertEqual(len(calls), 1, "TTL 内第二次 /api/hosts 又探测了一次")
        self.assertEqual(first["data"]["items"][0]["id"], "dsh")
        self.assertEqual(second["data"]["items"][0]["id"], "dsh")

    def test_p2_host_filter_has_its_own_cache_key(self):
        calls = []

        def fake_installed(*, db_path=None):
            calls.append(db_path)
            return [_host_item("dsh"), _host_item("workbuddy")]

        with mock.patch.object(wb.fish_hosts, "installed_statuses", fake_installed), \
                mock.patch.object(wb.fish_hosts, "consistency",
                                  lambda: {"ok": True, "warnings": [], "checked": {}}):
            all_items = wb.api_hosts("C:/tmp/fish-perf-none.db")
            only_dsh = wb.api_hosts("C:/tmp/fish-perf-none.db", "dsh")
            only_dsh_again = wb.api_hosts("C:/tmp/fish-perf-none.db", "dsh")

        self.assertEqual(len(all_items["data"]["items"]), 2)
        self.assertEqual([x["id"] for x in only_dsh["data"]["items"]], ["dsh"])
        self.assertEqual(only_dsh_again["data"]["items"], only_dsh["data"]["items"])
        self.assertEqual(len(calls), 2, "host 过滤参数没有进缓存 key（复用了全量槽）")

    def test_p3_refresh_invalidates_hosts_cache(self):
        installed_calls = []
        detect_calls = []

        def fake_installed(*, db_path=None):
            installed_calls.append(db_path)
            return [_host_item()]

        def fake_detect(host_id, *, db_path=None):
            detect_calls.append(host_id)
            return _host_item(host_id)

        with mock.patch.object(wb.fish_hosts, "installed_statuses", fake_installed), \
                mock.patch.object(wb.fish_hosts, "consistency",
                                  lambda: {"ok": True, "warnings": [], "checked": {}}), \
                mock.patch.object(wb.fish_hosts, "detect", fake_detect):
            wb.api_hosts("C:/tmp/fish-perf-none.db")                 # 1 次探测
            wb.api_hosts("C:/tmp/fish-perf-none.db")                 # 缓存命中
            wb.api_host("C:/tmp/fish-perf-none.db", "dsh", refresh=True)  # 清 hosts 档
            wb.api_hosts("C:/tmp/fish-perf-none.db")                 # 重探

        self.assertEqual(len(installed_calls), 2, "refresh 后列表没有重新探测")
        self.assertEqual(len(detect_calls), 1)

    def test_p1b_expired_entry_probes_again(self):
        calls = []

        def fake_installed(*, db_path=None):
            calls.append(db_path)
            return [_host_item()]

        with mock.patch.object(wb.fish_hosts, "installed_statuses", fake_installed), \
                mock.patch.object(wb.fish_hosts, "consistency",
                                  lambda: {"ok": True, "warnings": [], "checked": {}}):
            wb.api_hosts("C:/tmp/fish-perf-none.db")
            key = (os.path.abspath("C:/tmp/fish-perf-none.db"), "")
            ts, value = wb._CACHE[("hosts", key)]
            wb._CACHE[("hosts", key)] = (ts - wb.CACHE_TTL_S["hosts"] - 1, value)
            wb.api_hosts("C:/tmp/fish-perf-none.db")

        self.assertEqual(len(calls), 2, "过期条目没有触发重探（TTL 判定失效）")


class PushOverviewCacheTest(CacheBase):
    def test_p4_two_reads_once_then_write_invalidates(self):
        self.assertGreaterEqual(wb.CACHE_TTL_S["push_overview"], 10.0)
        overview_calls = []

        def fake_overview(db_path, *, days):
            overview_calls.append(days)
            return {"totals": {"total": 1}, "asOf": "2026-11-26T00:00:00Z", "sources": []}

        def fake_write(db_path, key, value):
            return {"key": key, "old": 0, "new": value, "changed": True,
                    "auditAt": "2026-11-26T00:00:01Z"}

        with mock.patch.object(wb.fish_push_audit, "overview", fake_overview), \
                mock.patch.object(wb.fish_push_audit, "write_config", fake_write):
            first = wb.api_push_overview("C:/tmp/fish-perf-none.db", 7)
            # 5 秒前：正常 10 秒 TTL 下命中；TTL 被删成 0 则必重算。
            key = (os.path.abspath("C:/tmp/fish-perf-none.db"), 7)
            ts, value = wb._CACHE[("push_overview", key)]
            wb._CACHE[("push_overview", key)] = (time.time() - 5.0, value)
            second = wb.api_push_overview("C:/tmp/fish-perf-none.db", 7)
            wb.api_push_write_config("C:/tmp/fish-perf-none.db",
                                     "push.open_budget_tokens", 600)
            third = wb.api_push_overview("C:/tmp/fish-perf-none.db", 7)

        self.assertEqual(len(overview_calls), 2,
                         "10 秒 TTL 内重复调用或写后未失效（期望：两次调用）")
        self.assertEqual(first["data"]["totals"]["total"], 1)
        self.assertEqual(second["data"]["totals"]["total"], 1)
        self.assertEqual(third["data"]["totals"]["total"], 1)

    def test_p4b_preset_invalidates_too(self):
        overview_calls = []

        def fake_overview(db_path, *, days):
            overview_calls.append(days)
            return {"totals": {"total": 1}, "asOf": "2026-11-26T00:00:00Z", "sources": []}

        def fake_preset(db_path, preset):
            return {"preset": preset, "changedCount": 1,
                    "auditAt": "2026-11-26T00:00:01Z"}

        with mock.patch.object(wb.fish_push_audit, "overview", fake_overview), \
                mock.patch.object(wb.fish_push_audit, "apply_preset", fake_preset):
            wb.api_push_overview("C:/tmp/fish-perf-none.db", 7)
            wb.api_push_overview("C:/tmp/fish-perf-none.db", 7)
            wb.api_push_apply_preset("C:/tmp/fish-perf-none.db", "pause")
            wb.api_push_overview("C:/tmp/fish-perf-none.db", 7)

        self.assertEqual(len(overview_calls), 2, "preset 写后 overview 缓存未失效")


class CacheBoundTest(CacheBase):
    def test_p5_cache_is_bounded_and_evicts_oldest(self):
        for index in range(wb.CACHE_MAX_ENTRIES + 44):
            wb.cache_put("facets", (index,), {"index": index})
        self.assertLessEqual(len(wb._CACHE), wb.CACHE_MAX_ENTRIES,
                             "_CACHE 没有上限：长跑进程会无限积累")
        self.assertIsNone(wb.cache_get("facets", (0,)),
                          "最旧条目没有被淘汰（淘汰顺序不是最旧）")
        newest = wb.cache_get("facets", (wb.CACHE_MAX_ENTRIES + 43,))
        self.assertEqual(newest["index"], wb.CACHE_MAX_ENTRIES + 43)


class McpCountDefaultTest(unittest.TestCase):
    """P2-4：MCP 交互面默认 count=False；显式 true 才统计。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-mcp-count-")
        self.server = mcp_server.FishServer(Path(self.tmp) / "data")
        self.ident = mock.Mock()
        self.ident.citizen_id = "01CIT_TEST_COUNT"
        self.ident.session_key = "dsh:mcp-count-test"
        self.ident.status = "valid"

    def tearDown(self):
        self.server.conn.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _fake_search(*args, **kwargs):
        return {"hits": [], "meta": {"total_matched": None, "returned": 0,
                                     "ranker": "default", "truncated": False,
                                     "degraded": []}}

    def test_p6_default_count_is_false(self):
        with mock.patch.object(mcp_server.fish_corpus, "search",
                               side_effect=self._fake_search) as mocked:
            self.server.tool_corpus({"action": "search", "query": "x"}, self.ident)
        self.assertIs(mocked.call_args.kwargs.get("count"), False,
                      "MCP corpus search 默认没有传 count=False（全库 COUNT 仍在付）")

    def test_p6b_explicit_count_true(self):
        with mock.patch.object(mcp_server.fish_corpus, "search",
                               side_effect=self._fake_search) as mocked:
            self.server.tool_corpus({"action": "search", "query": "x", "count": True},
                                    self.ident)
        self.assertIs(mocked.call_args.kwargs.get("count"), True)

    def test_p6c_schema_declares_default_false(self):
        tool = [t for t in mcp_server.TOOLS if t["name"] == "corpus"][0]
        prop = tool["inputSchema"]["properties"]["count"]
        self.assertIs(prop.get("default"), False)
        self.assertEqual(prop.get("type"), "boolean")


class CliCountExplicitTest(unittest.TestCase):
    def test_p7_cli_asks_for_exact_total(self):
        fake = {"hits": [], "meta": {"total_matched": 0, "returned": 0,
                                     "ranker": "default", "truncated": False,
                                     "degraded": []}}
        with mock.patch.object(fish_corpus, "search", return_value=fake) as mocked:
            rc, lines, _ = fish_corpus.run_cli("search", root="C:/tmp/none", target="x")
        self.assertEqual(rc, 0)
        self.assertIs(mocked.call_args.kwargs.get("count"), True,
                      "CLI/面板不再显式 count=True（会显示 total=None）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
