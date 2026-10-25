# 审查B·身份统一独立审查结论：有条件放行（operator_user_id 漏网 + 登录种子提权面）

> 编码：anyi-studio-261024-01 ｜ 类型：episode ｜ 记录于 2026-10-23T16:15:59Z

审查B 对 anyi-studio 身份统一重构（main@e9d8e0e）完成独立审查，报告：I:\app\anyi-studio-审查\anyi-studio_独立审查报告_审查B_v1.0_20261023.md。结论：送审报告与代码实况高度一致（迁移/鉴权/资金键/门禁逐条核实；precheck EXIT=0、audit_gate 533/533），但判定**有条件放行**——三个进窗口前阻塞项：A1 orders.operator_user_id 漏网（不在 056/058 清单，team_consumption_stats.py:156 有 JOIN identity 读点，order_attribution.py:75 等三处注释假称已归一）；A2 审计留痕列混域约 11 处（settle_op_user_id/actor_user_id/submitted_by 等，切换前 raw）；A3 按人计数验收需扩到资金 3 列（points_accounts/balances/point_transactions，域断言对 6↔7 恒真抓不到存量错行）。另发现与窗口无关的多租户提权面：登录幂等种子 ensure_identity_for_user(user, tenant_id=1) 会把跨租户聚合角色（无 status 过滤）种进官方租户 1（auth_service.py:789 + _verify_credentials 子查询），当前靠「t1 有 active owner 挡部分唯一索引」+「外站无 admin」两个数据巧合缓解，建议同批改 role='user'（对齐 bridge_service.py:874 的正确写法）。

