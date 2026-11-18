#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P1-3 判据：附件入口的四道校验。

## 审查方指出的问题（已复核成立，原实现逐条对上）

```python
data = src.read_bytes()                 # ① 没有大小上限
h = hashlib.sha256(data).hexdigest()    # ④ 不是流式（整份进内存）
ext = src.suffix.lower()                # 扩展名直接信任
bpath.write_bytes(data)                 # 写一半断电 → 目录里留个 sha256 对不上的 blob
```

② 没有敏感名单：`--attach ~/.ssh/id_rsa` 把私钥按**内容寻址**复制进 `blobs/`。
   内容寻址意味着**改名也挡不住**——sha256 一样就还是同一份。
③ 没有位置拦截：机器上任何可读路径都能被吸进来，包括系统目录与凭据目录。

## 这批判据为什么能失败

每一条都对着一个**具体缺失**：把大小检查删掉、把敏感名单清空、把流式换回
`read_bytes`，对应的判据就红。特别是"流式"那条——它读的是一个比内存分块大的
文件，断言 sha256 与独立算出的值一致；换回整份读也**能算对**，所以它单独不足以
证明流式——所以另有一条判据盯 `.tmp` 目录必须被清空（整份读的版本根本不会建它）。
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish            # noqa: E402
import fish_attach as A  # noqa: E402


def _conn():
    tmp = tempfile.mkdtemp(prefix="fish-p13-db-")
    data = Path(tmp) / "data"
    data.mkdir(parents=True)
    conn = fish.connect(data)
    conn.executescript(fish.SCHEMA)
    fish.migrate(conn)
    return conn


class AttachmentGuardTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-p13-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.conn = _conn()
        # record_memory 要分配编码，必须先在 code_registry 登记前缀
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq)"
            " VALUES ('P','P','2026-11-18',0)")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _cap(self, mb):
        """把上限临时改成 mb。

        为什么不走配置：本判据要验的是**校验逻辑**，不是配置管道。
        混进配置就读不到时会让人分不清"上限没生效"和"配置没写对"——
        实测就踩了一次（写进 DB 但 `get_config` 仍回默认值）。
        """
        import fish_attach as _A
        self._old_cap = _A._attach_max_bytes
        _A._attach_max_bytes = lambda conn=None: mb * 1024 * 1024
        self.addCleanup(lambda: setattr(_A, "_attach_max_bytes", self._old_cap))

    def _mk(self, name="a.txt", content=b"hello"):
        p = Path(self._tmp) / "src" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    # ------------------------------------------------------------- ① 大小
    def test_oversized_attachment_is_rejected(self):
        """**核心**：超过上限必须拒绝，且**不许先把它读完**。

        上限调到 1 MB，造一个 3 MB 的文件——判定必须在 stat 之后就发生。
        """
        self._cap(1)
        big = self._mk("big.bin", b"x" * (3 * 1024 * 1024))
        with self.assertRaises(A.AttachmentRejected) as ctx:
            A.store_blob(self.root, big, conn=self.conn)
        self.assertEqual(ctx.exception.code, "attach_too_large")
        self.assertIn("上限", str(ctx.exception))

    def test_size_limit_is_configurable(self):
        """上限可配置（`attach.max_mb`），默认值有明确出处。

        不测"写配置能被读到"——那是配置管道的事；这里只确认默认值是常量
        而不是散落在代码里的魔法数字。
        """
        self.assertIsInstance(A.ATTACH_MAX_MB_DEFAULT, int)
        self.assertGreater(A.ATTACH_MAX_MB_DEFAULT, 0)
        self.assertEqual(A.ATTACH_MAX_BYTES_KEY, "attach.max_mb")

    # ------------------------------------------------------------- ② 敏感
    def test_sensitive_names_are_rejected(self):
        """`.env` / `id_rsa` / `*.pem` 这些不该因为"用户点了个 attach"就进记忆库。"""
        for name in (".env", "id_rsa", "server.pem", "api_passwords.txt"):
            p = self._mk(name, b"SECRET")
            with self.assertRaises(A.AttachmentRejected, msg=f"{name} 没被拦") as ctx:
                A.store_blob(self.root, p, conn=self.conn)
            self.assertEqual(ctx.exception.code, "attach_sensitive",
                             f"{name} 的拒绝码不对")

    def test_normal_names_pass(self):
        """对照组：普通文件必须通过——否则"全部拒绝"也能让上面那条变绿。"""
        p = self._mk("report.pdf", b"%PDF-1.4 fake")
        meta = A.store_blob(self.root, p, conn=self.conn)
        self.assertEqual(meta["size"], len(b"%PDF-1.4 fake"))

    def test_allow_sensitive_bypasses_name_but_not_size(self):
        """显式放行只关敏感名单，**不关大小**——否则那个开关就成了万能钥匙。"""
        p = self._mk(".env", b"X=1")
        meta = A.store_blob(self.root, p, conn=self.conn, allow_sensitive=True)
        self.assertEqual(meta["size"], len(b"X=1"))

        self._cap(1)
        big = self._mk("big2.bin", b"y" * (2 * 1024 * 1024))
        with self.assertRaises(A.AttachmentRejected) as ctx:
            A.store_blob(self.root, big, conn=self.conn, allow_sensitive=True)
        self.assertEqual(ctx.exception.code, "attach_too_large",
                         "放行敏感文件不该顺带放行超大文件")

    # ----------------------------------- ③ 位置拦截（系统目录 + 凭据目录；**不是白名单**）
    def test_directory_is_rejected(self):
        d = Path(self._tmp) / "adir"
        d.mkdir()
        with self.assertRaises(A.AttachmentRejected) as ctx:
            A.store_blob(self.root, d, conn=self.conn)
        self.assertEqual(ctx.exception.code, "attach_not_file")

    def test_system_roots_are_rejected(self):
        """系统目录下的文件拒绝入库。用**判定函数**直接验，不去碰真系统文件。"""
        for fake in (r"C:\Windows\System32\drivers\etc\hosts",
                     r"C:\Program Files\app\x.dll"):
            with self.assertRaises(A.AttachmentRejected, msg=fake) as ctx:
                A._attach_check_root(Path(fake))
            self.assertEqual(ctx.exception.code, "attach_denied_root")

    # ------------------------------------------------------------- ④ 流式
    def test_hash_matches_for_multi_chunk_file(self):
        """跨多个 1 MB 分块的文件，sha256 必须与独立计算一致。"""
        payload = os.urandom(3 * 1024 * 1024 + 12345)   # 3 个多分块
        p = self._mk("multi.bin", payload)
        meta = A.store_blob(self.root, p, conn=self.conn)
        self.assertEqual(meta["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(meta["size"], len(payload))
        stored = (self.root / meta["blob"]).read_bytes()
        self.assertEqual(stored, payload, "落盘内容与源不一致")

    def test_no_temp_leftovers(self):
        """**流式的可观测证据**：`.tmp` 目录必须存在过、且现在是空的。

        ## 第三轮评审：这条判据原来是假的

        原写法是：

            leftovers = list(tmp_dir.glob("*.part")) if tmp_dir.is_dir() else []
            self.assertEqual(leftovers, [])

        `.tmp` **不存在**时 `glob` 走 `else []`，`assertEqual([], [])` 恒过。
        于是把实现换回 `read_bytes()`（根本不建 `.tmp`）它照样绿——
        评审官实测：这**不是**"改坏了会红"的判据。

        而且它的 docstring 自己写着"**有** .tmp 且为空"才算证据——
        **代码从不断言"有"**。陈述与实现不一致的判据最危险：
        读的人以为这里有检查。
        """
        p = self._mk("stream.txt", b"z" * 100)
        A.store_blob(self.root, p, conn=self.conn)
        tmp_dir = self.root / "blobs" / ".tmp"
        self.assertTrue(tmp_dir.is_dir(),
                        "`.tmp` 目录不存在——说明走的不是分块+原子 rename 那条路径，"
                        "本判据失去靶点（判不到，不是通过）")
        leftovers = list(tmp_dir.glob("*.part"))
        self.assertEqual(leftovers, [], f"留下了半个文件：{leftovers}")

    def test_dedup_stores_once(self):
        """内容寻址的去重不能因为改成分块就丢。"""
        p1 = self._mk("one.txt", b"same content")
        p2 = self._mk("two.txt", b"same content")
        m1 = A.store_blob(self.root, p1, conn=self.conn)
        m2 = A.store_blob(self.root, p2, conn=self.conn)
        self.assertEqual(m1["sha256"], m2["sha256"])
        blobs = list((self.root / "blobs").rglob("*.txt"))
        self.assertEqual(len(blobs), 1, f"同一内容存了多份：{blobs}")


    # --------------------------------------------------- 🟠-6 不许烧编码号
    def test_rejected_attachment_does_not_burn_a_code(self):
        """**核心**：附件被拒时，编码号不能已经被分配掉。

        旧顺序是 `next_code()` → 校验附件，于是被拒的附件烧掉一个号，
        下一条记忆从 -02 开始。而 `record_memory` 上方自己写着
        「编码一经分配不可回收」——**一条违反自己不变量、却不报错的路径，
        比没有这条不变量更糟**：它让人以为它成立。

        实测方式：拒绝一次，然后断言 `last_seq` 没动、下一条仍是 -01。
        """
        self._cap(1)          # 让大小检查生效，用敏感名也行
        bad = self._mk(".env", b"SECRET=1")
        with self.assertRaises(A.AttachmentRejected):
            fish.record_memory(
                self.conn, self.root, project="P", title="带敏感附件", body="x\n",
                attachments=[str(bad)])
        seq = self.conn.execute(
            "SELECT last_seq FROM code_registry WHERE prefix='P'").fetchone()[0]
        self.assertEqual(seq, 0, f"被拒的附件烧掉了编码号：last_seq={seq}")
        code, _rel = fish.record_memory(
            self.conn, self.root, project="P", title="正常一条", body="y\n")
        self.assertTrue(code.endswith("-01"), f"下一条应该是 -01，实际 {code}")

    def test_validation_happens_before_coding(self):
        """结构判据：`validate_attachment` 的调用点必须在 `next_code` **之前**。

        行为判据（上面那条）能验结果，但说不清"如果下次有人调换顺序会怎样"。
        这条直接钉顺序。
        """
        src = (HERE / "fish.py").read_text(encoding="utf-8")
        i_val = src.index("validate_attachment(_ap")
        i_code = src.index("    code = next_code(conn, project)")
        self.assertLess(i_val, i_code,
                        "附件校验排在了 next_code 之后——被拒时会烧编码号")


    # ------------------------------------------- 🟠-5 凭据目录必须拦
    def test_credential_directories_are_rejected(self):
        """**核心**：`.ssh` / `.aws` 这些目录下的文件不许入库。

        评审官实测：实现原来只是 4 条系统目录黑名单，`<tmp>\.ssh\config`
        能直接进去。它不是"配错了"，是**功能根本不存在却被描述成存在**。
        """
        base = Path(self._tmp)
        for seg, fname in ((".ssh", "config"), (".aws", "credentials"),
                           (".gnupg", "secring.gpg")):
            d = base / seg
            d.mkdir(exist_ok=True)
            p = d / fname
            p.write_text("SECRET")
            with self.assertRaises(A.AttachmentRejected, msg=f"{seg}/{fname} 没被拦") as ctx:
                A.validate_attachment(p)
            # 两种拒绝码都算对：文件名命中敏感名单（`credentials`）会先被拦，
            # 目录命中凭据段是另一条路。**判据要盯的是"拒了"，不是"哪一条先开火"**——
            # 盯具体码会让判据在两条防线顺序调整时假红。
            self.assertIn(ctx.exception.code,
                          ("attach_denied_credential_dir", "attach_sensitive"),
                          f"{seg}/{fname} 的拒绝码不对：{ctx.exception.code}")

    def test_normal_path_still_passes(self):
        """对照组：普通目录下的普通文件必须通过——否则"一律拒绝"也能让上面变绿。"""
        p = self._mk("report.md", b"# hello")
        A.validate_attachment(p)

    def test_the_limitation_is_documented(self):
        """**诚实判据**：拦截面覆盖不到什么，必须写在代码里。

        评审的核心不是"漏了一条规则"，是"名不副实"——叫"允许根校验"而实际是
        4 条黑名单。所以这条判据盯的不是实现，是**说明**：
        源码里必须写清"内容里带密码的普通文件判不出来"这件事。
        """
        src = (HERE / "fish_attach.py").read_text(encoding="utf-8")
        self.assertIn("内容里带密码的普通文件", src,
                      "没有写明拦截面覆盖不到内容型秘密——下一个人会以为它是全面的")
        self.assertIn("不是白名单", src,
                      "没写明它是黑名单而不是白名单")


if __name__ == "__main__":
    unittest.main(verbosity=2)
