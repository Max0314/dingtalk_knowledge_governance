# 增量更新设计（基于 2026-08-06/07 实证）

**状态：** 定向监控 watcher 已实现（试点：陈鹏列个人库）；Stream 事件通道对知识库上传**已证伪**；对账扫描器已具备；主基线切换待决策。

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
支柱一  定向监控 watcher（已实现，app/service.py watch_*）
        原支柱一 Stream 存储事件 2026-08-07 实测证伪：知识库上传不触发
        storage 事件（stream.py 降级为纯诊断通道）。
        替代机制: worker 每 KG_WATCH_INTERVAL_SECONDS(默认300s) 对
        KG_WATCH_WORKSPACES（id/名称/名称片段，1h 缓存解析）做全量遍历：
        首轮=播种不评审(mode watch_seed)；后续 首见=watch 评审、
        updated_at 变化=watch_change 重评、连续 KG_WATCH_DELETE_MISSES
        轮缺席=软删、复现=撤销软删。
        试点实证(2026-08-07, I-陈鹏列 1001 节点): 新增秒级可靠——两份
        adoc 测试文档与一份计划外二进制 .doc 均被自动发现并评审；但
        adoc 内容修改在 3 分钟内未推高节点 updated_at（与既有滞后观察
        一致）→ 修改重评不能依赖时间戳，待支柱二审计日志或内容指纹。
        实现注意: 新命名空间 creatorId 是数字 userId 而非 UnionID；
        workspace_detail 对个人/团队空间 404，须用列表页 rootNodeId；
        列表可能重复吐节点，单轮按 node_id 去重。
        成本: 个人库(~500节点)每轮 ~10 次调用，5 分钟档月耗 <0.1% 配额；
        扩到重点库按目录数线性增长，全 135 库请用支柱三节奏。
        可选增强: 库自动化「节点创建时→AI表格新增记录」（目标多维表必须
        位于库内，逐库配置）或宜搭表单登记（读取链路待验证）。

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

1. ~~事件→镜像消费流水线~~（Stream 证伪作废，由定向监控 watcher 取代，2026-08-07 已实现）
2. 正文获取与二进制解析（docx/pdf/xlsx → tmpfs 临时抽取），评审从元数据档升级到内容档
3. 支柱二审批通过后：增量拉取器 + 与 watcher 互校
4. 周期对账排产（建议每周日凌晨，看门狗模式）
5. bi_center 数字员工身份策略反馈（当前 official=true 需上游修正）
6. 分类评审策略（file_class：文档/表格/图片/工程残留 → 评审口径矩阵）
