# 2026-11-04 元数据恢复与统计推送发布

后续采集大表读取与传输异常修复的当前版本见 [采集读取发布记录](2026-11-04-collector-bounded-reads.md)，本文保留首轮上线对账。

## 范围与状态

本次双端实现、合并、生产部署及统计对账已完成。AI自动评审、正文读取、模型传输、通知和Stream继续关闭，原阿里云知识库保持停止；完整补采按用户后续要求暂不执行。

两端从最新已合并主线开始：知识库134964f、BI673915b。知识库旧导出分支的有效逻辑已由当前主线取代，先完成祖先合并；BI既有其他开发工作区不改动。正式统计发送必须等双端提交、合并、部署和健康检查后才打开。

## 已执行验证

- 知识库27项定向测试通过：基线去重、北京时间、稀疏增量、无变化不写、空闲不读文件大表/员工JSON、恢复基线固定、审计配对游标、元数据完成证明、暂停评审通知、预算和恢复幂等、冻结目标真实采集证明。
- BI接收、身份兼容与看板定向35项通过；后续12项覆盖非法增量结构和前端版本检查通过。compileall、前端node检查和diff检查通过。
- BI项目全量检查已运行，5个禅道SQLite缺少IF函数报错已在未修改673915b主线复现。资源版本相关旧断言随本次升级同步更新，最后branding目标检查通过。未把本地检查表述为生产验收。
- 数据服务器实测binlog_row_image=FULL，因此双端均将大报文与可变状态分表；租约、失败次数、心跳不更新大JSON。

## 发布顺序与命令

1. 固定双端提交；备份现有配置/镜像，保持发送、采集关闭。知识库执行 `python scripts/migrate_bi_push.py`，BI执行 `python scripts/migrate_knowledge_push.py`，均为新增表/可空列。
2. 双端启动新API检查健康、版本和拒绝未授权推送；不传真实统计。
3. 知识库设置KG_METADATA_ONLY=true，KG_CONTENT_EXTRACT_ENABLED=false，review/notify/model_allow_content_transfer/stream均false。在采集仍暂停时执行 `python -m app.bi_publisher seed`，500节点主键批次，单次可续播种。
4. 双端独立推送密钥准备完毕、检查通过后，BI启用push接收及push来源，知识库启动publisher（statistics profile）。首帧基线只发送一次，后续真实变化分区增量。
5. `python -m app.scan_recovery --recovery-id <固定UUID> --reason migration-recovery` 只做一次预算预估和持久准备；批准预算内启动元数据worker/watcher。失败库接续，不重扫成功库；冻结清单全部真实成功才标完整。
6. 对账源/接收sequence、hash、六个月及24月看板版本与核心计数，记录采集剩余/失败、审计缺口、调用量和资源；评审/通知前后不得消费或新增。

受控回滚：先停publisher/worker/watcher，恢复原配置及固定API镜像；BI保留最后发布看板和新增表。不清空quota、不重置binlog、不重跑原始表全量构建。

## 生产验收

首个575,301字节基线已接收，sequence=1，源/接收hash一致，六个月预计算看板已发布。初始投影160,168份文件，空闲不重扫。

用户随后决定：将本轮和本月节点限额调至65,000，但暂不补采。149库预估64,685次（含20%余量），此前60,000额度拦截生效；未创建recovery、未启动watcher或目录遍历，不重复执行预算大表估算。历史库存缺口不能宣称补齐。

源有181条旧审计待处理；增加metadataOnly独立24小时事件资格门禁，过期记录有界终态metadata_event_expired并保留，不能改Document或触发搜索/batchQuery。新增回归验证旧删除/恢复/改名/上传均零外呼、文档不变，元数据与quota17项通过，独立复审无阻断。

BI预计算模式的维护任务已移至实际后台角色启动，避免web子进程关闭后台开关后漏跑。浏览器自动化因工具无法可靠确定当前URL而被停止，未继续操作浏览器；实际页面展示尚未完成目视验收，接口及预计算结果另行对账。

### 固定版本与实测

| 项目 | 知识库 | bi_center |
| --- | --- | --- |
| 运行代码SHA | af469dbda385816b92ad9f6aa1b80a89472765f8 | d64c5273591a56ec226ac485a6a857e4bbb44257 |
| 镜像ID | cb65deb9423ca51d68fc032f542171c6314d0364e4fabd17ee5f5155ed77439d | 6dda319a33a9bc2881e86b24fc973431744b7cc4cb5769c9cdeb01cfce89a78e |
| 进程 | API healthy、worker/publisher running；watcher未启动 | 既有bi_center_neoflow healthy；后台维护在实际worker角色执行 |
| 入口检查 | /api/ready=200 | /api/health/ready=200 |

- 功能分支均为main祖先；知识库GitHub、BI GitHub/GitLab已同步。原用户AGENTS及其他工作区未提交资料保持原样。后续验收补记为文档提交，不替换上述已验证镜像。
- 基线sequence=1，575,301字节，一次确认；首个真实增量sequence=2，8,209字节，一次确认，只更新8个变化分区。接收后stateHash=`c2dc47974221adc47aa7be7e38d5e291fd49651d49db9c74667561358830b25f`与源确认manifest一致。
- 六个月与24个月实际预计算结果及HTTP响应组装均200，采用sequence=2及相同orgVersionHash；summary与逐月上传/日常/批量/评审结论逐项相等，浏览器响应无employeeKey。24个月另执行一次定点预计算验收，常态仍沿用低频调度。
- 范围125个正式统计知识库，文件155,009份，本月新增9份，最新有效历史评审1,431份；新增9份来自恢复后的元数据审计，不是全量补采结果。
- 审计窗口已完成到2026-11-04 03:01:13 UTC，lastAuditSuccessAt为03:01:22 UTC；181条旧事件保留为metadata_event_expired；8月31日至恢复窗口的历史缺口明确保留，inventoryReconciledAt未推进。
- 评审实例2,535、评审任务2,923、通知2,519均与发布前一致；任务succeeded=2,533/skipped=390，通知sent=58/skipped=2,461，状态数量无变化。metadata_only=true，review/notify/content/model_transfer/stream均false。
- 本轮与节点月限额65,000已生效；full_scan=false、recovery记录0、watcher未启动，149库完整补采待用户后续指令。
- 初始播种观测CPU35.31%、57.13MiB/384MiB；稳定publisher约0%CPU、56.8MiB，API约0.1%CPU、71.8MiB。播种时数据库运行线程2。空闲定向测试确认不读原始文件表和员工分区JSON，生产空闲日志changed=0/months=0/queued=false；不把单次观测表述为长期峰值。
- 变更前原始配置备份：知识库 `/var/backups/knowledge-governance/release-20261104T104342`；BI `/home/max/backups/bi_center/kg-push-20261104T104512`，均受限权限。原API镜像和旧完整数据库备份保留；本次新增迁移幂等执行，不覆盖原库。
