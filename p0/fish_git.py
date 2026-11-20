#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish 引入 git：仓库身份发现与观测（2026-11-20 · Phase 2）。

## 这个模块解决什么

fish 的「项目」只是记忆分组标签（`code_index.project`），它和「代码仓库」之间
没有任何键或推导——实测：40 个 git 仓库，`fish_activity.resolve_project` 只解析
出 4 个（12%），连 fish 自己的 `fish-public` 都是 `(None, None)`。

根因不是「少配了几条 workspace_map」，而是**路径不是可靠的身份**：目录会改名、
会移位置、有 junction / 符号链接 / 8.3 短名、Windows 还不区分大小写。

本模块给仓库一个**不依赖路径的身份**：规范化远端 URL（无远端时用内容哈希），
路径只是「它当前落在哪」的一个属性（`git_checkout`）。

## 核心设计（全部有实测支撑，见 83-方案 v2.0）

**P11 · 发现发生在运行时，不在扫描时。**
v1.0 方案是「从容器根向下扫 .git」——实测从 `workspace_map` 的 4 条根出发
只能发现 4 个仓库，且全盘扫描跑了 2 分钟没完成。改成从**落笔路径向上**
`git rev-parse --show-toplevel`：成本 O(路径深度)、必然命中、天然处理嵌套。

**P5 · 只读红线：所有 `status` 必须带 `--no-optional-locks`。**
实测：普通 `git status` 会刷新并**写回** index（哈希 `917FD00B…`→`100BC65D…`），
加参数后一个字节都不动。这不是洁癖——对 30 个仓库跑普通 status 就是改 30 个
仓库的 index，还可能和正在用它们的 agent 抢 `index.lock`。

**P4 · 判不到 ≠ 干净。**
`git_obs.state` 四态：`clean` / `dirty` / `unknown`（没采到）/ `degraded`
（采到但不可靠：bare / shallow / partial clone / 权限 / 损坏 / 超时）。

## 分层（J2）

本模块只依赖 `fish_common` / `fish_surface` / `fish_schema` / `fish_db`，
**不 import `fish`**。所有 git 操作走 subprocess，不引第三方库。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import fish_common
import fish_surface

# ── git 调用超时（秒）────────────────────────────────────────────────
# 为什么必须有：Windows 上 spawn git 本身约 90–114ms（实测），大仓库的
# status 见过 679ms；但没有超时的话，一个网络盘上的仓库能让 hook 挂死。
# hook 侧给的是 3 秒预算，这里留足余量但仍设硬上限。
_PROBE_TIMEOUT_SEC = 5.0

# Windows：不弹控制台窗口（hook 是后台进程，弹窗会闪）。
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run_git(args, *, cwd=None, timeout=_PROBE_TIMEOUT_SEC):
    """跑一条 git 命令，返回 `(ok, stdout, stderr)`；**从不抛异常**。

    为什么不抛：观测是**旁路**，一个坏仓库不该让整次采集挂掉。
    但也不静默——调用方拿到 `ok=False` 与 stderr，由它决定记 `degraded` 还是跳过。
    """
    cmd = ["git"]
    if cwd:
        cmd += ["-C", str(cwd)]
    cmd += list(args)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return False, "", f"timeout after {timeout}s"
    except OSError as exc:
        return False, "", f"spawn failed: {exc}"
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode == 0, out, err


# ══════════════════════════════════════════════════════════════════
# 一、身份归一：远端 URL → 稳定 identity
# ══════════════════════════════════════════════════════════════════
#
# 为什么需要归一：实测 21 个仓库里出现过这些形态——
#   git@github.com:kinganyi/fish.git          （SCP，无 scheme）
#   https://github.com/kinganyi/fish.git      （HTTPS，带 .git）
#   https://ghfast.top/https://github.com/... （代理前缀）
#   https://gitcode.com/gh_mirrors/co/ComfyUI.git（镜像站，非上游身份）
#   I:\app\...\fish-devA                      （远端本身是本地路径）
# 不归一就会让「同一个仓库」算出好几个 identity。

