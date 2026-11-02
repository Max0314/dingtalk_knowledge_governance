# 2026-11-02 公司内网部署记录

## 运行位置与版本

| 项目 | 实际配置 |
| --- | --- |
| 入口 | `http://10.70.32.32:39021`，用户指定使用 IP 和 HTTP 联调 |
| 应用目录 | `/opt/kg`，配置文件归 `kg-app` 所有 |
| 系统 | Rocky Linux 9.6，x86_64 |
| Docker / Compose | Engine 29.6.2 / Compose 5.3.1 |
| Compose 项目 | `knowledge_governance_internal` |
| Compose 文件 | `/opt/kg/compose.internal.yml` |
| 发布代码 | `3d8bb0c1de6df0eba14a5db9df697dd806d362a9` |
| 固化镜像 | `knowledge-governance-handoff:3d8bb0c1de6d` |
| 镜像 ID | `sha256:c1602c8697769a182a9be47cba8ed201047a14593b4d6d73b8e0ea97e6198188` |
| 数据库 | 独立数据服务器上的 MySQL 8.4.6，沿用同事恢复的业务库 |
| 当前模式 | 仅 API；认证开启；采集、扫描、评审、通知、Stream、实时目录和 BI 导出关闭 |
| 资源限制 | API 768 MiB、2 CPU、128 PID；独立 128 MiB `/tmp` tmpfs；日志 10 MiB × 3 |

使用原环境导出的固定镜像，没有在服务器重建 Python 依赖，也不使用手动 `uvicorn` 或宿主机 `.venv` 启动。Docker 开机自启，API 设置 `restart: unless-stopped`。当前入口直接发布 39021，Nginx 保持未启用。

原阿里云 `knowledge_governance` 项目的 API 仍停止，不恢复旧后台。BI 看板继续更新所需的主动推送尚未实现；本次未修改 `bi_center` 仓库或接口契约。

## 已完成的检查

- 导入固定镜像并核对完整镜像 ID 和代码标签。目标应用代码、静态文件与交付版本一致；目标目录为源码快照，没有 `.git`。
- MySQL 29 张表，导入后 604,560 行：与备份相比只清理了 2 条旧会话和 8 条查询缓存，其余各表行数一致。
- 保留内网数据库连接，补齐原环境的应用凭据、数字员工身份、企业标识、管理员、组织接口和模型环境密钥；生成新的认证 Secret。实际配置仍只保存于服务器 `.env`，权限 0600。
- 生效配置检查通过，`/api/health`、`/api/ready` 返回 200；从操作者内网电脑能访问入口与登录页；匿名业务 API 返回 401，BI 导出保持 404。
- 容器健康状态为 `healthy`，重启次数 0；核对实际内存、CPU、PID、tmpfs、日志轮转和自动重启策略，均与交付配置一致。登录与业务页面所需 CSS、JavaScript、ECharts 和图标均返回正确类型的 200 响应，最近日志无 ERROR/CRITICAL/Traceback。
- 使用临时合成只读会话验证总览、2025/2026 月度增量、文件、评审与知识库列表，均返回 200；非管理员访问模型配置返回 403。测试结束撤销该会话。
- 浏览器已从内网登录页进入对应应用的钉钉授权/扫码页；尚未完成真实用户授权，回调登记及登录换取凭证仍待验收。
- 业务表数量未变化，无活动评审或通知积压。API 启动和页面查询正常重建查询缓存；定点钉钉验证保留正常调用计数，不清零额度。

现有完整数据库备份为 2026-10-30 停止原服务后导出的版本。新服务器变更前的 `.env` 保存在 `/var/backups/knowledge-governance/release-20261102T174338`，目录权限 0700、配置副本 0600。原始配置和数据库备份不进入 Git。

## 外部联调待办

