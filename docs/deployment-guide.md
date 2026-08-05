# 部署指南（脱敏版）

本文是仓库内的团队版：结构、步骤与约束。真实凭据、私钥路径、公网入口在本机
`D:\code_CPL\dingtalk_knowledge_governance-secrets\部署与运维手册.md`，不进 Git。

## 目标环境

| 项 | 值 |
| --- | --- |
| 服务器 | NeoFlow 主应用服务器（SSH 别名 `neoflow`，见本机 `~/.ssh/config`） |
| 端口 | **39021**（分配段 39020-39029 内） |
| 数据库 | NeoFlowData `172.16.0.244:3306`，库 `knowledge_governance`，专用账号，仅本库 DML/DDL |
| Redis | 不使用（worker 为数据库轮询） |
| 文档存储 | 无。正文只在评审期间过 tmpfs，不落盘（平台 OSS 要求对本服务不适用） |
| 公网 | 暂不暴露；用 SSH 隧道访问。后续需要时按平台规则加 `~/.nginx/*.conf` 的 location 层 |

## 平台硬性要求对照

| 要求 | 本项目 |
| --- | --- |
| 禁止服务器上编辑代码 | 本地改 → push → 服务器 checkout → compose up |
| 禁止应用服务器跑数据库 | Compose 无 mysql/redis 容器；连外部 MySQL |
| 文件存储接 OSS | 不存文件，天然合规 |
| 只用分配端口段 | 39021 |
| 反代只写 location 层 | 暂不配置反代 |
| 数据库端口不暴露公网 | 数据服务器无公网入口 |
| `.env` 权限 600 不进 Git | 遵守 |

## 采集身份（关键约束）

- 同步 operator 必须是**数字员工**的 UnionID（配置项 `DINGTALK_SYNC_OPERATOR_ID`），
  不得绑定自然人账号。自然人调岗会静默丢权限——参见 neo_hardware 2026-08-01
  物料库被清空的事故记录。
- 该数字员工必须是目标知识库的成员。加成员用
  `dws wiki member add`（或钉钉管理后台）。
- 需要企业应用凭据（`DINGTALK_APP_KEY/SECRET`），开通
  `Wiki.Workspace.Read` 与 `Wiki.Node.Read`。

## 首次部署

```bash
ssh neoflow
mkdir -p ~/apps && cd ~/apps
git clone git@github.com:Max0314/dingtalk_knowledge_governance.git
cd dingtalk_knowledge_governance
cp .env.example .env && chmod 600 .env
vi .env                      # 按运维手册填写
docker compose up --build -d
```

建库建号（首次，用管理账号在数据服务器执行；密码见运维手册）：

```sql
CREATE DATABASE knowledge_governance DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'knowledge_governance'@'172.16.0.243' IDENTIFIED BY '<见运维手册>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX,
      REFERENCES, CREATE TEMPORARY TABLES, LOCK TABLES
  ON knowledge_governance.* TO 'knowledge_governance'@'172.16.0.243';
FLUSH PRIVILEGES;
```

## 历史基线导入（一次性）

2026-08-05 完成的全量扫描（53,908 个去重文件节点，44/48 工作区，已排除
测试库）是增量统计的起点。把 `wiki_scan_nodes.ndjson` 上传到服务器后：

```bash
docker compose cp wiki_scan_nodes.ndjson api:/tmp/
docker compose exec api python scripts/import_historical_snapshot.py \
  --input /tmp/wiki_scan_nodes.ndjson --snapshot-id wiki-baseline-2026-08-05
```

快照不可变：同一 `snapshot-id` 拒绝重复导入。

## 日常更新

```bash
# 本地
git add -A && git commit && git push
# 服务器
cd ~/apps/dingtalk_knowledge_governance && git pull
docker compose up --build -d
```

## 健康检查

```bash
docker compose ps
curl -s http://127.0.0.1:39021/api/health
```

本机验证（SSH 隧道）：

```bash
ssh -N -L 39021:127.0.0.1:39021 neoflow
# 浏览器打开 http://localhost:39021
```

## 回滚

```bash
cd ~/apps/dingtalk_knowledge_governance
git checkout <上一个可用提交>
docker compose up --build -d
```

数据库按数据服务器的常规 mysqldump 流程备份/恢复（命令见运维手册）。
