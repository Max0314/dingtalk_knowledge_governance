# 钉钉知识库治理部署与运维交接指引

适用对象：代码负责人、部署人员、数据库管理员及平台管理员。代码由代码负责人维护，部署人员按批准的版本和配置执行，不在服务器上修改业务代码。本文覆盖首次部署、升级、后台启用、验收、紧急止损、回滚、备份和恢复。

核对日期：2026-10-08。实现基线为 `6d904e742cbb946c534b541b8f1b6354bd17fccd`，即付费 API 调用保护修复。后续发布必须包含这些保护或经过评审的替代实现。不能仅因分支叫 main 或提交日期更新，就认定已包含该修复；合并、变基或替代实现由代码负责人验证。

**当前批准的生产模式：只启动 api，查看已有知识库、文档和历史评分；日志获取、事件定位、评审、通知、Stream、全量校准全部关闭。部署完成不等于获准开启后台功能。**

## 一 责任分工与操作边界

| 事项 | 代码负责人 | 部署人员 | 数据库或平台管理员 |
| --- | --- | --- | --- |
| 业务代码、评分和统计口径 | 修改、测试并批准 | 不现场改代码或打补丁 | 配合评估影响 |
| 发布版本与配置差异 | 提供 SHA、制品及发布单 | 核对并按单执行 | 审核平台影响 |
| 数据库迁移与回滚兼容性 | 提供变更、顺序和兼容结论 | 仅执行已批准步骤 | 审核权限、锁影响和备份 |
| 主机、网络、证书和入口 | 明确需求 | 执行授权内操作 | 提供资源与审批 |
| 后台启用、扩大范围、提高预算 | 联合业务及额度负责人批准 | 不自行开启或提额 | 配合权限与额度核对 |
| 紧急暂停 | 提供支持 | 可先按第十二节止损再报告 | 配合定向网络隔离 |

真实密码、Secret、模型 Key、内部 Token、Cookie、私钥和员工身份名单只通过秘密管理渠道交接，不写入 Git、本文、工单正文或截图。部署人员只取得必要权限；Docker 操作权限属于高权限主机访问。人员变更时回收权限，并按风险轮换密钥。

## 二 每次发布的交付材料

材料不完整时不开始生产变更。代码负责人至少交付：

- 发布标签和完整 40 位 Git SHA，或不可变镜像地址与 digest。不能仅交付浮动分支或 latest。
- 变更说明、影响范围、测试结果、已知限制，以及配置新增、删除和默认值变化。
- 数据库变更清单，区分自动初始化与人工迁移，给出顺序、耗时/锁影响和回滚兼容结论。
- 本次运行模式、后台开关、扫描范围、调用预算、正文外发及通知批准范围。
- 上版可用代码及镜像、配置备份位置、数据库备份编号、维护窗口与回滚条件。
- 代码、部署、数据库、平台和业务验收负责人的企业联系方式，登记在受控台账。

推荐由代码负责人或 CI 构建镜像，部署人员只拉取指定 digest。当前 Compose 为源码构建模式；基础镜像及部分 Python 依赖采用范围版本，**同一 SHA 在不同日期重新构建，不保证同一镜像**。必须保留验收过的镜像 ID/digest、构建记录和回滚制品。

服务器不能访问代码托管平台时，由代码负责人推送批准提交到受控中转裸仓库，或提供校验过的源码/镜像包；部署人员仍核对同一版本。不能复制个人工作目录交付，不能夹带 `.env`、数据库、日志或临时文件。

## 三 架构与硬性约束

| 组件 | 职责 | 运行要求 |
| --- | --- | --- |
| `api` | 页面、查询、登录、管理 API | 唯一默认启动服务，容器端口 39021 |
| `worker` | 审计拉取、事件定位、评审、通知 | `background` profile，未经批准不启动 |
| `watcher` | 持久化月度知识库校准 | `background` profile，未经批准不启动 |
| 外部 MySQL | 元数据、评分、队列、游标、预算、扫描进度 | 唯一生产业务数据持久化位置 |
| `bi_center` 内部 API | 员工、部门、业务组及月度组织归属 | 不直连其数据库、不复制组织规则 |
| 钉钉、可选模型服务 | 元数据、审计、临时正文和评分 | 权限、出站和费用分别核验 |

- 应用服务器禁止启动 MySQL、Redis 容器；本服务不使用 Redis。生产不得使用默认 SQLite 或演示数据。
- 主机端口默认 `127.0.0.1:39021`，由同机反代或 SSH 隧道访问；不直接向公网开放。可用端口段为已分配的 39020–39029，改动需登记并同步反代。
- 三个服务各自使用 128 MiB 的 `/tmp` tmpfs，不挂持久化文档目录。正文、附件和导出文件只在任务内存或临时空间中使用，任务结束清理，不进入日志、数据库或备份。
- tmpfs 占内存；平台需控制交换区和崩溃转储等敏感数据风险。当前 Compose 未设置总内存/CPU 硬限额，不能把临时空间限额当成容器内存限额。
- 仅 api 有 HTTP healthcheck；后台容器 Up 不等于任务成功。当前按单套服务部署，不能未经评审扩副本或并行运行旧、新后台。
- `docs/phase-1-plan.md` 是历史规划，其中旧端口、Redis 和数据库容器不作为部署依据，以批准版本及本指引为准。

## 四 环境与受控台账

下列命令适用于 Linux 应用服务器的 Bash，不直接用于开发者本机 PowerShell。交互输入来自发布单；真实凭据不放命令参数或历史。

```bash
docker version
docker compose version
git --version
python3 --version
df -h
free -h
timedatectl status
ss -ltn '( sport = :39021 )'
```