# 常见 GitHub 代理前缀（它们不是仓库身份的一部分，剥掉）
_PROXY_HOSTS = frozenset({
    "ghfast.top", "ghproxy.com", "gh-proxy.com", "mirror.ghproxy.com",
    "hub.fastgit.org", "github.com.cnpmjs.org",
})
# 镜像站：**身份与上游不同**（它们的 owner/repo 布局不一样），单列一类，
# 不能当 direct 处理——否则会把一个镜像标成上游本人。
_MIRROR_HOSTS = frozenset({
    "gitcode.com", "gitee.com", "gitlab.com", "codeberg.org",
})

_SCP_RE = re.compile(r"^(?P<user>[^@/]+)@(?P<host>[^:/]+):(?P<path>.+)$")
_URL_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?P<rest>.+)$")


def _strip_git_suffix(path):
    p = (path or "").strip().strip("/")
    if p.endswith(".git"):
        p = p[:-4]
    return p.rstrip("/")


def normalize_remote_url(url):
    """远端 URL → `(identity, kind)`。

    `kind` 取值：`direct` / `proxy` / `mirror` / `local_path` / `unknown`。

    判不到就返回 `(None, "unknown")`——**不猜**。identity 为 None 的仓库允许存在
    （空仓库、无法识别的远端），它们靠 `repo_id` 代理键区分、靠 `first_sha` 做弱证据。
    """
    raw = (url or "").strip()
    if not raw:
        return None, "unknown"

    # 本地路径（远端指向磁盘）：Windows 盘符或 POSIX 绝对路径
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("/"):
        norm = fish_surface.normalize_path(raw)
        return ("local:" + norm) if norm else None, "local_path"

    host = path = ""
    used_proxy = False
    m = _SCP_RE.match(raw)
    if m:                                  # git@host:owner/repo.git
        host, path = m.group("host"), m.group("path")
    else:
        m = _URL_RE.match(raw)
        if m:                              # scheme://rest
            rest = m.group("rest")
            # 代理形态：https://ghfast.top/https://github.com/owner/repo
            # 剥掉外层的 `.../` 前缀后，rest 里还嵌着一个完整 URL。
            # ⚠️ 不能只测「rest 是否以 scheme:// 开头」——实测 rest 是
            #    `ghfast.top/https://github.com/...`，它以一个主机名开头，
            #    那个判断永远为假，前缀就剥不掉（第一版就错在这里）。
            while True:
                idx = rest.find("://")
                if idx <= 0:
                    break
                used_proxy = True
                rest = rest[idx + 3:]
            host, _, path = rest.partition("/")
        elif "/" in raw:                   # 裸写法 host/owner/repo
            host, _, path = raw.partition("/")
        else:
            return None, "unknown"

    host = host.strip().lower()
    path = _strip_git_suffix(path)
    if not host or not path:
        return None, "unknown"

    if used_proxy or host in _PROXY_HOSTS:
        kind = "proxy"
    elif host in _MIRROR_HOSTS:
        kind = "mirror"
    else:
        kind = "direct"

    return f"{host}/{path.lower()}", kind


# ══════════════════════════════════════════════════════════════════
# 二、发现：从落笔路径向上找仓库根
# ══════════════════════════════════════════════════════════════════

