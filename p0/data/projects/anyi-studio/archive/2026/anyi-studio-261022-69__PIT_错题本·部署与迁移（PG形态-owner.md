# PIT 错题本·部署与迁移（PG形态/owner/chown/停服窗）16 条

> 编码：anyi-studio-261022-69 ｜ 类型：lesson ｜ 记录于 2026-10-22T05:42:30Z

# PIT 错题本·部署与迁移

> 权威源：`notes/error-ledger.md`。场景：上生产、跑迁移、重启服务、改部署脚本。

## 本质

本地 sqlite 全绿 → 生产 PG 爆炸，是本项目最高频的“假安全”。根因不是代码差，而是**两种库的行为差（方言/RLS/权限/owner）在本地结构性测不到**（ER-30）。

## 何时该想起

写新迁移、改部署脚本、重启服务、新建表、改 RLS、动权限。

## 关键判据（按顺序自查）

1. **谁跑 DDL**：应用启动用非 owner 角色 → DDL 被幂等跳过，**索引永不落地**（PIT-099）。迁移必须停服窗用 migrator 手跑，并以 `pg_proc`/`pg_indexes`/`information_schema` **计数验收**（不看日志）。
2. **建完要授权**：用 `postgres` 建表 → owner=postgres 且零授权 → 应用 500（PIT-088）。
3. **传统后要 chown**：rsync 后漏 `chown -R deploy:deploy` → 启动即崩（PIT-098，不可用 ~100s）。
4. **重启要看 PID**：只“启动”不“停旧” = 假重启，health 200 由旧实例提供（PIT-054）。必须断言新 PID ≠ 旧 PID。
5. **停服先拦在途**：未拦 processing 任务 → 恢复后僵尸态（PIT-046）。
6. **方言三雷**：BOOLEAN 列 vs bigint 0/1（PIT-089）；`COALESCE(text,0)` 静态推断必炸（PIT-092）；uvloop 启动路径单测不可达（PIT-061）。
7. **迁移文件不能有纯注释片段**：psql 会掩盖、asyncpg 会暴露（PIT-094，生产不可用 ~2 分钟）。
8. **双形态登记**：新增 PG 迁移必须同时登记 sqlite 侧（PIT-066），否则生产建表缺失。
9. **部署脚本四缺陷族**：顺序/断言脆弱/源选择/兜底参数（PIT-095）；远程 SFTP 大文件断连 + 续传同名陷阱 + 引号转义（PIT-068）。
10. **RLS**：tenant GUC 跨请求陈旧会误拒子域写（PIT-072）；跨租户写入在联调期直接 500（PIT-097）。

## 条目索引

019 dist 构建滞后｜046 停服未拦在途｜054 假重启｜061 uvloop｜066 双形态登记｜068 远程部署三坑｜070 子域 INSERT 缺 tenant｜072 GUC 跨请求陈旧｜088 建表 owner/授权｜089 BOOLEAN vs bigint｜092 COALESCE 类型｜094 纯注释片段｜095 部署脚本四缺陷｜097 RLS 跨租户写｜098 漏 chown｜099 迁移登记≠建成

