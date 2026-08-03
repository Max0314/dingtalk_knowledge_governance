"""External read-only adapters. No adapter writes into DingTalk or bi_center."""
from __future__ import annotations

import os
import time
from typing import Any
import httpx

from .config import Settings


class IntegrationError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 503):
        super().__init__(message)
        self.code, self.status_code = code, status_code


class DingtalkClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token = ""
        self._token_expires_at = 0.0

    def configured(self) -> bool:
        return bool(self.settings.dingtalk_app_key and self.settings.dingtalk_app_secret)

    async def _token_value(self) -> str:
        if not self.configured():
            raise IntegrationError("dingtalk_not_configured", "缺少 DINGTALK_APP_KEY 或 DINGTALK_APP_SECRET。")
        if self._token and self._token_expires_at > time.time() + 30:
            return self._token
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": self.settings.dingtalk_app_key, "appSecret": self.settings.dingtalk_app_secret},
            )
        if response.is_error:
            raise IntegrationError("dingtalk_token_failed", "钉钉应用 token 获取失败。", response.status_code)
        payload = response.json()
        self._token = payload.get("accessToken", "")
        self._token_expires_at = time.time() + int(payload.get("expireIn", 7200))
        if not self._token:
            raise IntegrationError("dingtalk_token_invalid", "钉钉 token 响应缺少 accessToken。")
        return self._token

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        token = await self._token_value()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"https://api.dingtalk.com/v2.0{path}", params=params, headers={"x-acs-dingtalk-access-token": token})
        if response.status_code == 403:
            raise IntegrationError("dingtalk_permission_denied", "钉钉权限不足，请检查 Wiki.Workspace.Read、Wiki.Node.Read 和 operatorId 的访问权限。", 403)
        if response.is_error:
            raise IntegrationError("dingtalk_request_failed", f"钉钉接口调用失败（HTTP {response.status_code}）。", response.status_code)
        return response.json()

    async def list_workspaces(self, operator_id: str, next_token: str = "", max_results: int = 30) -> dict[str, Any]:
        if not operator_id:
            raise IntegrationError("dingtalk_operator_missing", "缺少钉钉 operatorId（UnionID），不能伪造身份调用知识库接口。", 400)
        payload = await self._get("/wiki/workspaces", {"operatorId": operator_id, "nextToken": next_token or None, "maxResults": max(1, min(max_results, 30))})
        items = payload.get("workspaces", payload.get("data", []))
        return {"items": [normalize_workspace(item) for item in items], "next_token": payload.get("nextToken", "")}

    async def workspace_detail(self, workspace_id: str, operator_id: str) -> dict[str, Any]:
        return normalize_workspace(await self._get(f"/wiki/workspaces/{workspace_id}", {"operatorId": operator_id}))

    async def list_nodes(self, workspace_id: str, operator_id: str, parent_node_id: str = "", next_token: str = "", max_results: int = 30) -> dict[str, Any]:
        if not parent_node_id:
            parent_node_id = (await self.workspace_detail(workspace_id, operator_id)).get("root_node_id", "")
        payload = await self._get("/wiki/nodes", {"workspaceId": workspace_id, "operatorId": operator_id, "parentNodeId": parent_node_id, "nextToken": next_token or None, "maxResults": max(1, min(max_results, 30))})
        items = payload.get("nodes", payload.get("data", []))
        return {"items": [normalize_node(item) for item in items], "next_token": payload.get("nextToken", ""), "parent_node_id": parent_node_id}

    async def fetch_ephemeral_content(self, node_id: str) -> str:
        """Fetches text only when an explicitly verified content gateway is configured.

        The returned string is intentionally never written to DB, jobs, logs, or disk.
        """
        template = self.settings.dingtalk_doc_content_url_template
        if not template:
            return ""
        token = await self._token_value()
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.get(template.format(node_id=node_id), headers={"x-acs-dingtalk-access-token": token})
        if response.is_error:
            raise IntegrationError("dingtalk_content_fetch_failed", "文档正文临时获取失败。", response.status_code)
        payload = response.json()
        return str(payload.get("content", payload.get("text", "")))


class BiCenterClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def configured(self) -> bool:
        return bool(self.settings.bi_center_base_url and self.settings.bi_center_internal_token)

    async def resolve_batch(self, items: list[dict[str, Any]], month_key: str = "") -> list[dict[str, Any]]:
        if not self.configured():
            return [{"matched": False, "includeInOfficialStats": False, "status": "integration_not_configured"} for _ in items]
        payload = {"items": [{"sourceSystem": "dingtalk_knowledge_governance", "monthKey": month_key, "identity": item} for item in items]}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self.settings.bi_center_base_url.rstrip('/')}/api/internal/v1/employee-identity/resolve-batch", json=payload, headers={"Authorization": f"Bearer {self.settings.bi_center_internal_token}"})
        if response.is_error:
            raise IntegrationError("bi_center_request_failed", f"bi_center 身份解析失败（HTTP {response.status_code}）。", response.status_code)
        body = response.json()
        return body.get("data", {}).get("items", body.get("items", []))

    async def check(self) -> dict[str, Any]:
        if not self.configured():
            return {"status": "not_configured", "message": "未配置 BI_CENTER_BASE_URL 或 BI_CENTER_INTERNAL_TOKEN。"}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.settings.bi_center_base_url.rstrip('/')}/api/internal/v1/employee-directory/current?limit=1&offset=0", headers={"Authorization": f"Bearer {self.settings.bi_center_internal_token}"})
        return {"status": "healthy" if response.is_success else "failed", "message": "目录契约可访问。" if response.is_success else f"HTTP {response.status_code}"}


def normalize_workspace(item: dict[str, Any]) -> dict[str, Any]:
    return {"source": "dingtalk", "workspace_id": item.get("workspaceId", ""), "name": item.get("name", ""), "description": item.get("description", ""), "type": item.get("type", ""), "team_id": item.get("teamId", ""), "root_node_id": item.get("rootNodeId", ""), "url": item.get("url", ""), "permission_role": item.get("permissionRole", ""), "creator_id": item.get("creatorId", ""), "created_at": item.get("createTime", ""), "updated_at": item.get("modifiedTime", "")}


def normalize_node(item: dict[str, Any]) -> dict[str, Any]:
    stats = item.get("statisticalInfo") or {}
    return {"source": "dingtalk", "node_id": item.get("nodeId", ""), "workspace_id": item.get("workspaceId", ""), "name": item.get("name", ""), "type": item.get("type", ""), "category": item.get("category", ""), "extension": item.get("extension", ""), "url": item.get("url", ""), "size": item.get("size", 0) or 0, "has_children": bool(item.get("hasChildren")), "word_count": stats.get("wordCount", 0) or 0, "permission_role": item.get("permissionRole", ""), "creator_id": item.get("creatorId", ""), "created_at": item.get("createTime", ""), "updated_at": item.get("modifiedTime", "")}


async def model_connection_check(config: dict[str, Any], settings: Settings) -> dict[str, Any]:
    if not config.get("enabled"):
        return {"status": "disabled", "message": "模型配置未启用，评审将使用规则引擎。"}
    if not config.get("base_url") or not config.get("model_name"):
        return {"status": "not_configured", "message": "缺少模型基础地址或模型名称。"}
    key = os.getenv(config.get("api_key_env_name", "KG_MODEL_API_KEY"), "")
    if not key:
        return {"status": "not_configured", "message": f"环境变量 {config.get('api_key_env_name', 'KG_MODEL_API_KEY')} 未注入。"}
    try:
        async with httpx.AsyncClient(timeout=min(int(config.get("timeout_seconds", 30)), 60)) as client:
            response = await client.get(f"{config['base_url'].rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"})
        return {"status": "healthy" if response.is_success else "failed", "message": "模型服务可访问。" if response.is_success else f"HTTP {response.status_code}"}
    except httpx.HTTPError:
        return {"status": "failed", "message": "模型服务连接失败。"}


async def model_score_content(config: dict[str, Any], content: str, filename: str) -> dict[str, Any] | None:
    """Invoke an OpenAI-compatible model without persisting or logging document text.

    The model receives a bounded temporary body only after the caller has enforced the
    explicit data-transfer policy. Its output is schema-checked before use.
    """
    key = os.getenv(config.get("api_key_env_name", "KG_MODEL_API_KEY"), "")
    if not key or not content:
        return None
    prompt = ("你是企业知识库评审器。依据评分标准通用-V1.1对文档评分。"
              "只返回 JSON：{\"score\":0-100,\"findings\":[{\"rule\":\"章节号\",\"deduction\":整数,\"message\":\"不引用正文的简短问题\"}]}。"
              "不要复述或引用文档原文。文件名：" + filename + "\n正文：\n" + content[:60000])
    try:
        async with httpx.AsyncClient(timeout=min(int(config.get("timeout_seconds", 30)), 60)) as client:
            response = await client.post(
                f"{config['base_url'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": config["model_name"], "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": "Return strict JSON only."}, {"role": "user", "content": prompt}]},
            )
        if response.is_error:
            return None
        payload = response.json()
        text = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        import json
        result = json.loads(text)
        score = float(result.get("score"))
        if not 0 <= score <= 100:
            return None
        findings = result.get("findings", [])
        if not isinstance(findings, list): findings = []
        safe_findings = []
        for item in findings[:20]:
            if isinstance(item, dict):
                safe_findings.append({"rule": str(item.get("rule", "model"))[:32], "deduction": max(0, min(100, int(item.get("deduction", 0) or 0))), "message": str(item.get("message", "模型发现问题。"))[:240]})
        return {"score": score, "findings": safe_findings}
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return None
