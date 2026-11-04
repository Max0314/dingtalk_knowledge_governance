# 2026-11-04 采集有界读取与传输异常修复

## 范围

按用户授权修复采集端两处反复读取文档大表，提交、合并、部署并验证。增量审计和推送继续运行；全量补采、watcher、AI评审、正文、通知和Stream继续关闭，本轮/目录节点月限额保持65,000。不改变BI协议或组织统计口径，不修改bi_center代码。

此前“空闲不读文档大表”的验证只覆盖统计publisher，未覆盖采集bridge。本次生产只读EXPLAIN确认：名称查询为documents全表扫描（估计约20万行），受管库DISTINCT读取文档索引；删除/恢复的下载键查询同样未命中索引。

## 实现

- 生产不运行本地标题provisional查询，不加载历史名称候选；确切nodeId仍由远端搜索、节点时间/创建人等互证，或已确认的同bizId生命周期提供。同名、旧候选和A/A/B冲突不自动确认。旧候选仅保留演示诊断。
- 生产不为被禁用的树遍历回退查询受管库存；诊断/演示的集合改为Workspace加workspace_id索引EXISTS，不对文档库存做DISTINCT。
- 新增非唯一下载键索引；重复下载键不任选节点。迁移幂等，MySQL显式ALGORITHM=INPLACE、LOCK=NONE，lock_wait_timeout=3秒，失败中止，不改写表数据、不降级阻塞建索引。旧镜像可忽略该索引。
- 定位并发槽位取得后，先持久化尝试再外呼。整批截止保留已完成结果，取消并收回其他任务，未开始任务不扣次数；超时/断连/HTTP失败/坏响应按事件隔离，沿用有界退避，不回退大表扫描。元数据事件24小时门禁和暂停项保持。
- 发送端非JSON错误仍保留HTTP状态和Retry-After（整数或HTTP日期，最多1小时）；401/403/413/422及冲突按原规则阻断，坏确认也阻断。恢复基线已被远端接受但ACK丢失时，远端核对可结束force_baseline，避免再发一份全量。已存在硬阻断不自动解除。

## 验证

采集/元数据/quota35项、发送与增量21项、既有演示/locator22项定向通过（分批执行，共78项）；覆盖空闲/重试无库存或名称查询、同名/改名、旧候选/重复键、A/A/B生命周期、坏嵌套、合法空/null响应、部分完成截止、429/5xx、鉴权/大小/校验错误、畸形ACK、丢ACK、过期租约和ACK后数据库回写失败。演示用例在隔离SQLite、明确启用模拟评审的测试环境执行，不访问真实服务，不影响生产暂停配置；默认关闭模拟评审时两条旧用例的“应入评审队列”断言不适用，未修改暂停逻辑或用例规避检查。Python编译检查与diff检查通过。新增索引在SQLite验证幂等及查询计划，生产仍需验证真实MySQL计划。

发布采用固定SHA及旧镜像依赖层，先在线增加索引，再逐角色重建API/worker/publisher，保留持久游标、发件箱、配额和配置。迁移失败不切换镜像；运行失败可按已备份Compose及旧镜像回滚，新增索引可保留。实际版本、生产检查和回滚位置待发布后补记。

## 生产验收

- 运行代码：`7e349f8bf8690e4b4d32c7e4f19dc813092890f2`；镜像 `knowledge-governance-internal:7e349f8bf869`，ID `sha256:fd7449ffa8c42d6a56e3ce9becb78dfa8447a366eab0d664ec90585b99e8bbf9`。功能提交已合并main并推送GitHub；验收补记为后续文档提交，不重建业务镜像。
- 在线迁移成功（created=true），没有停止旧采集/推送或改写业务数据。下载键MySQL计划从ALL变为ref，命中ix_documents_storage_dentry_id，估计1行；受管库查询从Workspace约152行小表开始，文档侧使用workspace索引和FirstMatch，不枚举全部文档。生产桥接不调用这项受管库诊断。
- API、worker、publisher分角色更新，均为本次SHA且running，重启次数0，API healthy，ready=200。未启动watcher；其备用镜像也已更新，仍由full_scan=false阻止运行。`.env`发布前后字节一致，未临时关闭增量或改动凭据/额度。
- 核对5个变更模块在运行镜像中的SHA256与交付Git archive完全一致；Windows归档带CRLF，规范化换行后与提交blob一致。没有将换行差异当成业务代码差异。
- 审计继续成功：最近成功2026-11-04 11:43:11 CST，完成窗口推进至11:42:49.980 CST；持久游标未重置。历史缺口仍保留，不声称已完成库存补齐。
- 源、接收和常用六个月看板均为sequence=6，源确认manifest与接收stateHash一致，board=published。真实重发已确认的4869字节增量，返回already_accepted，acceptedSequence仍为6，无重复累计。HTTP异常和数据库故障仅在本地替身环境注入，未在生产故意制造断网或错误报文。
- 评审实例2535、任务2923、通知2519；任务succeeded2533/skipped390，通知sent58/skipped2461，均与发布前一致。metadata_only/audit_pull/bridge/bi_push开启，full_scan/review/notify/content/model_transfer/stream关闭，recovery记录0，配额仍为65000。
- 最近worker/publisher日志未发现采集或推送错误，发送状态无blocked、force_baseline=false。未执行新的全量扫描或重建投影。
- 回滚材料位于 `/var/backups/knowledge-governance/collector-20261104T114230`，配置副本0600、目录0700；原af469db固定镜像保留。回滚只恢复角色镜像及Compose，新增索引保留即可。用户原有AGENTS改动未提交。