def discover_root(path):
    """从 `path` 向上找最近的仓库根，返回归一化绝对路径；找不到返回 None。

    **为什么向上而不是向下扫**：见模块 docstring 的 P11。`rev-parse` 天然返回
    最近的仓库根，所以嵌套仓库（`anyi-studio/.../ComfyUI`）会正确返回 ComfyUI
    自己，而不是被父项目吞并——那是 v1.0 用「最长前缀匹配」时的实测错误。

    bare 仓库要单独判：`rev-parse --show-toplevel` 在 bare 仓里会失败，
    但它确实是个仓库，所以先用 `--is-bare-repository` 分一次。
    """
    if not path:
        return None
    p = Path(str(path))
    # 落笔路径可能是文件（就像 Write 的 file_path）——从它的目录开始找
    if p.suffix and not p.is_dir():
        p = p.parent
    if not p.exists():
        return None

    ok, out, _ = _run_git(["rev-parse", "--is-bare-repository"], cwd=p)
    if ok and out.strip() == "true":
        ok2, out2, _ = _run_git(["rev-parse", "--git-dir"], cwd=p)
        if ok2:
            return fish_surface.normalize_path(out2.strip()) or None
        return None

    ok, out, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=p)
    if not ok:
        return None
    root = out.strip()
    return fish_surface.normalize_path(root) if root else None


def read_remotes(root):
    """读仓库的全部远端，返回 `[(name, url), ...]`（按 name 排序）。

    `git remote -v` 每行是 `<name>\\t<url> (fetch|push)` —— 所以是
    `parts[0]=name`、`parts[1]=url`。**第一版写反了**（`seen[parts[1]] = parts[0]`），
    实测输出 `[('git@github.com:...', 'origin')]` 才发现。
    """
    ok, out, _ = _run_git(["remote", "-v"], cwd=root)
    if not ok:
        return []
    seen = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in seen:
            seen[parts[0]] = parts[1]      # name -> url（fetch 行先到）
    return sorted(seen.items())


# ══════════════════════════════════════════════════════════════════
# 三、观测：L0（廉价）/ L1（状态）
# ══════════════════════════════════════════════════════════════════

def probe_l0(root):
    """L0：只读 ref，拿分支与 HEAD。近零成本（实测 40 仓 67.8ms）。

    不用手解 `.git/HEAD` + `packed-refs` 的原因：worktree/submodule 的 `.git`
    是**文件**不是目录、bare 仓没有 `.git/HEAD`、reftable 后端没有 packed-refs
    ——三种情况都会静默判空。走 `for-each-ref` 更稳。
    """
    result = {"branch": None, "head_sha": None, "degraded": None}
    ok, out, err = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if ok:
        name = out.strip()
        result["branch"] = None if name == "HEAD" else name   # detached
    else:
        result["degraded"] = "timeout" if "timeout" in err else "unresolvable"

    ok, out, _ = _run_git(["rev-parse", "HEAD"], cwd=root)
    if ok:
        result["head_sha"] = out.strip() or None
    return result


def probe_l1(root):
    """L1：脏净 + ahead/behind（`--porcelain=v2 --branch`）。

    **`--no-optional-locks` 是硬约束**（模块 docstring 的 P5）：普通 status 会
    写回 index、还可能抢 `index.lock`。
    """
    result = {
        "branch": None, "head_sha": None, "upstream": None,
        "ahead": None, "behind": None, "dirty": None,
        "staged": None, "unstaged": None, "untracked": None,
        "state": "unknown", "degraded_reason": None,
    }
    ok, out, err = _run_git(
        ["--no-optional-locks", "status", "--porcelain=v2", "--branch"], cwd=root)
    if not ok:
        result["degraded_reason"] = "timeout" if "timeout" in err else "probe_failed"
        result["state"] = "degraded"
        return result

    staged = unstaged = untracked = 0
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            name = line[len("# branch.head "):].strip()
            result["branch"] = None if name == "(detached)" else name
        elif line.startswith("# branch.oid "):
            result["head_sha"] = line[len("# branch.oid "):].strip()
        elif line.startswith("# branch.upstream "):
            result["upstream"] = line[len("# branch.upstream "):].strip()
        elif line.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if m:
                result["ahead"], result["behind"] = int(m.group(1)), int(m.group(2))
        elif line.startswith("1 ") or line.startswith("2 "):
            # 1 = 普通变更，2 = 重命名/复制；第二个字符是 XY 状态
            xy = line.split()[1] if len(line.split()) > 1 else ".."
            if len(xy) >= 2:
                if xy[0] not in ". ": staged += 1
                if xy[1] not in ". ": unstaged += 1
        elif line.startswith("u "):
            staged += 1                     # 未合并冲突算「脏」
        elif line.startswith("? "):
            untracked += 1

    result["staged"], result["unstaged"], result["untracked"] = staged, unstaged, untracked
    result["dirty"] = 1 if (staged or unstaged or untracked) else 0
    result["state"] = "dirty" if result["dirty"] else "clean"
    return result


