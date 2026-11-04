"""Local status reads; external probes run only on an explicit administrator POST."""
import asyncio

import httpx
from sqlalchemy import select

from .db import ModelConfig, SyncRun
from .integrations import BiCenterClient, DingtalkClient, IntegrationError, model_connection_check


def local_status(db, settings):
    ding = DingtalkClient(settings)
    model = db.scalar(select(ModelConfig).where(ModelConfig.enabled.is_(True)).limit(1))
    configured = ding.configured() and bool(settings.dingtalk_sync_operator_id)
    items = [
        {"target": "dingtalk", "name": "钉钉知识库", "status": "configured" if configured else "not_configured",
         "message": "已配置，点击测试检查当前连接。" if configured else "缺少钉钉应用凭据或采集身份。"},
        {"target": "bi_center", "name": "bi_center 组织架构", "status": "configured" if BiCenterClient(settings).configured() else "not_configured",
         "message": "组织归属由 bi_center 提供，点击测试检查连接。"},
        {"target": "model", "name": "AI 评审模型", "status": "paused" if settings.metadata_only else ("configured" if model else "disabled"),
         "message": "模型调用已暂停，模型配置保留。" if settings.metadata_only else ("已配置模型，点击测试检查连接。" if model else "未启用模型配置。")},
    ]
    last = db.scalar(select(SyncRun).where(SyncRun.mode.in_(("watch", "watch_seed", "bridge", "bridge_seed")))
                     .order_by(SyncRun.created_at.desc()).limit(1))
    if not settings.full_scan_enabled:
        status, message = "paused", "全量校准已暂停；审计增量按独立开关运行。"
    elif last:
        status = "healthy" if last.status == "succeeded" else "failed"
        message = f"最近校准：{last.status}，节点 {last.documents_seen}，{last.created_at.isoformat()}。"
    else:
        status, message = "pending", "等待首轮校准。"
    items.append({"name": "月度目录校准", "status": status, "message": message})
    return {"items": items, "body_storage": "正文不保存；打开本页不调用钉钉、模型或组织接口。"}


async def probe(target, config, settings):
    async def execute():
        if target == "dingtalk":
            ding = DingtalkClient(settings)
            if not ding.configured() or not settings.dingtalk_sync_operator_id:
                return {"status": "not_configured", "message": "缺少钉钉应用凭据或采集身份。"}
            await ding.list_workspaces(settings.dingtalk_sync_operator_id, max_results=1)
            return {"status": "healthy", "message": "知识库读取接口与采集身份可用。"}
        if target == "bi_center":
            return await BiCenterClient(settings).check()
        if target == "model":
            return await model_connection_check(config, settings)
        raise ValueError("unknown_probe")

    try:
        result = await asyncio.wait_for(execute(), timeout=8)
        return {"target": target, **result}
    except IntegrationError as exc:
        return {"target": target, "status": "failed", "code": exc.code, "message": "连接测试失败，请检查权限、额度和接口配置。"}
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return {"target": target, "status": "failed", "code": "probe_timeout", "message": "连接测试超过 8 秒，请稍后重试。"}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {"target": target, "status": "failed", "code": "probe_failed", "message": "连接失败或接口返回格式异常。"}
