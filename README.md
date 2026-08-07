# DingTalk Knowledge Governance

## Quick start

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

Open `http://localhost:39021`. If Docker is not installed, run a local SQLite demonstration:

```powershell
$env:KG_DATABASE_URL='sqlite:///./runtime/knowledge_governance.db'
$env:KG_DEMO_MODE='true'
python -m uvicorn app.main:app --port 39021
```

Docker Compose runs the API and the review worker only. Per platform rules the
stack contains no database container: `KG_DATABASE_URL` must point at the
external MySQL server. There is no Redis — the worker polls the database. API
and worker use `/tmp` tmpfs and do not mount a persistent document volume.

Server deployment: see [docs/deployment-guide.md](docs/deployment-guide.md)
(sanitized; live credentials stay in the local ops manual outside this repo).

## Implemented capabilities

- Read-only DingTalk workspace/node adapters, incremental sync batches, and connection diagnostics.
- Targeted workspace watcher (`KG_WATCH_WORKSPACES`): periodic complete walks of pilot workspaces that seed silently, queue reviews for new/changed files, and soft-delete files missing for consecutive walks.
- Document metadata, ingestion time, monthly counts, new/changed increments, immutable review instances, and derived rerun counts.
- Read-only `bi_center` identity contract adapter using `employeeKey=UnionID`.
- Explainable deductions based on the provided V1.1 rule; see [scoring mapping](docs/scoring-standard-v1.1-mapping.md).
- Model configuration, environment-only API-key references, connectivity checks, and governance UI.

When DingTalk, bi_center, or model credentials have not been injected, diagnostics explicitly return `not_configured`; the service never fabricates connectivity or persists document body content.

钉钉知识库治理服务的设计起点，面向“入库可追溯、质量可评审、增量可统计”的管理诉求。

## 命名

- 目录 / 服务标识：`dingtalk_knowledge_governance`
- 中文名：钉钉知识库治理服务
- 不命名为“知识库管理”：该名称容易与钉钉知识库本身的创建、编辑、删除能力混淆；本服务的边界是采集、评审、统计与治理，不替代钉钉。

## 当前结论

可行，但应拆成两个能力层：

1. **只读治理层（建议先做）**：采集知识库、节点和文件元数据，统计文件数量、入库时间与新增/变更/删除增量；保存自身审计快照。
2. **入库质量门禁（后续验证）**：在文件进入钉钉前提取正文并评分，只有通过后才由本服务执行入库。若继续使用钉钉原生入口直接上传，则只能“入库后评审”，不能在入库前阻断。

钉钉节点元数据可提供创建时间、修改时间、创建人、文件大小和可选字数统计；其中没有“知识库评审分数”字段。因此评分必须由本服务定义、计算并持久化，不能把钉钉当作评分数据源。

完整的可行性判断、架构、数据模型、风险和 POC 验收项见 [docs/feasibility-and-design.md](docs/feasibility-and-design.md)。

结合 Docker Compose、正文临时处理、`bi_center` 组织契约、管理角色、评分实例和 UI 的一期实施规划见 [docs/phase-1-plan.md](docs/phase-1-plan.md)。

## 本阶段产物

本目录目前只包含设计材料，未配置密钥、未调用钉钉生产接口、也未读取任何真实知识库文件。
