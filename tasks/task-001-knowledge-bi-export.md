# Task 001 · 知识库治理 BI 只读导出

## 交付

- worktree：`D:\code_CPL\.codex-worktrees\knowledge-bi-export`
- 分支：`codex/knowledge-bi-export`
- 基线：`origin/main` / `bcc78be8911388f5286d3797f3e06399339f6867`
- 目标：实现供 `bi_center` 拉取的只读、版本化、API Key 鉴权导出接口，覆盖月度汇总、员工×月及员工×知识库×月上传事实；不输出正文、文档标题、URL、附件或未脱敏人员资料。

## 契约范围

- `GET /api/export/v1/knowledge-governance/latest`
- `GET /api/export/v1/knowledge-governance/monthly-summary?month=YYYY-MM`
- `GET /api/export/v1/knowledge-governance/monthly-employees?month=YYYY-MM&page=1&pageSize=200`
- `GET /api/export/v1/knowledge-governance/monthly-employee-workspaces?month=YYYY-MM&page=1&pageSize=200`

认证采用独立 `X-API-Key`，且即使 Web Cookie 登录保护开启也必须经过该认证。对外统计只使用 `matched && includeInOfficialStats` 的正式员工 `employeeKey`；BI 侧仍按自己的月度组织快照完成部门归属。

## 验收

- 接口禁用、未配置密钥、错误密钥、非法月份与分页均有稳定 JSON 响应。
- 测试覆盖员工和知识库月度上传统计、机器人/未匹配排除、同一 UnionID 聚合、无正文泄露。
- 运行 `pytest`、`compileall` 与 Docker Compose 配置检查。

## 交付状态

已提交未推送：`feat: add BI upload export API`。

已验证：

- `python -m pytest tests/test_bi_export.py -q`：4 passed。
- `python -m compileall -q app tests`：通过。
- `python -m pytest tests -q`：135 passed；3 个既有 `tests/test_content.py` 用例失败，原因是测试在 `autoflush=False` 的会话中新增 `Document` 后立即 `db.get()`，未 flush 即取回，和本任务无关。
- `docker compose config --quiet`：本机未安装 Docker，无法执行。