def probe_shallow(root):
    """是否浅克隆。浅仓的「历史」不完整，`rev-list` 之类的读数要标注。"""
    ok, out, _ = _run_git(["rev-parse", "--is-shallow-repository"], cwd=root)
    return bool(ok and out.strip() == "true")


# ══════════════════════════════════════════════════════════════════
# 四、离线清单：一次性基线（与运行时的 P11 分工明确）
# ══════════════════════════════════════════════════════════════════
#
# 为什么要两份机制，而不是一份：
#   运行时（hook 里）：从落笔路径向上找，O(深度)、220ms/仓、必然命中。
#   离线（一次性）：向下扫根目录，O(全盘)、约 10s、只用于建基线清单。
# 判据锚在**清单**上而不是绝对数：v1.0 写「33 个仓库中被找到的都登记」，但
# 那个「33」复现不出来（另两次实测分别是 40 和 45）。锚在清单上，判据才可复现。

# 扫描时跳过的目录（不是仓库、但会拖慢 walk）
_SKIP_DIRS = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".tox",
})


def scan_repos(roots, *, max_depth=3):
    """离线扫描 `roots` 下深度 ≤ `max_depth` 的全部 git 仓库。

    返回按 path 排序的列表，每项：
    `{path, identity, identity_kind, remotes:[{name,url}], shallow}`

    **嵌套仓库会被独立发现**（不吞并到父仓库）——实测 `anyi-studio` 里套着
    `anyi-studio-comfy/ComfyUI`，它是独立仓库。
    """
    found = {}
    for base in roots:
        base_p = Path(str(base))
        if not base_p.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base_p):
            rel = os.path.relpath(dirpath, base_p)
            depth = 0 if rel == "." else len(Path(rel).parts)
            if depth > max_depth:
                dirnames[:] = []
                continue
            if ".git" in dirnames or ".git" in filenames:
                norm = fish_surface.normalize_path(dirpath)
                if norm:
                    remotes = read_remotes(dirpath)
                    ident, kind = (None, "unknown")
                    if remotes:
                        ident, kind = normalize_remote_url(remotes[0][1])
                    found[norm] = {
                        "path": norm,
                        "identity": ident,
                        "identity_kind": kind,
                        "remotes": [{"name": n, "url": u} for n, u in remotes],
                        "shallow": probe_shallow(dirpath),
                    }
            # 继续深入（嵌套仓库要发现），只跳噪声目录
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
    return sorted(found.values(), key=lambda x: x["path"])


def write_inventory(out_path, roots, *, max_depth=3):
    """生成 `repo_inventory.json` —— J1 判据的**清单锚点**。

    返回写出的 payload（调用方可以直接用，不必回读文件）。
    `identity` 为 None 的条目是**允许的**（无远端的本地仓库），它们靠 path
    区分、靠 `shallow` 等字段标注质量。
    """
    repos = scan_repos(roots, max_depth=max_depth)
    payload = {
        "version": 1,
        "generated_at": fish_common.now_utc(),
        "roots": [str(r) for r in roots],
        "max_depth": max_depth,
        "count": len(repos),
        "with_identity": sum(1 for r in repos if r["identity"]),
        "repos": repos,
    }
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
