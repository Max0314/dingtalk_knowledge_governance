# 脚本索引与执行边界

AI 和部署人员先读 [工程约束](../AGENTS.md)、[部署指引](../docs/deployment-guide.md)。脚本不是启动清单，禁止遍历执行。生产默认后台暂停；`--help` 未必无副作用，先读源码。

| 分类 | 入口 | 风险/前置条件 |
| --- | --- | --- |
| 发布验收 | `ops_resilience.py` | 本地 HTTP/DB；压测用临时会话；备份含秘密需受控；演练仅专属临时表 |
| 本地预览 | `dev_server.py` | 开发端口 39027，SQLite 演示，不使用生产凭据 |
| 运维状态 | `status_brief.py`、`watch_status.py`、`review_detail.py` | 只读，但可能展示人员/文件元数据，不粘贴原始输出 |
| 组织缓存 | `sync_employee_org_month_cache.py`、`backfill_org.py` | 消费 bi_center，写本地缓存；批准月份，不能用当前组织覆盖历史 |
| 知识库登记 | `sync_workspace_registry.py`、`backfill_yida_departments.py` | 存在外呼及本地写入，先 dry-run 并确认额度 |
| 月度校准 | `run_watch_once.py` | 走持久任务和额度保护，不重复开同月已完成的库 |
| 遗留全量基线 | `scan_uploader_baseline.py` | 非常规任务；默认拒绝，显式参数 `--approve-paid-baseline` + 全量开关，仍受共享额度限制 |
| 评审/推送 | `run_review_once.py`、`notify_pump_once.py`、`send_notify_samples.py` | 外呼收费/真实消息；未批准不执行，样例不进入生产镜像 |
| 数据维护 | `cleanup_*.py`、`reopen_dead_letters.py`、`switch_primary_baseline.py`、`seed_space_map.py`、`import_historical_snapshot.py`、`migrate_*.py` | 可能删除/改口径/重放/迁移；精确范围、备份、审批先于执行 |
| 能力探测归档 | `*probe*.py`、`bizid_crosstest.py`、`bulk_day_attribution.py` | 历史研究工具，不是现行方案；probe 不进入生产镜像，可能绕过常规额度保护，禁止在生产执行 |

测试统一在 `tests/`，新回归不得写入上述临时探测脚本。运行产物放 `runtime/` 或系统临时目录，不加入 Git/镜像。不无差别删除已有运行数据；只清理本次明确创建的可重建产物。历史脚本保留追溯并分类，后续按引用检查逐项移除，而非未经核对批量删除。
