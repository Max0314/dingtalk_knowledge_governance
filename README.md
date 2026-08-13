# DingTalk Knowledge Governance · 钉钉知识库治理

面向"入库可追溯 · 质量可评审 · 增量可统计"的知识库治理服务。
2026-08-13 起正式运行：全库监控 + AI 评审 + 部门灰度推送。

## 已上线能力

- **全库实时镜像**：watcher 按 `KG_WATCH_WORKSPACES`（`*` = 服务身份可见的全部知识库）分片轮巡；首轮只建镜像不评审存量，之后新增/变更自动入评审队列，连续缺席软删除。分片间穿插评审任务、推送与审计拉取，长周期不饿死其他后台能力。
- **AI 评审**：V1.1 规则引擎（7 维度 24 条，参数可配置）+ 可选大模型正文评审（`KG_MODEL_ALLOW_CONTENT_TRANSFER`），按上传人一级部门解析规则（部门覆盖 → 全局 → 内置），实例不可变、逐条留痕 `rule_config_ref` 与正文不可用原因 `content_note`。数字员工（机器人）上传不参与评审。
- **评分规则配置页**：全局默认 + 各部门覆盖；全局管理员与部门维护人两级编辑权限，修改留历史可回滚。
- **评审推送**：机器人单聊，通过/低分双文案（低分为说明语气，带分析页链接与试点尾注），按人静默去抖汇总（5 分钟静默/30 分钟兜底），`KG_NOTIFY_DEPARTMENTS` 按上传人部门灰度，`KG_NOTIFY_ON_PASS` 控制合格推送。
- **看板**：总览（月度趋势下钻 月→日）、数据看板（年→月→日 图表下钻 + 部门→业务组→成员组织下钻）、统一文档列表（基线快照 + 实时增量去重合并，部门/上传人/文件名检索）、评审记录、知识库管理（宜搭登记表回填归属部门）、连接诊断。
- **组织归属**：bi_center 只读契约解析上传人（employeeKey=UnionID）；知识库归属部门来自宜搭「知识库基本信息表」（scripts/backfill_yida_departments.py）。

## 架构与平台约束

- FastAPI（api 容器）+ 单 worker 轮询（无 Redis，队列即数据库表）；前端为 vendored ECharts 的原生 JS 单页（hash 路由）。
- 平台规则：应用栈不含数据库容器，`KG_DATABASE_URL` 指向外部 MySQL；正文只存在于评审进程内存/tmpfs，绝不落库、落盘、进日志。
- 端口 39021，默认只绑定 127.0.0.1（本机 nginx 反代对外）；`KG_PUBLISH_BIND` 可显式放开。
- 服务器访问 GitHub 受限，部署走服务器裸仓库双推：`git push origin main && git push neoflow main`，服务器 `git pull && docker compose up --build -d`。详见 [docs/deployment-guide.md](docs/deployment-guide.md)（脱敏版；真实凭据在仓库外的运维手册）。

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
| `scripts/backfill_yida_departments.py` | 宜搭 → 知识库归属部门回填（dry-run 默认） |
| `scripts/cleanup_robot_reviews.py` | 清理数字员工文档误入的历史评审（dry-run 默认） |
| `scripts/send_notify_samples.py` | 把三种推送样例真实发给指定人 |
| `scripts/migrate_*.py` | 结构迁移（常规列由启动时 `_ensure_columns` 自动补齐） |

## 命名

- 目录 / 服务标识：`dingtalk_knowledge_governance`；中文名：钉钉知识库治理服务。
- 不叫"知识库管理"：本服务边界是采集、评审、统计与治理，不替代钉钉知识库本身的增删改。

## 设计文档

[可行性与架构](docs/feasibility-and-design.md) · [一期规划](docs/phase-1-plan.md)（历史文档，以当前实现为准） · [评分规则映射](docs/scoring-standard-v1.1-mapping.md) · [增量口径设计](docs/incremental-design.md)
