"""推送的「融合/排序」层（2026-10-29 从 fish_push.py 抽出）。

抽出的理由：**fish_push.py 已经 1,647 行、J4 窗口被抬了 6 次**（审查 §6.1：
"按正在出事故的不变量抽层，而不是按行数拆文件"）。这一层的四组不变量各自独立：

  · `_rrf_*`      —— 跨路融合（Reciprocal Rank Fusion：只看名次，免疫量纲）
  · `_factor*`    —— 三因子（Generative Agents：recency × importance × relevance）
  · `_mmr_*`      —— 多样性（Maximal Marginal Relevance）
  · `_value_*`    —— 价值门（importance × relevance 低于阈值就不推）

四个开关全部**默认关**（`push.rrf_enabled` / `push.factors_enabled` /
`push.mmr_enabled` / `push.value_floor`），所以"抽出去"本身不改变任何行为；
行为由 test_rrf_fuse / test_factors / test_mmr_value 逐条钉住。

依赖方向：本模块只依赖 `fish_common`（配置读取）与 stdlib——不 import `fish`，
也不 import `fish_push`（J2/J6）。
"""
import math

from fish_common import get_config


def _factors_enabled(conn) -> bool:
    """`push.factors_enabled`（默认 False：保持既有排序，零回归）。"""
    try:
        return bool(get_config(conn, "push.factors_enabled"))
    except Exception:       # noqa: BLE001
        return False


def _factor_weights(conn) -> dict:
    """三个可配权重（`push.factor_recency/importance/relevance`）。"""
    def _w(key, default):
        try:
            return float(get_config(conn, key))
        except Exception:   # noqa: BLE001
            return default
    return {"recency": _w("push.factor_recency", 0.2),
            "importance": _w("push.factor_importance", 0.3),
            "relevance": _w("push.factor_relevance", 0.5)}


def _apply_factors(conn, cands, weights=None):
    """三因子打分（P2 阶段，Generative Agents: recency × importance × relevance）。

    为什么放在 RRF 之后：**RRF 解决跨路公平**（只排名次），三因子解决**路内重要性**。
    三个因子全部来自已有数据、不新造模型：
      · recency    = `cand["freshness"]["factor"]`（`memory_freshness` 算的；
                     rule/identity/lesson 恒 1.0，天然免疫年龄惩罚）
      · importance = `access_count` 的 log 归一（50 次封顶）——"全局热度"的代理
      · relevance  = `cand["relevance"]`（有则用）否则 RRF/初排分

    权重可配、默认关（`push.factors_enabled`）；返回新候选（不改入参）。
    """
    w = weights or _factor_weights(conn)
    out = []
    for cand in cands:
        rec = float((cand.get("freshness") or {}).get("factor", 1.0))
        rel = float(cand.get("relevance") or cand.get("score") or 0.0)
        row = conn.execute("SELECT access_count FROM code_index WHERE code=?",
                           (cand.get("code"),)).fetchone()
        acc = int((row[0] if row else 0) or 0)
        imp = math.log1p(acc) / math.log1p(50.0)      # 50 次封顶 → [0,1]
        rel = max(0.0, min(rel, 1.0))
        score = (max(rel, 0.01) ** w["relevance"]
                 * max(imp, 0.01) ** w["importance"]
                 * max(rec, 0.01) ** w["recency"])
        cand = dict(cand)
        cand["score"] = round(min(score, 1.0), 6)
        cand["factors"] = {"recency": round(rec, 4),
                           "importance": round(imp, 4),
                           "relevance": round(rel, 4)}
        out.append(cand)
    return out


def _rrf_enabled(conn) -> bool:
    """`push.rrf_enabled`（默认 False：保持"分数直排"的既有行为，零回归）。"""
    try:
        return bool(get_config(conn, "push.rrf_enabled"))
    except Exception:       # noqa: BLE001 —— 配置坏了就按关处理
        return False


