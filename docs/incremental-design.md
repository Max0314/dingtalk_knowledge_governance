# 增量更新设计（基于 2026-08-06/07 实证）

**状态：** 架构已验证，事件通道运行中，对账扫描器已具备，主基线切换待决策。

## 1. 实证结论：为什么不能靠时间戳轮询

在真实知识库上的对照实验（个人库写操作 + 离线滞后分析）：

| 操作 | 空间 updateTime | 目录 mtime 传播 |
| --- | --- | --- |
| 原生文档创建（adoc） | ✅ 即时推高 | ✗ |
| **二进制上传（docx/pdf 等，占绝对多数）** | ✗ 不推 | ✗ |
| 内容修改 | ✗（实测滞后可达 20 天） | ✗ |
| 建目录 / 移动 / 删除 | ✗ | ✗ |

结论：不存在可靠的廉价"脏标记"。增量必须依靠**事件推送 + 操作审计 + 周期对账**三支柱。

## 2. 三支柱架构

```
支柱一  存储事件（Stream 推送，已上线）
        POST /v1.0/storage/events/subscribe  scope=ORG 一次订阅全企业
        事件: storage_dentry_create / update / delete
        字段: dentryId, spaceId, unionId(操作人), extension, type
        覆盖: 钉盘与二进制文件为主；配额零消耗；落 stream_events 表
        限制: 知识库原生文档操作不触发（实测）

支柱二  文件操作行为 API（专属钉钉，开通审批中）
        GET /v1.0/exclusive/fileAuditLogs  游标分页(nextGmtCreate+nextBizId)
        官方示例明确覆盖知识库文档的新建/删除；含操作人/动作/文件名/IP
        角色: 准实时增量拉取 + 历史留痕；成本 O(变化数)
        前置: 管理后台开启文件审计（员工告知 + 协议 + 钉钉审核）

支柱三  全量对账扫描（已具备，scan_uploader_baseline.py）
        节点分页 100/条（实测上限，文档写 30 是假的）
        135 库全量 = 47,980 次调用 = 月配额(550万)的 0.87%
        判定: node_id 首见=新增 / updated_at 变化=修改 /
              parent+name 变化=移动改名 / 连续两轮缺席=删除(软删)
        node_id 跨新旧命名空间同值——快照间可直接对账
        频率: 每周一次绰绰有余；看门狗自愈，断点续跑
```

## 3. 数据模型（均在 MySQL knowledge_governance）

- `historical_snapshots`：每次全量一个不可变快照；`definition.is_primary_baseline`
  锁定头条口径；`definition.scan_stats` 记录运行统计
- `historical_file_nodes`：节点全量（文件+目录），含父指针/创建人/修改人/
  URL/大小/分类/时间；id 列 utf8mb4_bin（大小写敏感）
- `uploader_month_stats`：(人×库×月) 预聚合，看板唯一读源
- `stream_events`：事件原始留痕（processed 标记待流水线消费）
- `employee_map`：bi_center 身份缓存（7 天 TTL）；机器人账号由
  `KG_ROBOT_USER_IDS` 显式标记

## 4. 口径规则

- 总量全计（含机器人/批量导入），排行与考核视图按开关剔除
- 月份按 source createTime（Asia/Shanghai）归属
- 删除仅由对账确认（软删，保留历史）；回收站 30 天窗口
- 头条增量当前锁定 08-05 主基线（44 库）；**切换到 135 库新基线需业务拍板**
  （切换即一行标记，批量日检测自动按新基线重算）

## 5. 待办

1. 事件→镜像消费流水线（dedupe by eventId → dentry 反查 → 更新镜像 → 触发评审）
2. 支柱二审批通过后：增量拉取器 + 与事件流互校
3. 周期对账排产（建议每周日凌晨，看门狗模式）
4. bi_center 数字员工身份策略反馈（当前 official=true 需上游修正）