使用支持 profiles 的 Docker Compose v2。宿主机 Python 3 用于标准库暂停脚本；业务运行在镜像的 Python 3.11。主机时间需同步，业务日/月预算按 Asia/Shanghai 划分。Docker 安装、用户授权、防火墙及更新遵循平台规范，不临时运行未审核的安装脚本。

首次上线前记录 CPU、内存、磁盘、文件描述符及日志轮转方案；后台启用前实测正文提取和数据库负载峰值，不根据 API-only 的消耗推断后台容量。

| 方向 | 核对项目 |
| --- | --- |
| 用户到入口 | HTTPS、有效证书、企业访问控制、正确子路径 |
| 反代到 API | 同机 127.0.0.1:39021，不额外暴露端口 |
| 应用到数据库 | 私网连接、准确来源 IP、专库账号和加密要求 |
| 应用到组织服务 | 内部 API 可达，Token 有效 |
| 应用到钉钉 | DNS、HTTPS 出站、应用权限及数字员工可见范围 |
| 应用到模型和临时资源域 | 仅对应能力获准后放行批准域名 |
| 构建及拉取制品 | 代码渠道、镜像仓库、软件源可用 |

受控台账登记：主机、绝对应用目录、Compose 项目名、端口、数据库实例/库名、入口 URL、反代位置、证书续期方式、备份位置和负责团队。真实连接信息另行保管，不能只存在某位开发者个人电脑上。

## 五 生产配置与权限

### 配置管理

首次从批准版本的 `.env.example` 创建 `.env`；升级保留生产 `.env`，按差异修改，不能用样例覆盖。样例注释可能早于实现，冲突由代码负责人核对 `app/config.py` 和发布单。

`.env` 和备份权限为 600，秘密备份受控且与 Git 分离。MySQL URL 密码按 URL 规则编码，特殊字符还需按 Compose 环境规则处理，不通过回显 URL 排查。

