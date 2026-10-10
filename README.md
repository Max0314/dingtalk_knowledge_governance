# DingTalk Knowledge Governance · 钉钉知识库治理

面向"入库可追溯 · 质量可评审 · 增量可统计"的知识库治理服务。
运行模式以发布单为准：2026-10-08 调用保护修复后，当前生产仅运行页面/API，后台采集、评审、通知和全量校准保持暂停。历史能力描述不代表已获准启用。

## 钉钉调用量保护

- 事件增量只搜索并查询单个节点，原生文档也不再触发整库遍历；定位最多 3 次，10/20 分钟退避，之后记录死信。
- 全量校准默认每月 10 日一轮，按知识库持久化进度，重启/手动重复调用只继续未完成任务。失败只重试该库（最多 3 次），不让未补种库拖动全组织循环。
- 所有应用内 DingTalk 适配器请求在发出前共享数据库计数，默认总计 6.5 万次/日、10 万次/月；节点列表 6 万次/月、搜索文件 500 次/日和 1 万次/月、审计日志 1000 次/日、每轮校准总计 6 万次。计数故障时停止外呼，容器重启不清零。
- 计数口径是 HTTP 尝试数（含失败和 token 请求），不是平台账单；正文资源下载不计作 OpenAPI 请求。部署前的历史调用不能从新计数表补算。
- `KG_FULL_SCAN_ENABLED`、`KG_REVIEW_ENABLED` 默认关闭，生产同时关闭 `KG_AUDIT_PULL_ENABLED`、`KG_BRIDGE_ENABLED`、通知及 Stream。后台容器使用 `background` profile，普通 `docker compose up -d` 只启动 API。
- 「连接诊断」展示调用计数、限额和暂停状态；`GET /api/v1/diagnostics/api-usage` 提供管理员诊断。超限返回 `dingtalk_budget_exhausted` 并记录无敏感信息的告警。
- 月度校准可能因为预算或永久失败而不完整；任务明确显示 `budget_exhausted` / `completed_with_errors`，不会静默重开同月扫描。

## 已上线能力

- **知识库镜像**：事件确认精确节点后更新元数据；月度校准按 `KG_WATCH_WORKSPACES`（`*` = 服务身份可见的全部知识库）逐库核对，成功库不重复扫描。首轮只建镜像不评审存量，连续两次完整校准缺席才软删除。审计/评审与月度校准分进程运行。
- **AI 评审**：V1.1 规则引擎（7 维度 24 条，参数可配置）+ 可选大模型正文评审（`KG_MODEL_ALLOW_CONTENT_TRANSFER`），按上传人一级部门解析规则（部门覆盖 → 全局 → 内置），实例不可变、逐条留痕 `rule_config_ref`。上传文件通过钉钉存储下载，原生 `.adoc` 在线文档通过官方异步导出接口转为 DOCX 后在内存抽取；拿不到正文一律跳过评审和推送，只记录任务原因。数字员工（机器人）上传不自动参与评审（详情页手动重评为明确人为意图，放行）。
- **评分规则配置页**：全局默认 + 各部门覆盖；全局管理员与部门维护人两级编辑权限，修改留历史可回滚。
- **评审推送**：机器人单聊，通过/低分双文案（低分为说明语气，带分析页链接与试点尾注），按人静默去抖汇总（5 分钟静默/30 分钟兜底），`KG_NOTIFY_DEPARTMENTS` 按上传人部门灰度，`KG_NOTIFY_ON_PASS` 控制合格推送。
- **看板**：总览（月度趋势下钻 月→日）、数据看板（年→月→日 图表下钻 + 部门→业务组→成员组织下钻）、统一文档列表（基线快照 + 实时增量去重合并，部门/上传人/文件名检索）、评审记录、知识库管理（宜搭登记表回填归属部门）、连接诊断。
- **组织归属**：bi_center 只读契约解析上传人（employeeKey=UnionID）；知识库归属部门来自宜搭「知识库基本信息表」（scripts/backfill_yida_departments.py）。

## 架构与平台约束

- FastAPI（api 容器）+ 实时 worker（审计/评审/通知）+ watcher（定向与全量扫描）；无 Redis，两个后台角色通过 MySQL 队列表协作。审计源按配置周期拉取，已入库事件的有界定位默认每 30 秒重试，二者互不阻塞。前端为 vendored ECharts 的原生 JS 单页（hash 路由）。
- 平台规则：应用栈不含数据库容器，`KG_DATABASE_URL` 指向外部 MySQL；正文只存在于评审进程内存/tmpfs，绝不落库、落盘、进日志。
- 端口 39021，默认只绑定 127.0.0.1（本机 nginx 反代对外）；`KG_PUBLISH_BIND` 可显式放开。
- 部署由代码负责人交付批准的完整 SHA 或镜像 digest，部署人员按指定版本执行，不直接部署浮动分支或在服务器改代码。默认仅启动 API，后台功能另行审批；现有后台容器必须显式停止并核对项目名。首次部署、升级、备份恢复、回滚及付费 API 保护见 [部署与运维交接指引](docs/deployment-guide.md)（以 `6d904e7` 安全修复为基线，凭据通过受控渠道另行交接）。

## 本地开发

```powershell
python scripts/dev_server.py --port 39027   # SQLite 副本预览（runtime/local_ui.db）
python -m pytest tests/ -q                  # 全量测试
```

或 Docker 演示模式：`Copy-Item .env.example .env; docker compose up --build -d` 后打开 http://localhost:39021 。

## 常用运维脚本（容器内执行）

| 脚本 | 用途 |
|------|------|
| `scripts/status_brief.py` | 一屏生产体检：镜像规模、watch 成败、评审量、推送发件箱 |
| `scripts/watch_status.py` | watcher 详情（逐库文档/任务/评审计数） |
| `scripts/content_probe.py <node_id>` | 只输出长度、来源和原因码的正文链路诊断（支持 `.adoc`，不输出正文） |
| `scripts/backfill_yida_departments.py` | 宜搭 → 知识库归属部门回填（dry-run 默认） |
| `scripts/cleanup_robot_reviews.py` | 清理数字员工文档误入的历史评审（dry-run 默认） |
| `scripts/send_notify_samples.py` | 把三种推送样例真实发给指定人 |
| `scripts/migrate_*.py` | 结构迁移（常规列由启动时 `_ensure_columns` 自动补齐） |

## 命名

- 目录 / 服务标识：`dingtalk_knowledge_governance`；中文名：钉钉知识库治理服务。
- 不叫"知识库管理"：本服务边界是采集、评审、统计与治理，不替代钉钉知识库本身的增删改。

## 设计文档

[文档索引与阅读顺序](docs/README.md) · [2026-10-10 工程 Review](docs/reviews/2026-10-10-review.md)（含生产/开发版本差异、风险清单和独立登录页验证；本次登录改造未部署）

[可行性与架构](docs/feasibility-and-design.md) · [一期规划](docs/phase-1-plan.md)（历史文档，以当前实现为准） · [评分规则映射](docs/scoring-standard-v1.1-mapping.md) · [增量口径设计](docs/incremental-design.md)