| 项目 | 检查结果 | 待处理内容 |
| --- | --- | --- |
| 钉钉 HTTPS | 生产仍严格校验；候选配置已绕过证书错误，但接口被 302 跳转至公司上网认证页 | 运维处理服务器的出网认证或配置正式代理，之后复测真实接口 |
| bi_center 组织接口 | 候选配置同样被跳转至上网认证页 | 与钉钉一起处理出网认证，之后复测当前及月度目录接口 |
| 登录回调 | 应用已生成新的 IP 回调地址，尚未完成真实用户登录 | 在钉钉应用的安全设置登记下方重定向 URL，再由用户在内网登录验收 |
| 模型权限 | `/models` 返回 200；`qwen3.7-plus` 推理返回 403 `AccessDenied.Unpurchased` | 为现密钥开通模型权限，或由负责人指定已开通的模型；本次没有自动换模型 |
| BI 看板更新 | 原外部拉取方向无法直接覆盖内网入口 | 后续另行确定出站推送契约与两边开发范围 |

钉钉与 NeoFlow 收到的证书使用相同颁发者，疑似出口 HTTPS 解密，具体设备配置由运维确认。TCP 443 可连接，但上述接口尚未通过业务联调；生产 API 仍使用默认的证书校验。

## 应用侧 TLS 兼容修复

用户于本次迁移中明确授权降低 TLS 安全等级或按域名配置例外，并要求修改代码、提交 Git。新增 `KG_TLS_INSECURE_HOSTS`，默认空；候选版仅对 `api.dingtalk.com`、`oapi.dingtalk.com`、`login.dingtalk.com`、`neoflow-cn.neo-net.com` 设置例外。阿里云模型接口保持严格校验。

- 候选镜像使用原固化镜像的依赖层，只替换本项目应用代码和文档，没有更新 Python 或依赖。
- 65 项针对性测试通过，覆盖精确域名、相似域名、跨域重定向、流式读取、连接关闭、代理/NO_PROXY，以及既有登录、正文和额度保护回归；隔离测试不访问外部网络或业务数据库。
- 实际 TLS 探测中，四个例外域名均能返回 HTTP 响应；模型域名继续使用正常证书校验。
- 钉钉接口与 bi_center 目录接口随后都返回 HTTP 302，重定向到 `http://10.2.254.7/ac_portal/needauth.html`。这是出网上网认证阻断，应用侧 TLS 兼容不能完成该认证。
- 候选版尚未切换到正式 API。生产继续运行原镜像、保持严格证书校验；待运维处理服务器免认证出网或提供正式代理后，再启用已授权的域名例外并验收接口。

当前需要运维处理的具体问题为：为应用服务器 `10.70.32.32` 提供上述域名的正常出站访问，API 请求不能被重定向到交互式上网认证页面。候选版验证只保留正常钉钉调用计数，没有触发扫描、评审或通知。

钉钉重定向 URL：

```text
http://10.70.32.32:39021/api/auth/dingtalk/callback
```

授权完成后由用户浏览器回到此地址；用户电脑须能访问内网 IP。钉钉后台登记与回调流程参考 [钉钉开发者百科](https://open-dingtalk.github.io/developerpedia/docs/develop/permission/token/browser/get_user_app_token_browser/)。

## 运维命令

在服务器执行，所有命令使用同一个 Compose 项目与文件：

```bash
cd /opt/kg
sudo docker compose -p knowledge_governance_internal -f compose.internal.yml ps
curl -fsS http://127.0.0.1:39021/api/ready
sudo docker compose -p knowledge_governance_internal -f compose.internal.yml logs --tail=50 api
```

修改 `.env` 后重新创建 API：

```bash
cd /opt/kg
sudo docker compose -p knowledge_governance_internal -f compose.internal.yml config --quiet
sudo docker compose -p knowledge_governance_internal -f compose.internal.yml up -d --no-build --pull never --no-deps --force-recreate api
```

停止 API：

```bash
cd /opt/kg
sudo docker compose -p knowledge_governance_internal -f compose.internal.yml stop api
```

Docker 官方源在目标服务器也遇到证书校验问题，本次从操作者电脑经 HTTPS 下载官方 RPM，按官方仓库元数据校验 SHA-256，传入服务器后检查 RPM 签名再安装。未启用不可访问的 Docker 软件源；后续升级沿用审核后的离线包或先解决出口证书问题。安装方法参考 [Docker RHEL 官方文档](https://docs.docker.com/engine/install/rhel/)。