def _rrf_fuse(cands, *, k=60):
    """RRF 融合（Reciprocal Rank Fusion，Cormack et al., SIGIR 2009）。

    为什么需要它：记忆的 score 是"收益−成本×静默降权"、知识库是字面命中数、
    语料是检索分——**三把不同的尺子**（外部审查 🟠-P/Q 指出的正是这个）。
    RRF 的解法：每路**只在路内排**，`score = Σ_r 1/(k + rank_r(d))`，最后归一到 [0,1]
    （与既有分数同域，不破坏下游的阈值判断）。k=60 是 RRF 论文的默认值。
    """
    routes = {}
    for cand in cands:
        kind = str(cand.get("kind") or "")
        route = ("kb" if kind == "kb" else
                 "corpus" if kind in ("conv", "conv_related", "corpus") else
                 "board" if kind in ("board", "situational") else "memory")
        routes.setdefault(route, []).append(cand)
    fused = {}
    for route, items in routes.items():
        items.sort(key=lambda c: (-float(c.get("score") or 0.0), str(c.get("code"))))
        for rank, cand in enumerate(items, start=1):
            key = str(cand.get("code"))
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank)
    if not fused:
        return cands
    top = max(fused.values()) or 1.0
    out = []
    for cand in cands:
        key = str(cand.get("code"))
        if key in fused:
            cand = dict(cand)
            cand["score"] = round(fused[key] / top, 6)
            cand["rrf"] = round(fused[key], 6)
        out.append(cand)
    return out

# ---------------------------------------------------------------- 六特征重排（T03b）
#
# T03b-R2（2026-10-28）把重排**整段**搬进 `fish_push_rerank`（评审 S4：本文件当时贴着
# J4 的 1,500 行上限，只剩 1 行余量）。这里只留三样"生成侧才有的知识"：
#   · `_with_ctx_project`：当前项目由本模块知道，贴给候选（`_infer_project` 不必猜）；
#   · `select_push_candidates`：开关 / top-K / now 的唯一入口；
#   · `_kb_push_candidates` / `_conv_push_candidates` 的断路器调用点。
# 依赖方向不变（J2/J6）：fish_push → fish_push_rerank → fish_common / fish_corpus_rank。


def _value_floor(conn) -> float:
    try:
        return float(get_config(conn, "push.value_floor"))
    except Exception:       # noqa: BLE001
        return 0.0


def _value_ok(cand, floor) -> bool:
    """价值门：`relevance × (0.3 + 0.7×importance)` 是否过线。

    **为什么 importance 不能是硬零**（2026-10-29 部署验证抓到的真问题）：
    新记忆 / 从没被读过的条目 `access_count=0` → `importance=0` → 旧公式
    `importance × relevance = 0` → 被 `value_floor` 一刀切挡掉。实测把
    `push.value_floor=0.1` 打开后，**整条推送变空**（"漫剧剧本怎么写"一个候选
    都不剩）——配置看起来"成功"，实际上是**把自己关掉了**。

    新内容有**先天价值**：给 importance 一个 0.3 的底（读得越多越接近 1.0）。
    缺 factors 的候选按 1.0 放过，不误杀。
    """
    f = cand.get("factors") or {}
    imp = float(f.get("importance", 1.0))
    rel = float(f.get("relevance", cand.get("score") or 1.0))
    return (rel * (0.3 + 0.7 * max(0.0, min(imp, 1.0)))) >= floor


def _mmr_enabled(conn) -> bool:
    try:
        return bool(get_config(conn, "push.mmr_enabled"))
    except Exception:       # noqa: BLE001
        return False


def _mmr_select(conn, cands, quota, *, lam=None):
    """MMR 贪心选择：`λ·rel(d) − (1−λ)·max_{s∈S} sim(d,s)`。

    没有 embedding，所以用**可解释的粗相似度**（取两者较大）：
      · 同 kind + 同 project → 0.8（同一类东西的两条）
      · 标题字符集合的 Jaccard → [0,1]
    目标只有一个：**别让两条几乎一样的条目占两格**。
    """
    if quota <= 0:
        return []
    if lam is None:
        try:
            lam = float(get_config(conn, "push.mmr_lambda"))
        except Exception:   # noqa: BLE001
            lam = 0.7

    def _sim(a, b):
        same = 0.8 if (a.get("kind") == b.get("kind")
                       and (a.get("project") or "") == (b.get("project") or "")) else 0.0
        ta = set(str(a.get("title") or ""))
        tb = set(str(b.get("title") or ""))
        jac = (len(ta & tb) / float(len(ta | tb))) if (ta or tb) else 0.0
        return max(same, jac)

    pool, chosen = list(cands), []
    while pool and len(chosen) < quota:
        best, best_v = None, None
        for cand in pool:
            rel = float(cand.get("score") or 0.0)
            div = max((_sim(cand, s) for s in chosen), default=0.0)
            val = lam * rel - (1.0 - lam) * div
            if best_v is None or val > best_v:
                best, best_v = cand, val
        chosen.append(best)
        pool.remove(best)
    return chosen