校验用 `docker compose config --quiet`。完整 config、inspect 环境字段、env/printenv 和 `.env` 内容可能暴露密钥，不能粘贴工单。Shell、覆盖文件及命令行注入可能改变实际配置，验收必须看生效值。环境改变需重建容器，单纯 restart 不重新注入环境变量。[Docker 环境优先级](https://docs.docker.com/compose/how-tos/environment-variables/envvars-precedence/)，[restart 说明](https://docs.docker.com/reference/cli/docker/compose/restart/)。

### 必须核对的配置

| 配置 | 生产要求 |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | 新环境登记唯一名称；已有环境沿用，不能照抄样例 kg |
| `TZ` | Asia/Shanghai |
| `KG_DATABASE_URL` | mysql+pymysql 外部专用库，charset=utf8mb4，不得落回 SQLite |
| `KG_DEMO_MODE` | false |
| `KG_PUBLISH_BIND` / `KG_PUBLISH_PORT` | 127.0.0.1 / 39021，改动需登记 |
| `KG_PUBLIC_BASE_URL` | 实际 HTTPS 入口，包含 /knowledge_governance 子路径，不含 hash 路由 |
| `KG_AUTH_ENABLED` | 对外或共享访问必须 true；样例未列出也须补齐 |
| `KG_AUTH_SECRET` | 秘密管理系统生成的高强度值，禁止默认 dev-only-not-secret |
| `DINGTALK_CORP_ID` | 正确企业标识 |
| `KG_ADMIN_UNION_IDS` | 批准的管理员 UnionID；认证开启后留空拒绝管理员操作 |
| `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET` | 企业应用凭据，登记是否与其他系统共用 |
| `DINGTALK_SYNC_OPERATOR_ID` | 服务数字员工的 UnionID，不用自然人或 UserID 代替 |
| `BI_CENTER_BASE_URL` / `BI_CENTER_INTERNAL_TOKEN` | 正式组织与历史月份归属所需，缺失不能作为统计完整验收 |

新环境可登记 `knowledge_governance` 为项目名，现有项目以容器标签为准。改项目名会产生另一套容器，旧后台可能继续运行，造成重复调用或端口冲突。

数字员工需有目标知识库的读取权限，由知识库管理员在授权界面维护，不由部署流程批量改权限。最小知识库应用权限为 `Wiki.Workspace.Read`、`Wiki.Node.Read`；审计、文档下载/导出、登录和机器人权限分别由应用管理员确认并发布生效。获权不等于获准启动。

### 默认暂停状态

批准基线的暂停脚本会关闭以下七项，并设置第六节预算和重试参数：

```dotenv
KG_FULL_SCAN_ENABLED=false
KG_REVIEW_ENABLED=false
KG_AUDIT_PULL_ENABLED=false
KG_BRIDGE_ENABLED=false
KG_BRIDGE_WALKS_ENABLED=false
KG_NOTIFY_ENABLED=false
KG_STREAM_ENABLED=false
```

`KG_MODEL_ALLOW_CONTENT_TRANSFER=false` 默认禁止正文外发，需要业务及安全批准后才可开启。模型 Key 可以经管理员页面存入数据库，环境变量可回退，所以数据库备份也可能包含密钥。无正文时跳过评审，不生成替代评分、不推送。

`KG_BI_EXPORT_ENABLED` 默认关闭。启用只读导出需单独管理 `KG_BI_EXPORT_API_KEYS`；它与 `BI_CENTER_INTERNAL_TOKEN` 用于相反方向认证，不得混用。

## 六 调用预算与月度校准保护

| 配置 | 基线值 | 范围 |
| --- | ---: | --- |
| `KG_DINGTALK_DAILY_LIMIT` | 65000 | 纳入计数的总请求每日预算 |
| `KG_DINGTALK_MONTHLY_LIMIT` | 100000 | 总请求每月预算 |
| `KG_DINGTALK_NODES_MONTHLY_LIMIT` | 60000 | 节点列表每月预算 |
| `KG_DINGTALK_SEARCH_DAILY_LIMIT` | 500 | 文件搜索每日预算 |
| `KG_DINGTALK_SEARCH_MONTHLY_LIMIT` | 10000 | 文件搜索每月预算 |
| `KG_DINGTALK_AUDIT_DAILY_LIMIT` | 1000 | 专属版文件审计日志每日预算 |
| `KG_SCAN_MAX_API_CALLS` | 60000 | 同月校准周期总请求预算 |
| `KG_SCAN_DAYS` | 10 | 每月从 10 日起允许自动建立一轮 |
| `KG_SCAN_MAX_ATTEMPTS` / `KG_SCAN_RETRY_SECONDS` | 3 / 900 | 每库最多 3 次尝试，失败约 15/30 分钟退避 |
| `KG_BRIDGE_LOCATOR_MAX_ATTEMPTS` / `KG_BRIDGE_LOCATOR_RETRY_SECONDS` | 3 / 600 | 事件定位最多 3 次，失败约 10/20 分钟退避 |

这些是本服务的请求预算，不是钉钉购买额度。提额必须审批，0 不是合法的禁用限额方式。请求在 MySQL 中先原子预留再发出，重启不清零；计数数据库不可用则拒绝外呼。超限错误为 `dingtalk_budget_exhausted`，计数不可用为 `dingtalk_budget_unavailable`。

`scan_cycles` 和 `scan_tasks` 保存月度周期及逐库进度。成功库当月不重扫；失败/中断的单个知识库可能从头重试，不是节点级断点续扫。最多 3 次包括首次及中断尝试。调用量由目录、分页和其他请求决定，不是一个知识库一次调用；单轮实际量查看 `scan:YYYY-MM`。

手动校准可提前建立当月周期，重复操作只推进未完成任务，不开第二轮。旧版 10,24 只取最早日期。`completed_with_errors` 和 `budget_exhausted` 不是全部成功，也不会自动重开同月周期。暂停保留任务；禁止删表、清计数、改月份或反复重启强制重扫。

计数及暂停边界：

- 口径为接入适配器的 HTTP 尝试/预留数，包含失败及应用 token 请求，从修复部署后开始，不补算历史，不等于钉钉付费成功调用账单。
- `GET /api/v1/diagnostics/api-usage` 只查数据库；但当前“连接诊断”整页还自动做真实连通性检查，打开或刷新会有少量钉钉调用，配置模型时也可能测试模型。
- `/api/v1/dingtalk/knowledge-bases...` 是实时钉钉代理，交互浏览会消耗额度。后台关闭不等于所有 API 均无外呼。
- 浏览器登录的用户 token/资料接口、部分独立历史脚本未统一纳入计数，其他服务共用应用的调用也不在本库预算内。禁止定时运行旧扫描/探测脚本；未经评审不得运行 `scan_uploader_baseline.py`、`*_probe.py` 等直连脚本。
- 正文资源下载不作为 OpenAPI 请求计数，模型费用另算。需要严格禁止出站时另加平台网络策略，登录也可能因此不可用；日常零钉钉调用验收用第十节数据库检查。

## 七 首次部署

### 数据库准备

由 DBA 在外部 MySQL 建专库和限定来源的专用账号，字符集 utf8mb4，不授权其他业务库。运行需 DML；启动还会建表、补列、建索引和执行已实现的数据修正，由 `kg_schema_migration` 命名锁串行化。

DBA 按版本审核 CREATE、ALTER、INDEX 及特殊迁移权限，不默认授全局管理或实例 DROP 权限。若临时迁移账号与运行账号分离，需隔离验证受限账号后续也能启动；当前未把所有初始化操作拆成独立迁移工具。

MySQL DDL 不保证被应用事务撤销，可能锁表，耗时须实测。新库为空正常，查看历史数据需要完整恢复业务库；不能为填页面直接启用全量扫描。

### 获取代码并配置

以下仅用于尚不存在的新目录，已有服务走第八节。输入必须来自发布单。

```bash
set -euo pipefail
umask 077
read -r -p '应用绝对路径: ' KG_APP_DIR
read -r -p '批准的只读仓库地址: ' KG_REPO_URL
read -r -p '批准的完整发布 SHA: ' KG_RELEASE_SHA
export KG_RELEASE_SHA
test "${KG_APP_DIR:0:1}" = /
test ! -e "$KG_APP_DIR"
git clone "$KG_REPO_URL" "$KG_APP_DIR"
cd "$KG_APP_DIR"
git switch --detach "$KG_RELEASE_SHA"
test "$(git rev-parse HEAD)" = "$KG_RELEASE_SHA"
git merge-base --is-ancestor 6d904e742cbb946c534b541b8f1b6354bd17fccd HEAD
cp .env.example .env
chmod 600 .env
```

祖先检查失败即停止，由代码负责人确认修复是否以其他形式纳入；部署人员不能删检查继续。无 Git 的源码包采用发布单指定校验和和安全验证记录。

通过受控编辑器或秘密注入填写第五节，尤其认证、MySQL、项目名。change_me、默认认证 Secret、空管理员及 localhost 入口不得留在生产。

```bash
read -r -p '登记的 Compose 项目名: ' KG_PROJECT_NAME
python3 scripts/pause_background_work.py --apply
docker compose -p "$KG_PROJECT_NAME" config --quiet
KG_RELEASE_SHA="$KG_RELEASE_SHA" docker compose -p "$KG_PROJECT_NAME" --profile background build api worker watcher
docker compose -p "$KG_PROJECT_NAME" up -d --no-deps api
```

构建后台镜像不启动容器，避免未来误用旧镜像。暂停脚本产生的 `.env.quota-backup-*` 含原秘密，权限为 600，部署后转入受控秘密备份位置，不作普通附件。脚本会恢复基线预算；若发布单另批预算，在脚本之后应用差异并核对。构建失败不能临时换未批准的依赖或源码版本。

镜像的 `org.opencontainers.image.revision` 标签必须与批准的完整 SHA 相等；使用已导出的 `KG_RELEASE_SHA` 构建，不接受生产标签为 `development`。发布单同时记录 Git SHA、镜像 ID 与标签，回滚按保留的旧镜像 ID/标签，不凭目录里的最新源码判断容器版本。

### 历史数据初始化

已有生产迁移优先完整数据库恢复，保留评分、队列、游标和预算。元数据快照导入不等于恢复数据库，不能还原丢失的历史评分。仅经批准且已备份时导入不可变快照：

```bash
read -r -p '批准的 NDJSON 元数据文件绝对路径: ' KG_SNAPSHOT_FILE
read -r -p '批准的唯一快照 ID: ' KG_SNAPSHOT_ID
docker compose -p "$KG_PROJECT_NAME" cp "$KG_SNAPSHOT_FILE" api:/tmp/import.ndjson
docker compose -p "$KG_PROJECT_NAME" exec -T api python -m scripts.import_historical_snapshot \
  --input /tmp/import.ndjson --snapshot-id "$KG_SNAPSHOT_ID"
docker compose -p "$KG_PROJECT_NAME" exec -T api python -c \
  "from pathlib import Path; Path('/tmp/import.ndjson').unlink(missing_ok=True)"
```

先核对格式、范围和唯一快照 ID，同 ID 不覆盖，不换 ID 反复导入。失败时也要执行上面针对确切临时路径的清理，并保留脱敏错误码；不要把“换 ID 再试”当作排障。文件含敏感元数据，源文件及传输按受控规范处理，这里不删除源资料。

## 八 生产升级流程

本流程有维护窗口，不宣称零停机；不操作共享主机上的其他应用。

1. 核对发布单、备份及回滚兼容性，记录旧 SHA、镜像 ID、数据计数和配置备份。旧镜像不要提前清理。
2. 识别本服务现有 Compose 项目，沿用标签中的名称，不能按目录或样例猜测：

   ```bash
   docker ps -a --filter label=com.docker.compose.service=api \
     --format '{{.Names}} {{.Label "com.docker.compose.project"}} {{.Image}}'
   ```

3. 进入登记目录，确认无本地代码差异。有差异交代码负责人，不能强制 reset、clean 或覆盖。

   ```bash
   set -euo pipefail
   umask 077
   read -r -p '现有应用绝对路径: ' KG_APP_DIR
   read -r -p '现有 Compose 项目名: ' KG_PROJECT_NAME
   read -r -p '批准的完整发布 SHA: ' KG_RELEASE_SHA
   export KG_RELEASE_SHA
   cd "$KG_APP_DIR"
   git status --short
   test -z "$(git status --porcelain)"
   git rev-parse HEAD
   docker compose -p "$KG_PROJECT_NAME" --profile background ps -a
   docker compose -p "$KG_PROJECT_NAME" images
   ```

4. 按第十三节保全配置、镜像和数据库。维护窗口先停后台再停 API，取得停写点，DBA 无并行 DDL 时完成备份；备份失败不继续。

   ```bash
   docker compose -p "$KG_PROJECT_NAME" --profile background stop worker watcher
   docker compose -p "$KG_PROJECT_NAME" stop api
   ```

5. 从批准远端取得版本，固定 SHA，不 git pull 浮动分支后直接部署：

   ```bash
   git fetch origin --tags
   git cat-file -e "$KG_RELEASE_SHA^{commit}"
   git switch --detach "$KG_RELEASE_SHA"
   test "$(git rev-parse HEAD)" = "$KG_RELEASE_SHA"
   git merge-base --is-ancestor 6d904e742cbb946c534b541b8f1b6354bd17fccd HEAD
   python3 scripts/pause_background_work.py --apply
   ```

6. 按发布单补配置并迁移。当前基线常规补列随 API 启动执行，不一口气运行全部历史 migrate 脚本。

   ```bash
   docker compose -p "$KG_PROJECT_NAME" config --quiet
   KG_RELEASE_SHA="$KG_RELEASE_SHA" docker compose -p "$KG_PROJECT_NAME" --profile background build api worker watcher
   docker compose -p "$KG_PROJECT_NAME" up -d --no-deps --force-recreate api
   ```

7. 执行第十节验收，失败保持后台关闭，按第十一节回滚。即使升级前后台曾获准运行，本次也不自动恢复，重新启用走第九节。

profiles 不是停机开关，不会停止以前已运行的后台；显式 up worker 仍可启动该服务。普通 down 也不能证明所有 profile 已停。必须显式 stop 并检查旧项目残留。[Docker profiles 规则](https://docs.docker.com/compose/how-tos/profiles/)。

## 九 后台功能逐项审批启用

当前不执行本节启用。启用前审批剩余额度、精确知识库范围、积压任务、审计游标和上线时间、正文授权、模型及通知费用、异常阈值和止损负责人。

| 批准能力 | 容器及开关 | 其他要求 |
| --- | --- | --- |
| 仅审计采集 | worker；AUDIT_PULL_ENABLED=true | bridge/review/notify/full_scan/stream 关闭 |
| 事件定位 | worker；另开 BRIDGE_ENABLED=true | review/notify/full_scan/stream 关闭，整树回退始终关闭 |
| 月度校准 | watcher；FULL_SCAN_ENABLED=true | 单独批准 WATCH_WORKSPACES 范围及窗口 |
| 正文评审 | worker；REVIEW_ENABLED=true | notify 先关闭，正文外发单独审批 |
| 评审通知 | worker；最后开 NOTIFY_ENABLED=true | 接收人和部门范围单独批准 |

表内配置均带 `KG_` 前缀。先隔离验证，再按“审计 → 定位 → 评审 → 通知”逐项观察；校准独立安排。1000 次审计/日只是上限，不保证追平全部事件，需观察游标延迟，不能无限追赶。

首次试点使用批准的精确知识库 ID，不用 `*` 或模糊名称扩大。`KG_WATCH_WORKSPACES` 不是全系统评审/通知白名单；通知另有 `KG_NOTIFY_WORKSPACES` 和 `KG_NOTIFY_DEPARTMENTS`，空白通常是不限制，边界由代码负责人确认。

恢复 worker 前审查旧评审任务、通知、未处理审计和合并窗文档。`KG_REVIEW_SINCE` 与 `KG_AUDIT_REVIEW_SINCE` 不同用途，带时区且由代码负责人制定积压处置；不随意调整、不删历史、不全量重开死信。开启评审或通知可能消费旧队列，即使没有新采集。

配置变更后重新创建获准的角色，不能 start/restart 旧容器带回旧配置。批准 worker 时：

```bash
docker compose -p "$KG_PROJECT_NAME" --profile background up -d --no-deps --force-recreate worker
```

批准 watcher 时只把服务名替换为 watcher，不同时拉起另一角色。先观察 30–60 分钟，再检查下一个业务日、计划日和跨月边界；通知先定向测试，不向全员发试验消息。异常立即按第十二节处理。

## 十 部署验收

### 不访问钉钉的基础验收

```bash
git rev-parse HEAD
docker compose -p "$KG_PROJECT_NAME" --profile background ps -a
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:39021/api/health
docker compose -p "$KG_PROJECT_NAME" logs --since 10m --tail 100 api
```

预期只有 api 运行且 healthy，后台不存在或停止，无持续重启。health 返回 status=ok、document_body_persistence=disabled，但它不查数据库，也不证明外部或后台正常。日志先本地查看并脱敏，不能直接转发。

以下只打印数据库聚合和开关，不调用 init_db 或外部 HTTP，不输出人员、正文和密钥：

```bash
docker compose -p "$KG_PROJECT_NAME" exec -T api python - <<'PY'
import json
from sqlalchemy import func, select, text
from app.config import get_settings
from app.db import Document, HistoricalFileNode, ReviewInstance, ReviewJob, Notification, SyncRun, SessionLocal
from app.api_usage import usage_status
s = get_settings()
try:
    with SessionLocal() as db:
        db.execute(text('SELECT 1'))
        counts = {name: db.scalar(select(func.count()).select_from(model)) for name, model in [
            ('documents_including_folders', Document), ('historical_file_nodes', HistoricalFileNode),
            ('review_instances', ReviewInstance), ('sync_runs', SyncRun)]}
        groups = {name: dict(db.execute(select(model.status, func.count()).group_by(model.status)).all())
                  for name, model in [('review_jobs', ReviewJob), ('notifications', Notification)]}
        switches = {name: getattr(s, name) for name in ['full_scan_enabled', 'review_enabled',
            'audit_pull_enabled', 'bridge_enabled', 'bridge_walks_enabled', 'notify_enabled', 'stream_enabled']}
        out = {'db_ok': True, 'is_mysql': db.bind.dialect.name == 'mysql',
               'demo_mode': s.demo_mode, 'auth_enabled': s.auth_enabled,
               'admin_configured': bool(s.admin_union_ids.strip()), 'switches': switches,
               'counts': counts, 'queues': groups, 'usage': usage_status(db, s)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
except Exception as exc:
    print(json.dumps({'db_ok': False, 'error_type': type(exc).__name__}))
    raise SystemExit(1)
PY
```

暂停模式必须 MySQL=true、demo=false、认证=true、管理员已配、七项开关 false。对照发布前计数，文档包含目录，历史快照和当前态可能重叠，不能简单相加。没有主动数据变更的发布，历史评分不应减少。旧队列可保留，但不能继续消费；比较两个时点确认暂停期间无新扫描和评审。

### 入口与业务验收

- 实际 HTTPS 入口、子路径、JS/CSS/ECharts 加载正常，新资源生效；必要时强制刷新。
- 未登录业务 API 返回 401，不返回业务数据；匿名 health 可 200。管理员与普通用户分别测试权限，不通过关闭认证绕过 401/403。
- 抽查历史年度、月份、知识库和既有评审详情：数量、平均 AI 分、已评分数及口径正确。无评分不能当 0 分，不能全量重评生成伪历史值。
- 核对组织当前目录和对应月度快照，缺失交代码负责人，不在部署端写部门映射规则。
- 暂停模式校准不可执行，正常管理员会话下对应管理请求返回 409；Cookie 不进入共享命令和日志。
- 要求不增加钉钉调用时，不开“连接诊断”整页、不做真实连接/正文/机器人测试、不访问实时代理；使用已有会话及数据库检查，重新登录本身可能外呼。

记录版本、镜像、容器、检查时间、前后计数、开关和验收人；证据脱敏后附工单。

## 十一 回滚流程

健康失败、关键页面不可用、数据异常、认证退化、预算保护失效或迁移错误时停止发布，后台保持关闭。

1. 停后台，必要时停 API，保留脱敏错误码、版本与时间，不先删日志、队列或数据库。
2. 代码负责人确认旧版本与现数据库兼容。回代码不会撤销数据库变更，不兼容则审批恢复或修复。
3. 使用批准回滚 SHA，保留安全修复。不能直接退到 6d904e7 之前的危险扫描实现，需要时由代码负责人提供安全回滚构建。
4. 按批准差异恢复配置，再暂停并核对预算；不直接恢复一份开启后台的旧 env。
5. 优先部署保存的上版镜像，源码重建可能改变依赖，不等同原镜像。
6. 只开 API，完整验收后恢复入口，记录回滚原因及停机时间。

代码与数据库兼容、允许源码回滚时：

```bash
read -r -p '批准的完整回滚 SHA: ' KG_ROLLBACK_SHA
docker compose -p "$KG_PROJECT_NAME" --profile background stop worker watcher
docker compose -p "$KG_PROJECT_NAME" stop api
git switch --detach "$KG_ROLLBACK_SHA"
test "$(git rev-parse HEAD)" = "$KG_ROLLBACK_SHA"
git merge-base --is-ancestor 6d904e742cbb946c534b541b8f1b6354bd17fccd HEAD
python3 scripts/pause_background_work.py --apply
docker compose -p "$KG_PROJECT_NAME" config --quiet
docker compose -p "$KG_PROJECT_NAME" --profile background build api worker watcher
docker compose -p "$KG_PROJECT_NAME" up -d --no-deps --force-recreate api
```

预构建镜像方式由代码负责人交付受控 Compose 覆盖文件，为三个服务指定上版 image；同项目名使用 `-f docker-compose.yml -f <批准覆盖文件>`，再 `up -d --no-build --no-deps --force-recreate api`。后续所有运维命令须带同组覆盖文件，防止无意切回另一镜像。

## 十二 异常调用的紧急止损

先暂停再取证。仅改 env 不会停止正在运行的容器，页面状态也不能证明后台已停。

```bash
docker compose -p "$KG_PROJECT_NAME" --profile background stop worker watcher
python3 scripts/pause_background_work.py --apply
docker compose -p "$KG_PROJECT_NAME" config --quiet
docker compose -p "$KG_PROJECT_NAME" up -d --no-deps --force-recreate api
docker compose -p "$KG_PROJECT_NAME" --profile background ps -a
```

在途请求可能已发出。持续失控由平台定向阻断出站，无法安全保留 API 时先停止并报告页面停用。排查同机旧项目、旧容器、cron/systemd/CI、人工脚本及共用钉钉应用的其他系统，不批量停服务器所有 worker：

```bash
docker ps --filter label=com.docker.compose.service=worker \
  --format '{{.Names}} {{.Label "com.docker.compose.project"}}'
docker ps --filter label=com.docker.compose.service=watcher \
  --format '{{.Names}} {{.Label "com.docker.compose.project"}}'
```

保全平台应用/API 分布、时间窗、预算计数和扫描任务。禁止删 `dingtalk_api_usage`、`scan_cycles`、`scan_tasks` 或全量重开死信，不能以提额代替查因。账单可能延迟，时间窗对齐后比较；本服务计数不增长而平台增长时查共用应用和计数外调用者。

## 十三 备份及保留

| 对象 | 必备内容 |
| --- | --- |
| MySQL 整库 | 元数据、历史基线、不可变评分、人工审核、组织快照、规则/模型配置、队列、通知、审计事件/游标、预算和扫描进度 |
| 秘密及平台配置 | env 及回滚配置、反代/证书和秘密引用；加密、审计、Git 外保管 |
| 发布制品 | SHA、镜像 digest/ID、必要离线包、构建与迁移记录、依赖清单 |

发布前必须备份并核验可读。常规频率按业务批准的 RPO（允许丢失多久的数据）设置，RTO（允许停机多久）需演练实测。仅每日备份不代表小时级恢复；在交接表填写频率、异地副本、保留周期和恢复演练计划。

数据库可能含模型 Key 和认证会话，备份按秘密/敏感数据管理。正文和附件不应在备份中；`.env.quota-backup-*` 也不是普通工单附件。

应用配置和原 API 镜像在升级前保全。以下在原应用目录执行，备份根目录须事先审批、位于仓库外且仅授权人员可访问：

```bash
umask 077
read -r -p '仓库外受控备份根目录绝对路径: ' KG_RELEASE_BACKUP_ROOT
test "${KG_RELEASE_BACKUP_ROOT:0:1}" = /
test -d "$KG_RELEASE_BACKUP_ROOT"
KG_RELEASE_BACKUP_ROOT=$(realpath -- "$KG_RELEASE_BACKUP_ROOT")
test "$KG_RELEASE_BACKUP_ROOT" != /
case "$KG_RELEASE_BACKUP_ROOT/" in
  "$(pwd -P)/"*) printf '%s\n' '备份目录不能在应用仓库内' >&2; exit 1 ;;
esac
KG_RELEASE_BACKUP_DIR=$(mktemp -d "$KG_RELEASE_BACKUP_ROOT/release-XXXXXXXX")
install -m 600 .env "$KG_RELEASE_BACKUP_DIR/env.before"
KG_PREVIOUS_SHORT=$(git rev-parse --short=12 HEAD)
KG_API_IMAGE_ID=$(docker compose -p "$KG_PROJECT_NAME" images -q api)
test -n "$KG_API_IMAGE_ID"
docker image tag "$KG_API_IMAGE_ID" "knowledge-governance-api:rollback-$KG_PREVIOUS_SHORT"
docker image save --output "$KG_RELEASE_BACKUP_DIR/api-image.tar" \
  "knowledge-governance-api:rollback-$KG_PREVIOUS_SHORT"
sha256sum "$KG_RELEASE_BACKUP_DIR/api-image.tar"
```

同时记录完整 SHA、镜像 ID 和备份目录。运行过的 worker/watcher 镜像按同样原则保留并登记，或由受控镜像仓库按 digest 留存；不能只留 API 却丢掉获准的后台制品。导出的镜像包及 env 按秘密策略加密、异地保留，不能只放故障主机。镜像标签是本地回退引用，不自动改变 Compose 部署镜像。

DBA 在数据服务器或受控备份机执行。示例前提：MySQL 客户端兼容、凭据文件 600、路径受控、InnoDB 为主、无并行 DDL；按实际 GTID/引擎及工具规范调整：

```bash
set -euo pipefail
umask 077
read -r -p '备份凭据文件绝对路径: ' KG_MYSQL_CNF
read -r -p '新的备份 SQL 绝对路径: ' KG_DB_BACKUP_SQL
test -f "$KG_MYSQL_CNF"
test ! -e "$KG_DB_BACKUP_SQL"
mysqldump --defaults-extra-file="$KG_MYSQL_CNF" \
  --single-transaction --quick --no-tablespaces --set-gtid-purged=OFF \
  --routines --events --triggers knowledge_governance > "$KG_DB_BACKUP_SQL"
test -s "$KG_DB_BACKUP_SQL"
sha256sum "$KG_DB_BACKUP_SQL"
```

DBA 管理凭据及所需权限，不用 -p密码。非空文件和校验和不证明完整，需检查退出码、表和关键计数，并隔离实际恢复。single-transaction 不保证非事务表一致，备份期间并行 DDL 也会破坏一致性。[MySQL 备份说明](https://dev.mysql.com/doc/refman/8.4/en/mysqldump.html)。

## 十四 恢复及迁移

恢复需业务、代码负责人和 DBA 批准。默认先恢复到新建隔离库，不覆盖生产；保留现场库备份和证据，明确恢复点后的数据损失与处置。

1. 停止旧环境后台及 API 写入，确认无其他部署写同库；迁移时只能有一套获准生产后台。
2. 验证备份来源、时间、校验和及配套版本，DBA 建隔离恢复库。示例目标 knowledge_governance_restore 必须是本次新建空库，不可借用未经核对的连接。

   ```bash
   mysql --defaults-extra-file="$KG_MYSQL_CNF" knowledge_governance_restore < "$KG_DB_BACKUP_SQL"
   ```

3. 新环境指向恢复库，独立项目名、登记测试端口及非生产入口，七项开关 false。隔离环境阻断真实钉钉/模型/通知出站，不带生产凭据主动测试。
4. 配套代码只启动 API，核对结构、计数、历史评分、组织月份、规则和模型版本，不能只查 SELECT 1。
5. 恢复的计数可能早于实际已用额度，游标、任务和通知也可能倒退，造成重复扫描/评审/推送。代码负责人对账并批准保守预算和积压方案前，不启动后台、不清零计数。
6. 安全处置恢复的密钥和会话；旧会话可能重新有效，轮换认证 Secret 不等于撤销数据库会话。需要失效哪些会话由代码负责人提供审核方案，部署人员不临时写 SQL。
7. 业务通过后由平台切入口及配置，重建 API，确认旧容器/旧项目/旧定时任务不再外呼。旧环境保留到回退窗口结束。
8. 完整验收并记录恢复点、损失范围、停机及遗留项。数据恢复、入口恢复和后台重启分别审批。

某个月没有评分记录时，恢复不能凭空产生；当前重评也不能冒充当时月度评分。

## 十五 反向代理及登录路径

平台在共享 nginx 站点内维护 location，不覆盖整个 server。子路径示意需按平台规则审核：

```nginx
location = /knowledge_governance {
    return 301 /knowledge_governance/;
}
location /knowledge_governance/ {
    proxy_pass http://127.0.0.1:39021/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

proxy_pass 末尾 / 用于去掉入口子路径，后端收到 /api/...；不能随意删。反代不在同机时不能照抄 loopback，另审网络。[nginx 路径规则](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)。

同步设置 `KG_PUBLIC_BASE_URL=https://<批准域名>/knowledge_governance`，钉钉配置对应域名及 `/knowledge_governance/api/auth/dingtalk/callback`。反代先测配置再平滑 reload，检查 HTTPS、Cookie、跳转与静态资源。

网关、nginx 和应用访问日志可能记录 authCode/code、state 或签名下载 URL。平台与代码负责人必须审核敏感路径禁记或脱敏；不能把凭据 URL、Cookie 和正文纳入日志监控，未完成审核不扩大对外访问。

## 十六 故障排查与禁止操作

| 现象 | 优先检查 | 禁止临时做法 |
| --- | --- | --- |
| 502/页面打不开 | API、端口、反代、日志，区分本机和公网 | 重装数据库、直接放开 0.0.0.0 |
| 页面无数据 | 错库/SQLite、新库、API 401/403 | 为填页面直接全量扫描 |
| 历史月份/部门不完整 | 快照、评分覆盖、月度组织契约、筛选 | 当前部门覆盖历史或改口径 |
| 迁移卡住/重启 | MySQL 可达性、权限、DDL 锁、错误码 | 并行启动全部容器或跑所有历史迁移 |
| 后台退出再重启 | 全关闭时进程退出，restart 策略可能循环 | 全开开关消除重启，应 stop 后台 |
| 登录/回调失败 | 域名、子路径、PUBLIC_BASE_URL、企业配置 | 关闭认证、共享 Cookie |
| 429/预算耗尽 | 本地预算还是平台限流、各周期计数 | 清计数、改时间、自动提额 |
| 计数不可用 | MySQL 和计数表，保护策略为拒绝外呼 | 绕过计数直接 HTTP |
| 校准不完整 | 周期/任务状态、尝试数、权限和预算 | 重置当月任务重复扫描 |
| 正文 unavailable/skipped | 类型、权限、导出/下载、临时容量 | 元数据替代评分、保存正文排查 |
| 平台仍增长 | 共用应用、旧项目、脚本、登录/探测 | 计数为 0 就断言平台账单错误 |
| 重复消息/历史重跑 | 旧库恢复、旧容器、积压与开关 | 删除全部通知或评分掩盖 |

默认先本地健康、数据库聚合和脱敏日志。watch_status.py 会解析实时目标，content_probe.py 会临时读正文，send_notify_samples.py 会真实发消息，都不是无调用验收。重开死信、清理和回填脚本逐次审批。

## 十七 发布与交接记录模板

复制到受控工单填写，秘密仅记录引用位置，不记录值。

| 项目 | 记录 |
| --- | --- |
| 发布单、环境、维护窗口 | 待填写 |
| 代码、部署、DBA、平台与验收负责人 | 企业联系台账引用 |
| 新旧标签、完整 SHA、镜像 digest/ID | 待填写 |
| 主机、应用绝对路径、Compose 项目名 | 受控台账引用 |
| 数据库实例/库名及来源限制 | 受控台账引用 |
| 入口、端口、反代、证书续期责任 | 待填写 |
| 配置差异、秘密引用、权限变更 | 名称与引用，不填值 |
| 本次模式与七项开关 | 默认 API-only，全部 false |
| 扫描范围、调用预算和审批人 | 无批准不扩大 |
| 迁移步骤及回滚兼容结论 | 待填写 |
| 配置/数据库备份、校验和、恢复演练 | 待填写 |
| RPO/RTO、备份频率及保留周期 | 业务和 DBA 批准 |
| 前后计数、队列、任务及调用量 | 脱敏证据引用 |
| 健康、认证、页面及历史月份验收 | 时间、结果、验收人 |
| 回滚版本、条件及负责人 | 待填写 |
| 遗留、观察期限、值班及止损人 | 待填写 |

只有版本可核验、备份可恢复、权限合格、后台与批准一致且业务通过，才登记部署完成。本文不代表已执行部署、恢复演练或配置监控，也不授权开启后台。

## 2026-10-10 性能版本补充

当前架构/时效与资源上限见 [性能与恢复方案](performance-resilience.md)，本次证据见 [发布验收](reviews/2026-10-10-release-validation.md)。旧版章节中的 `6d904e7` 是额度保护基线，不是应覆盖新发布版本的操作指令。

1. 新增 `read_cache_entries` 表、`review_jobs.lease_until` 列及评分 `(node_id, created_at)` 索引；启动迁移持有 MySQL 命名锁。均为添加式变更，旧代码可运行；回滚镜像不删表/历史实例，不回放评审队列。
2. 默认 `KG_READ_CACHE_ENABLED=true`，软刷新 600 秒、硬过期 900 秒。若禁用缓存须重新压测；不要未经测试增加并发 worker/连接池。页面读取不做外部补查。
3. `KG_LIVE_BROWSE_ENABLED=false`：实时目录代理暂停，普通浏览只读本地数据。保持其余后台开关 false。`/api/ready` 代替 `/api/health` 作为容器健康检查；存活不等于数据库就绪。
4. API 资源限额与轮转日志由 Compose 生效；部署后 `docker inspect` 核对内存/CPU/PID 上限，不仅看 YAML。共享 MySQL 不可停机压测，先用独立 Compose 项目在 39028 验证，完成后关闭并移除该临时 API 容器。
5. 按 `ops_resilience.py` 执行受限 HTTP 压测、加密备份和隔离表恢复。备份立即 `docker cp` 到权限 0700 的受控目录；键文件 0600，后续分离存管。不要把备份/键文件放仓库、发送给 AI 或复制到文档目录。
6. 回滚：保留部署前 API 镜像，使用 `ops/compose.rollback.yml` 和明确的 `KG_ROLLBACK_IMAGE`，`up -d --no-deps --no-build api`。旧版没有 `/api/ready`，override 临时改用 `/api/health`；恢复新版后移除 override 再检查 ready/原数据/额度。严禁 `down -v`、停外部数据库或改代码“回滚”。
7. 冷缓存丢失演练只能清可重建的 `read_cache_entries`，不清 canonical 业务表。数据库不可达仅在临时 API 中设置不可达 URL，再恢复原配置；不操作共享数据库端口/防火墙。生产上线不携带故障注入配置。
8. 全量/日志/评审再次启用前，评估主表孤儿、组织历史快照缺口、积压任务、历史脚本绕过计数和长期资源曲线；本次性能达标不构成启用授权。

隔离演练可使用 `python3 ops/drill_staging.py --project kg_validation_日期 --directory /home/max/apps/kg-validation-日期 --rollback-image 批准的本地镜像`。脚本仅允许该临时项目/路径前缀和 39028；会清除可重建查询缓存、短时注入临时 API 数据库连接失败、测试旧镜像，再恢复新镜像。严禁绕过前缀检查在生产运行。某些 Docker 对 tmpfs 上 `docker cp` 无法归档，需先在容器内把**加密备份**复制到受控临时层路径再复制出，确认校验后随容器替换清理；正文仍不得持久化。
