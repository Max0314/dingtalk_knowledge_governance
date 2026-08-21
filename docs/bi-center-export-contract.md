# BI Center 知识库上传事实导出契约 V1

## 目标和边界

本接口供 `bi_center` 定时拉取知识库治理服务的**上传事实**，用于个人、部门、业务组和知识库的月度看板。

- 接口只读；不会触发钉钉请求、评审、同步或正文读取。
- 不返回正文、附件、文档标题、文档 URL、源 UserID、姓名、部门或业务组。
- 员工明细只返回 `matched && includeInOfficialStats=true` 的 `employeeKey=UnionID`；`bi_center` 必须按请求月份使用自己的组织快照归属部门和业务组。
- V1 的度量范围是上传文件。评审完成、人工审核、预览和下载行为不在本版本中，不能据此推导“访问最频繁的知识库”。

“常用知识库”在 V1 中的准确含义是指定周期内**上传文件最多的知识库**。BI 将员工×知识库×月事实累加后，按 `uploadedFileCount` 降序即可得到 Top N。

## 配置与鉴权

```dotenv
KG_BI_EXPORT_ENABLED=true
KG_BI_EXPORT_API_KEYS=<独立随机密钥，可用逗号配置轮换密钥>
KG_BI_EXPORT_MAX_PAGE_SIZE=200
```

调用方必须带 `X-API-Key`。该密钥与 `BI_CENTER_INTERNAL_TOKEN` 相互独立：后者仅用于知识库服务调用 `bi_center` 的员工目录接口。

导出路由不需要浏览器 Cookie，但每个路由均强制校验 API Key。关闭功能返回 `404 export_disabled`；未配置密钥返回 `503 export_not_configured`；密钥错误返回 `401 unauthorized`。

## 路由

| 路由 | 说明 |
| --- | --- |
| `GET /api/export/v1/knowledge-governance/latest` | 可拉取月份和来源快照状态。 |
| `GET /api/export/v1/knowledge-governance/monthly-summary?month=YYYY-MM` | 一个自然月的全量观测、正式员工和排除诊断汇总。 |
| `GET /api/export/v1/knowledge-governance/monthly-employees?month=YYYY-MM&page=1&pageSize=200` | 正式员工×月上传事实。 |
| `GET /api/export/v1/knowledge-governance/monthly-employee-workspaces?month=YYYY-MM&page=1&pageSize=200` | 正式员工×知识库×月上传事实。 |

月份必须为 `YYYY-MM`，最大页长受 `KG_BI_EXPORT_MAX_PAGE_SIZE` 限制（上限 500）。分页响应遵循硬件平台既有形式：`{ok,data,pagination,meta}`。

## 响应示例

```json
{
  "ok": true,
  "data": [
    {
      "month": "2026-08",
      "employeeKey": "union-id",
      "workspaceId": "workspace-id",
      "workspaceName": "研发知识库",
      "uploadedFileCount": 12
    }
  ],
  "pagination": {"page": 1, "pageSize": 200, "total": 1, "totalPages": 1},
  "meta": {
    "contractVersion": 1,
    "timezone": "Asia/Shanghai",
    "sourceSnapshotId": "uploader-baseline-id",
    "dataStatus": "live_derived",
    "asOf": "2026-08-21T10:00:00+00:00"
  }
}
```

`monthly-employees` 的事实字段为 `month`、`employeeKey`、`uploadedFileCount`、`workspaceCount`。同一员工存在多个钉钉源账号时，会先按同一个 `employeeKey` 合并。机器人、未匹配、非正式统计人员不会出现在明细，但会计入 `monthly-summary.data.diagnostics` 的排除计数。

## 数据来源和一致性

V1 从已有 `uploader_month_stats` 预聚合表读取，不扫描正文、文档列表或评审历史；`sourceSnapshotId` 明示当前统计来源。`dataStatus=live_derived` 表示该接口尚未实施月度冻结，补扫或重新归属可能导致历史月重新计算。

`bi_center` 应按如下方式使用：

1. 先拉取 `latest`，确定可用月份和当前 `sourceSnapshotId`。
2. 逐页拉取两个员工事实接口，将结果写入自己的本地同步表；不在页面请求路径直接依赖远端接口。
3. 以 `employeeKey` 和该事实的 `month` 连接 `bi_center` 月度员工目录；不得使用知识库服务缓存的部门字段。
4. 同步失败时保留上一次成功事实，并向看板暴露 `asOf` 和数据陈旧状态。

将来引入评审质量事实和月度冻结时，必须新增版本化字段或 V2 路由，不能改变 V1 上传字段的含义。
