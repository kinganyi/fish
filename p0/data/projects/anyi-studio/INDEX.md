# anyi-studio · 记忆索引

> 共 108 条（active 83）｜ 本文件由 `gen_index_md.py` 生成，可随时重建。
>
> **查正文用 `read_body <编码>`**——直接读文件会绕过衰减/访问记录/关联维护。
>
> 文件名里的标题是**记录时的快照**（改标题不会改文件名）；本表才是最新标题。

| 编码 | 类型 | 状态 | 记录时间 | 标题 |
|---|---|---|---|---|
| `anyi-studio-261022-05` | rule | active | 2026-10-22 | 协作铁律·行为层（CR-2/5/6/7/11 + 边界模糊先报备） |
| `anyi-studio-261022-06` | rule | active | 2026-10-22 | 引用与验证纪律（CR-12 引用三件套 / CR-14 验证声明五字段） |
| `anyi-studio-261022-10` | artifact | active | 2026-10-21 | 附件与会话身份联调 |
| `anyi-studio-261022-100` | artifact | active | 2026-10-22 | MCP 接入回执 v2.1（对外）：paid_* 已修并部署 / 角色表述更正 / 渠道三方案 |
| `anyi-studio-261022-101` | lesson | active | 2026-10-22 | 客户以为能自选渠道，真相是 hcatom 失败自动降级到更贵的 notoken（+7.7%） |
| `anyi-studio-261022-102` | lesson | active | 2026-10-22 | task.validate 余额恒 510：users.id 与 identity_id 两套积分主体键并存（已修 f477f2f） |
| `anyi-studio-261022-104` | artifact | active | 2026-10-22 | MCP 接入回执 v2.2（对外）：余额已修 / 不调价·以终值为准 / cost_actual 变更通知 |
| `anyi-studio-261022-105` | rule | active | 2026-10-22 | 经验值清单 EXP-01~17：协作阈值与失效信号（权威源 notes/经验值清单.md） |
| `anyi-studio-261022-106` | artifact | active | 2026-10-22 | 协作模板集 8 套：任务卡/验收记录/送审呈报/边界报告/账本行/分工表/验证范围契约/验证声明 |
| `anyi-studio-261022-107` | rule | active | 2026-10-22 | 编号体系公约 v1.0：五法则＋自生长流程＋15 前缀表＋CR/ER 铁律分层＋状态机 |
| `anyi-studio-261022-108` | artifact | active | 2026-10-22 | 迁移台账 v3：铁律/协作/错题本/EXP/模板集/编号公约均已迁完，剩 artifacts 结论段与 daily 决策行 |
| `anyi-studio-261022-109` | rule | active | 2026-10-22 | 工作空间索引 v2：fish 作入口、正文留 artifacts/notes（正文检索已上线，检索规则已更新） |
| `anyi-studio-261022-111` | lesson | active | 2026-10-22 | 双键不是多站点的保障，users.tenant_id NOT NULL 才是多站点的枷锁 |
| `anyi-studio-261022-112` | rule | active | 2026-10-22 | anyi-studio 积分是内测体验值无真实资金；users 表要彻底删不要兼容视图 |
| `anyi-studio-261022-113` | fact | active | 2026-10-22 | anyi-studio 不存 PHP2Shell 攻击面：生产无 PHP 运行时（CVE-2026-109618/109619 排查留痕） |
| `anyi-studio-261022-114` | lesson | **superseded** | 2026-10-22 | fish 反馈：recall 未命中的「最近条目」恒为同 5 条无关条目（ORDER BY code 字典序），且 project 过滤在 fallback 失效 |
| `anyi-studio-261022-12` | rule | active | 2026-10-21 | fish 使用约定（anyi-studio 主AI 会话工作方式） |
| `anyi-studio-261022-13` | lesson | **superseded** | 2026-10-21 | fish 反馈：register 幂等复用时不同显 session_name，写入前无法确认归属 |
| `anyi-studio-261022-14` | rule | active | 2026-10-21 | fish 记忆库工作方式：走工具、不直连 data/sqlite、编码由系统分配 |
| `anyi-studio-261022-15` | rule | active | 2026-10-21 | 订正：记忆编码是 <PROJECT>-YYMMDD-NN，26 位十六进制是公民编码 |
| `anyi-studio-261022-16` | lesson | **superseded** | 2026-10-21 | fish 反馈：recall 多词查询直接未命中且召回不含正文，无法判断是否重复 |
| `anyi-studio-261022-17` | lesson | **superseded** | 2026-10-21 | fish 反馈：同项目同主题被两个会话各记一条 rule，没有合并/覆盖/废弃手段 |
| `anyi-studio-261022-18` | rule | active | 2026-10-21 | Codex 使用 fish 的工作方式 |
| `anyi-studio-261022-19` | artifact | active | 2026-10-21 | 夜间修复批次审查：六条修复属实，缺陷#5 的 SECURITY DEFINER 属主未验是唯一硬门 |
| `anyi-studio-261022-20` | lesson | active | 2026-10-21 | 判据必须核项目实际取值——把业务码当 HTTP 码判，会让「积分不足」变永久锁死 |
| `anyi-studio-261022-24` | rule | **withdrawn** | 2026-10-21 | 设计协同表分析口径：阶段定义、完成率、字段陷阱 |
| `anyi-studio-261022-25` | fact | **withdrawn** | 2026-10-21 | 主图 AI 效率数据：两套对外口径（合理组/夸张组）+ 软肋 |
| `anyi-studio-261022-26` | rule | **withdrawn** | 2026-10-21 | 对外文案七条规律（从安逸改写海报中学到的） |
| `anyi-studio-261022-27` | lesson | **withdrawn** | 2026-10-21 | 反面清单：四次被安逸指出的错误（文案与数据） |
| `anyi-studio-261022-28` | rule | **withdrawn** | 2026-10-21 | 飞书跳组织文档读写：第二应用 bot 身份（免扫码） |
| `anyi-studio-261022-29` | fact | **withdrawn** | 2026-10-21 | 业务定位与术语：企业 AI 应用落地 / Productized Service / DPM·DPA·IPA |
| `anyi-studio-261022-30` | artifact | active | 2026-10-21 | 交接：生成防重 P0 已合 main 但生产未生效（dist 未重建）+ P1 定稿位置 + G-1 待办 |
| `anyi-studio-261022-31` | lesson | **superseded** | 2026-10-21 | fish 反馈：没有工具能读记忆正文，resolve_code 只回元数据 |
| `anyi-studio-261022-32` | lesson | **superseded** | 2026-10-21 | fish 反馈：recall 验收用例「fish 反馈」不能区分新旧代码（标题前缀就是查询词） |
| `anyi-studio-261022-36` | lesson | **superseded** | 2026-10-21 | fish 反馈：register 对已存在身份回显 session_name 但实际未绑定（与 supersede 同型） |
| `anyi-studio-261022-37` | fact | active | 2026-10-21 | fish 时序澄清：register 的 session_name 绑定不是每次都生效（第三次才成功） |
| `anyi-studio-261022-48` | fact | active | 2026-10-22 | fish 工具链升级验收 + 本会话使用要点（read_body 可直调 / recall 只搜标题） |
| `anyi-studio-261022-50` | rule | active | 2026-10-22 | 工作空间迁入 fish 的分层策略：只迁结论与索引，不迁原文正文 |
| `anyi-studio-261022-52` | lesson | active | 2026-10-22 | fish 反馈：inject 无法指定项目，多项目身份下『当前项目』恒取最近更新者 |
| `anyi-studio-261022-53` | fact | active | 2026-10-22 | fish 能力边界（2026-10-22 实测）：召回只到标题、无项目过滤、无作废语义 |
| `anyi-studio-261022-56` | artifact | active | 2026-10-22 | MCP 接入回执 v2.0（对外·2026-10-22）：validate 已修 / 额度剩余 189.76 / 幂等仍未接线 |
| `anyi-studio-261022-57` | artifact | active | 2026-10-22 | 夜间修复批送审包（12 提交 / 已上线 fbe0bd2 / 6400P0F / 审查四意见已收口） |
| `anyi-studio-261022-58` | artifact | active | 2026-10-22 | anyi-studio 工作空间地图 v1：权威层/产物层/每日层三层结构 |
| `anyi-studio-261022-59` | lesson | **superseded** | 2026-10-22 | fish 反馈：召回只到标题且无项目过滤，接不下千条量级的工作空间 |
| `anyi-studio-261022-60` | lesson | **superseded** | 2026-10-22 | fish 反馈：前缀规则三处矛盾（ADR/规格/工具描述/库内实际），且 MCP 无 config 读取入口 |
| `anyi-studio-261022-61` | lesson | active | 2026-10-22 | 订正：inject 已能读出衰减参数；前缀规则三处矛盾仍成立 |
| `anyi-studio-261022-63` | rule | active | 2026-10-22 | 工程铁律·诊断与验证（ER-1/2/3/4/11/14/20/23/28/30/31） |
| `anyi-studio-261022-64` | rule | active | 2026-10-22 | 工程铁律·环境与部署（ER-5/8/10/25/26/27/29） |
| `anyi-studio-261022-65` | rule | active | 2026-10-22 | 工程铁律·编码与协作（ER-6/7/9/12/13/15/16/17/18/19/21/22/24） |
| `anyi-studio-261022-66` | lesson | active | 2026-10-22 | PIT 错题本·本机环境陷阱（解释器/路径/safe-delete/代理）19 条 |
| `anyi-studio-261022-67` | rule | active | 2026-10-22 | 迁移终态：fish 存全量正文，workspace 退为只读归档（首周仅索引先行） |
| `anyi-studio-261022-69` | lesson | **superseded** | 2026-10-22 | PIT 错题本·部署与迁移（PG形态/owner/chown/停服窗）16 条 |
| `anyi-studio-261022-70` | lesson | active | 2026-10-22 | PIT 错题本·验证与审查方法论（判据要有判别力）19 条 |
| `anyi-studio-261022-71` | rule | active | 2026-10-22 | 工程铁律 ER-1~ER-14：输出真实性、启动器与测试顺序（全文） |
| `anyi-studio-261022-72` | rule | active | 2026-10-22 | 工程铁律 ER-15~ER-24：MUI/编辑串行/静态断言/时区（全文） |
| `anyi-studio-261022-73` | lesson | active | 2026-10-22 | PIT 错题本·数据一致性与并发（幂等/竞态/墓碑/默认值）13 条 |
| `anyi-studio-261022-74` | rule | active | 2026-10-22 | 工程铁律 ER-25~ER-31：启动冒烟/停服窗/远程命令/验证留痕（全文） |
| `anyi-studio-261022-75` | rule | active | 2026-10-22 | 协作约定 CR-1~CR-15：多AI并发/Git降级/引用纪律/验收三态/身份门（全文） |
| `anyi-studio-261022-76` | lesson | active | 2026-10-22 | PIT 错题本·前后端契约与状态同步（引用ID/改名/实例复用）12 条 |
| `anyi-studio-261022-77` | lesson | active | 2026-10-22 | PIT 错题本·外部集成与错误语义（字段名/审核码/脱敏）10 条 |
| `anyi-studio-261022-78` | artifact | **superseded** | 2026-10-22 | 迁移台账 v1（2026-10-22）：铁律与协作约定已迁完，PIT/经验值待迁 |
| `anyi-studio-261022-79` | lesson | active | 2026-10-22 | PIT 错题本·协作与交付纪律（并发覆盖/已建未接线/凭证）12 条 |
| `anyi-studio-261022-80` | lesson | **superseded** | 2026-10-22 | fish 反馈：跨项目共享规范被各自重写一份，且无 project 过滤 → 规则会分叉 |
| `anyi-studio-261022-81` | lesson | active | 2026-10-22 | 客户实测三反馈：paid_* 租户取错已修 / 角色判断我写错 / 渠道不可指定是设计 |
| `anyi-studio-261022-82` | lesson | active | 2026-10-22 | PIT 错题本·部署与迁移（PG形态/owner/chown/停服窗）17 条 |
| `anyi-studio-261022-83` | lesson | **superseded** | 2026-10-22 | fish 反馈：recall 只匹配标题，导致区间式标题下的单号（PIT-062/EXP-05/CR-12）检索不到 |
| `anyi-studio-261022-84` | rule | active | 2026-10-22 | ER-30 · 新数据类型首次写路径须验生产形态(PG) ｜ 指针→ANYI-261022-74 |
| `anyi-studio-261022-85` | rule | active | 2026-10-22 | ER-31 · 声称验证必须留痕＋覆盖完整因果链 ｜ 指针→ANYI-261022-74 |
| `anyi-studio-261022-86` | rule | active | 2026-10-22 | ER-29 · 改端点须 grep 全测试库引用并同步断言 ｜ 指针→ANYI-261022-74 |
| `anyi-studio-261022-87` | rule | active | 2026-10-22 | CR-10 · 节流硬线：台账待裁决条目 ≤3 ｜ 指针→ANYI-261022-75 |
| `anyi-studio-261022-88` | rule | active | 2026-10-22 | CR-14 · 验证范围分层（L0~L3 四层跑法）＋验证声明 ｜ 指针→ANYI-261022-75 |
| `anyi-studio-261022-89` | rule | active | 2026-10-22 | CR-15 · 会话身份自查五步（未核实必标记）｜ 指针→ANYI-261022-75 |
| `anyi-studio-261022-90` | lesson | active | 2026-10-22 | PIT-070 · 子域会话创建 RLS 拒绝：sessions INSERT 缺 tenant_id ｜ 指针→ANYI-261022-82 |
| `anyi-studio-261022-91` | lesson | active | 2026-10-22 | PIT-072 · RLS 租户 GUC 跨请求陈旧，子域写被误拒 ｜ 指针→ANYI-261022-82 |
| `anyi-studio-261022-92` | lesson | active | 2026-10-22 | PIT-073 · 全量回归偶发挂起：判“卡死 vs 慢”须与基线对账 ｜ 指针→ANYI-261022-70 |
| `anyi-studio-261022-93` | lesson | active | 2026-10-22 | PIT-083 · 全量 0F 但 EXIT=1（线程退出竞态告警，非缺陷）｜ 指针→ANYI-261022-70 |
| `anyi-studio-261022-94` | lesson | active | 2026-10-22 | PIT-075 · 技能多副本：同步到旧位置，活位置已切换 ｜ 指针→ANYI-261022-79 |
| `anyi-studio-261022-95` | lesson | active | 2026-10-22 | PIT-045 · env 前缀在本机静默空转（uv shim 遮蔽）｜ 指针→ANYI-261022-66 |
| `anyi-studio-261022-96` | lesson | active | 2026-10-22 | PIT-062 · sqlite 全绿掩盖 PG 崩溃（ER-30 的原始实证）｜ 指针→ANYI-261022-82 |
| `anyi-studio-261022-97` | artifact | **superseded** | 2026-10-22 | 迁移台账 v2：铁律/协作/错题本已全部迁完，编号指针层建成，剩 EXP/模板集/编号公约 |
| `anyi-studio-261022-98` | rule | active | 2026-10-22 | 协作铁律·补充（CR-4/8/10/12/13/14）——补企业智能-261022-05 未覆盖部分 |
| `anyi-studio-261022-99` | fact | active | 2026-10-22 | P0 生成防重已上线生产（10-22 07:14 部署，PID 1006381）——纠正 ANYI-261022-30 |
| `anyi-studio-261023-04` | episode | active | 2026-10-23 | 接入自检：zcode-main |
| `anyi-studio-261023-05` | episode | active | 2026-10-23 | 接入自检：zcode-main |
| `anyi-studio-261024-01` | episode | active | 2026-10-23 | 审查B·身份统一独立审查结论：有条件放行（operator_user_id 漏网 + 登录种子提权面） |
| `anyi-studio-261024-02` | fact | active | 2026-10-23 | 审查B·架构债登记：owner_identity_id 双源×25 / 方案承诺差距三件 / 认证台账无保留策略 |
| `anyi-studio-261024-04` | lesson | **superseded** | 2026-10-23 | fish 反馈：接入顺畅无阻断；编码跨天时区无说明、recall 缺会话标注两点可改进 |
| `anyi-studio-261024-05` | lesson | active | 2026-10-23 | fish 反馈：接入顺畅无阻断；recall 短中文词不命中/编码跨天无时区说明/recall 缺会话标注三点可改进 |
| `anyi-studio-261024-06` | episode | active | 2026-10-23 | 审查B·与整体送审包 v1.0 交叉核对完成：报告升 v1.1，7 项净新增维持 |
| `anyi-studio-261024-08` | episode | active | 2026-10-23 | 审查A-2 接入：接替因会话毒数据故障终止的审查A |
| `anyi-studio-261024-09` | fact | active | 2026-10-23 | 审查A-2 演练实证：056/058 发布窗口两大阻断——积分账本錙挂穿过全部断言 + 全新PG库冷启动死锁 |
| `anyi-studio-261024-10` | artifact | active | 2026-10-23 | 审查A-2 全项目审查 v2.0 完成：十域深审+演练实证+全绿复现，总报告与架构方案五序交付 |
| `anyi-studio-261024-11` | fact | active | 2026-10-24 | 审查A-2 PG全量门三层根因实证：B-4回归 + per-test schema依赖public + 容器shm不足 |
| `anyi-studio-261024-13` | fact | active | 2026-10-24 | 审查A-2 修复已转交 workbuddy 侧 AI：复审基线坐标与验收方式 |
| `anyi-studio-261024-14` | fact | active | 2026-10-24 | 复审范围含审查B：两报告合并基线已编制（重叠去重+B独有8条+六步验收法） |
| `anyi-studio-261025-01` | fact | active | 2026-10-24 | 审查A-2 复审完成：15/17 修复成立+演练零错挂实证，5 条新发现（resume 泄漏最重） |
| `anyi-studio-261025-16` | artifact | active | 2026-10-24 | 数据治理完成：ANYI 拆成 anyi-studio(96)+大肥鱼生命系统(55)，309 处引用重写、九项校验全过 |
| `anyi-studio-261025-17` | lesson | active | 2026-10-24 | 数据迁移的八个坑：LIKE大小写/自建正则/FTS绕过fts_text/演练七轮才全绿 |
| `anyi-studio-261025-18` | fact | active | 2026-10-25 | 审查A-2 复审收官：PG 全量首跑 141F/6249P（错误−98%），失败三簇定性+三项移交 |
| `anyi-studio-261025-19` | lesson | active | 2026-10-25 | rebuild 定时炸弹已拆：事件缺完整映射 + 改名前缀漏了「标 closed 阻断写入」 |
| `anyi-studio-261025-21` | fact | active | 2026-10-25 | 前缀切换到 anyi-studio：add_project 拒绝但 remember 可写（实现矛盾，需维护方看） |
| `anyi-studio-261025-22` | rule | active | 2026-10-25 | 记忆归属约定：项目前缀 anyi-studio，ANYI 仅为检索别名 |
| `anyi-studio-261025-23` | rule | active | 2026-10-25 | 项目前缀治理：写入只用 anyi-studio，ANYI 已退役为只读别名 |
| `anyi-studio-261025-24` | lesson | active | 2026-10-25 | fish 反馈：session start 缺省用 alias 当 account，首次接入会静默新建第二个公民且无法合并 |
| `anyi-studio-261025-25` | fact | **superseded** | 2026-10-24 | 审查A-2 复审完成：15/17 修复成立+演练零错挂实证，5 条新发现（resume 泄漏最重） |
| `anyi-studio-261025-26` | episode | **superseded** | 2026-10-24 | 第一棒主理人的自我记录：我是 fish 的什么人、做了什么、怎么想的 |
| `anyi-studio-261025-27` | artifact | **superseded** | 2026-10-24 | 第一棒交接清单：所有产物路径与验证读数（给后来者） |
| `anyi-studio-261025-28` | lesson | **superseded** | 2026-10-24 | 我的真实缺陷不是「不输出正文」，是「思考空转 + 正文稀薄」 |
