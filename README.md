# fish（大肥鱼生命系统）

给 AI 助手用的**长期记忆与协作层**。单机、单仓、多模块的 Python 项目：
SQLite 事件溯源 + FTS5 全文检索 + MCP 服务 + CLI + 编辑器/宿主 hooks。

> 一句话：让多个 AI 会话共享同一份"记得住"的记忆，并且**只在真有价值时**才知道。

## 它能做什么

| 能力 | 入口 | 说明 |
|---|---|---|
| 记一条记忆 | `fish remember` / MCP `remember` | 编码规则 `<前缀>-<YYMMDD>-<序号>`，正文落 Markdown 文件，索引进 SQLite |
| 检索 | `fish recall` / MCP `recall` | 两段式召回（先 AND，无命中再 OR 回退并标记 `meta.fallback`） |
| 推送 | `fish push` / `fish prompt-push` / PostToolUse hook | 按"收益−成本"打分选候选；结案（读过/过期）后不再重复推 |
| 注入包 | `fish inject` / MCP `inject` | 按 token 预算裁剪的 L0/L1/L2 三段包，超预算在正文里留痕 |
| 协作看板 | `fish board` | 谁在动哪块、有没有撞车 |
| 语料层 | `fish corpus *` | 把历史会话切成可检索的段 |
| 知识库 | `fish kb *` | 对 Obsidian 之类的 Markdown 库做离线索引 |

## 快速开始

```bash
# 1. 依赖（只有一个可选依赖，缺了会让部分测试静默少跑）
python -m pip install -r p0/requirements.txt

# 2. 初始化数据目录
python p0/fish.py init

# 3. 注册一个项目前缀（编码需要它）
python p0/fish.py project-add demo --namespace "示例项目"

# 4. 记一条、再搜回来
python p0/fish.py remember demo "第一条记忆" --body "正文"
python p0/fish.py recall 第一条

# 5. 健康检查（事件/记忆/索引/断链）
python p0/fish.py healthcheck
```

**要求**：Python ≥ 3.11（用到 `tomllib` / `zoneinfo`）。

## 测试

```bash
cd p0
python -m unittest discover -p "test_*.py"
```

当前基线：**1,647 个测试通过，3 个 skip**（skip 的 3 条是"判不到"——公开包目录不存在等）。

仓库里还有 **27 个变异体**（`p0/mutation_*.py`），用来证明判据**真的会失败**：

```bash
cd p0
python mutation_p2.py     # 9 个变异：CAUGHT 9/9 才算过
```

## 目录结构

```
p0/                 主体（单文件编排 + 领域模块，依赖方向有判据钉住）
  fish.py               编排层（CLI / 领域函数 / 事件 / 投影）
  fish_push*.py         推送：候选生成 / 门槛 / 去重 / 融合排序
  fish_corpus*.py       语料层（采集 → 切段 → 检索 → 推送候选）
  fish_kb*.py           知识库索引（离线 FTS）
  mcp_server.py         MCP 服务（stdio）
  hook_fastpath.py      hook 快速通道（关闭态早退 + 按 hook 冷却）
  mutation_*.py         变异体（判据自证）
  test_*.py             判据
docs/               设计/评审/对账文档（历史记录，含早期本机路径）
*.md                各个阶段的设计与评审报告
```

## 设计原则（也是判据）

1. **单一写入/读取入口**：CLI 与 MCP 共用同一份函数，禁止各写一份。
2. **依赖方向不可逆**：领域模块不得 `import fish`（J2/J6），行数窗口与导出面双向对账（J1b/J4）。
3. **判据必须能失败**：每个关键判据都配一个变异体，改坏实现必须红。
4. **不静默降级**：拿不到证据就标"判不到"，不写成"通过"。
5. **默认关闭新行为**：会改变排序/推送的开关全部默认关，观察后再开。

## 已知限制（诚实清单）

- **`visibility=private` 目前只是标签**：写入可选、默认 `shared`；执行层未完成——
  只有 `search_memories` 一条路径会按 owner 过滤，另有 10 条读路径仍会返回 private
  （`read_body` 全文、`resolve_code` 元数据、`recall` 的回退列表、`list`、`neighbors`、
  `inject` 注入包、`recall --by relevance`、推送管线、`fish://index` 资源面）。
  **不要**打开 `privacy.visibility_enforced`（打开后连 owner 自己也看不到）。
- **新开关默认关**：`push.rrf_enabled` / `push.factors_enabled` / `push.mmr_enabled` /
  `push.value_floor` 都会改变推送排序，需观察后再开。
- **单机单用户**：多 AI 会话共享同一个 OS 账户与数据库文件，逻辑层的"可见性"不构成
  存储层边界（正文是明文 `.md`）。
- **`docs/` 与根目录的历史文档**保留了写作当时的环境信息（本机路径等），属项目记录。

## 许可

未声明。内部项目，暂未选择开源许可证。
