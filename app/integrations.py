"""External read-only adapters. No adapter writes into DingTalk or bi_center."""
from __future__ import annotations

import json
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
        # httpx serializes None as an empty string; DingTalk rejects e.g. nextToken= with HTTP 400.
        cleaned = {key: value for key, value in params.items() if value not in (None, "")}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"https://api.dingtalk.com/v2.0{path}", params=cleaned, headers={"x-acs-dingtalk-access-token": token})
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

    async def list_nodes(self, workspace_id: str, operator_id: str, parent_node_id: str = "", next_token: str = "", max_results: int = 100) -> dict[str, Any]:
        if not parent_node_id:
            parent_node_id = (await self.workspace_detail(workspace_id, operator_id)).get("root_node_id", "")
        # The documented page cap is 30, but the API accepts 100 (verified live
        # 2026-08-06) — a 70% cut in enumeration quota.
        payload = await self._get("/wiki/nodes", {"workspaceId": workspace_id, "operatorId": operator_id, "parentNodeId": parent_node_id, "nextToken": next_token or None, "maxResults": max(1, min(max_results, 100))})
        items = payload.get("nodes", payload.get("data", []))
        return {"items": [normalize_node(item) for item in items], "next_token": payload.get("nextToken", ""), "parent_node_id": parent_node_id}

    async def resolve_user_id(self, union_id: str) -> str:
        """unionId -> userId via the legacy contact endpoint (same access token)."""
        if not union_id:
            return ""
        token = await self._token_value()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://oapi.dingtalk.com/topapi/user/getbyunionid",
                params={"access_token": token}, json={"unionid": union_id},
            )
        if response.is_error:
            raise IntegrationError("dingtalk_union_resolve_failed", f"unionId 解析失败（HTTP {response.status_code}）。", response.status_code)
        payload = response.json()
        if payload.get("errcode") != 0:
            raise IntegrationError("dingtalk_union_resolve_denied", f"unionId 解析被拒：{payload.get('errmsg', '')}", 403)
        return str(payload.get("result", {}).get("userid", ""))

    async def send_robot_markdown(self, user_ids: list[str], title: str, text: str) -> dict[str, Any]:
        """One-to-one robot push. Requires the robot capability plus its send scope."""
        if not user_ids:
            raise IntegrationError("dingtalk_no_recipient", "缺少接收人 userId。", 400)
        robot_code = self.settings.robot_code or self.settings.dingtalk_app_key
        token = await self._token_value()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                headers={"x-acs-dingtalk-access-token": token},
                json={"robotCode": robot_code, "userIds": user_ids[:20], "msgKey": "sampleMarkdown",
                      "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False)},
            )
        if response.status_code == 403:
            raise IntegrationError("dingtalk_robot_permission_denied", "机器人发送权限不足，请在开发者后台开通并发布版本。", 403)
        if response.is_error:
            raise IntegrationError("dingtalk_robot_send_failed", f"机器人消息发送失败（HTTP {response.status_code}）。", response.status_code)
        return response.json()

    async def list_dentry_permissions(self, dentry_uuid: str, operator_union_id: str, max_results: int = 100) -> list[dict]:
        """Storage-layer permission roster for one dentry (a KB root node gives
        the workspace's member list with roles). Endpoint per official SDK:
        POST /v2.0/storage/spaces/dentries/{uuid}/permissions/query."""
        token = await self._token_value()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"https://api.dingtalk.com/v2.0/storage/spaces/dentries/{dentry_uuid}/permissions/query",
                params={"unionId": operator_union_id},
                headers={"x-acs-dingtalk-access-token": token},
                json={"option": {"maxResults": max(1, min(max_results, 100))}},
            )
        if response.is_error:
            raise IntegrationError("dingtalk_permissions_failed", f"权限名册查询失败（HTTP {response.status_code}）。", response.status_code)
        payload = response.json()
        rows = payload.get("permissions", payload.get("items", [])) or []
        result = []
        for row in rows:
            member = row.get("member") or {}
            role = row.get("role") or {}
            result.append({"user_id": str(member.get("id", "")), "name": str(member.get("name", "")),
                           "type": str(member.get("type", "")),
                           "role": str(role.get("id", "") or role.get("name", ""))})
        return result

    async def search_dentries(self, keyword: str, operator_id: str, space_ids: list[str] | None = None,
                              max_results: int = 20) -> list[dict]:
        """Storage-layer name search (POST /v2.0/storage/dentries/search). Hits
        carry dentryUuid — which equals the wiki nodeId — plus name and path."""
        if not keyword or not operator_id:
            return []
        token = await self._token_value()
        option: dict[str, Any] = {"maxResults": max(1, min(max_results, 50))}
        if space_ids:
            option["spaceIds"] = space_ids
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api.dingtalk.com/v2.0/storage/dentries/search",
                params={"operatorId": operator_id},
                headers={"x-acs-dingtalk-access-token": token},
                json={"keyword": keyword[:100], "option": option},
            )
        if response.is_error:
            raise IntegrationError("dingtalk_dentry_search_failed", f"存储搜索失败（HTTP {response.status_code}）。", response.status_code)
        items = response.json().get("items", []) or []
        return [{"dentry_uuid": str(item.get("dentryUuid", "")), "name": str(item.get("name", "")),
                 "path": str(item.get("path", ""))} for item in items if isinstance(item, dict)]

    async def batch_query_wiki_nodes(self, node_ids: list[str], operator_id: str) -> list[dict]:
        """POST /v2.0/wiki/nodes/batchQuery — nodeIds -> full nodes incl. workspaceId."""
        if not node_ids:
            return []
        token = await self._token_value()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api.dingtalk.com/v2.0/wiki/nodes/batchQuery",
                params={"operatorId": operator_id},
                headers={"x-acs-dingtalk-access-token": token},
                json={"nodeIds": node_ids[:30]},
            )
        if response.is_error:
            raise IntegrationError("dingtalk_nodes_batch_failed", f"节点批量查询失败（HTTP {response.status_code}）。", response.status_code)
        payload = response.json()
        items = payload.get("nodes", payload.get("data", [])) or []
        return [normalize_node(item) for item in items if isinstance(item, dict)]

    async def download_file_bytes(self, space_id: str, numeric_dentry_id: str, max_bytes: int = 50_000_000) -> bytes:
        """Two-step storage download by NUMERIC dentry id — which is exactly
        the audit trail's bizId (cross-verified 2026-08-12). Bytes live only
        in the caller's memory for the duration of one review."""
        operator = self.settings.dingtalk_sync_operator_id
        token = await self._token_value()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"https://api.dingtalk.com/v1.0/storage/spaces/{space_id}/dentries/{numeric_dentry_id}/downloadInfos/query",
                params={"unionId": operator},
                headers={"x-acs-dingtalk-access-token": token},
                json={"option": {}},
            )
        if response.is_error:
            raise IntegrationError("dingtalk_download_info_failed", f"下载信息获取失败（HTTP {response.status_code}）。", response.status_code)
        info = response.json().get("headerSignatureInfo") or {}
        urls = info.get("resourceUrls") or []
        headers = info.get("headers") or {}
        if not urls:
            raise IntegrationError("dingtalk_download_url_missing", "下载信息响应缺少资源地址。")
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(urls[0], headers=headers)
        if response.is_error:
            raise IntegrationError("dingtalk_download_failed", f"文件下载失败（HTTP {response.status_code}）。", response.status_code)
        if len(response.content) > max_bytes:
            raise IntegrationError("dingtalk_download_too_large", "文件超出下载上限。")
        return response.content

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
        data = body.get("data", {}) or {}
        return data.get("results", data.get("items", body.get("items", [])))

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
    return {"source": "dingtalk", "node_id": item.get("nodeId", ""), "workspace_id": item.get("workspaceId", ""), "name": item.get("name", ""), "type": item.get("type", ""), "category": item.get("category", ""), "extension": item.get("extension", ""), "url": item.get("url", "") or item.get("docUrl", ""), "size": item.get("size", 0) or 0, "has_children": bool(item.get("hasChildren")), "word_count": stats.get("wordCount", 0) or 0, "permission_role": item.get("permissionRole", ""), "creator_id": item.get("creatorId", ""), "modifier_id": item.get("modifierId", ""), "created_at": item.get("createTime", ""), "updated_at": item.get("modifiedTime", "")}


def resolve_model_key(config: dict[str, Any]) -> str:
    """Stored key first, env fallback second."""
    return config.get("api_key") or os.getenv(config.get("api_key_env_name", "KG_MODEL_API_KEY") or "KG_MODEL_API_KEY", "")


async def model_connection_check(config: dict[str, Any], settings: Settings) -> dict[str, Any]:
    if not config.get("enabled"):
        return {"status": "disabled", "message": "模型配置未启用，评审将使用规则引擎。"}
    if not config.get("base_url") or not config.get("model_name"):
        return {"status": "not_configured", "message": "缺少模型基础地址或模型名称。"}
    key = resolve_model_key(config)
    if not key:
        return {"status": "not_configured", "message": "未配置 API Key（页面填写或注入环境变量均可）。"}
    try:
        async with httpx.AsyncClient(timeout=min(int(config.get("timeout_seconds", 30)), 60)) as client:
            response = await client.get(f"{config['base_url'].rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"})
        return {"status": "healthy" if response.is_success else "failed", "message": "模型服务可访问。" if response.is_success else f"HTTP {response.status_code}"}
    except httpx.HTTPError:
        return {"status": "failed", "message": "模型服务连接失败。"}


GENRE_RUBRICS = """· 规范制度：适用范围明确、版本与生效信息完整、条款可执行不含糊、与现行流程无明显冲突
· 方案设计：背景与目标清楚、方案对比或选型依据充分、实施计划可落地、风险与对策
· 测试用例：前置条件与环境说明、步骤可复现、每步预期结果与判定标准明确、覆盖点清晰有标识
· 测试报告：结论先行且明确、测试环境与范围交代完整、数据支撑结论、遗留问题与风险如实列出
· 操作手册：步骤完整可跟随、关键处有示例或截图说明、异常情况与回退处理、面向读者无前置知识断层
· 会议纪要：决议明确不含糊、每项决议有责任人与时限、遗留议题清楚
· 数据报告：数据来源与统计口径说明、结论与数据一致、图表可读
· 其他知识文档：按通用四维从严评估"""

JSON_CONTRACT = ("只返回 JSON："
                 "{\"genre\":\"文体\",\"score\":0-100,"
                 "\"dimensions\":{\"要素完整\":0-15,\"准确清晰\":0-15,\"结构可读\":0-15,\"规范性\":0-15,\"文体专属\":0-40},"
                 "\"findings\":[{\"rule\":\"维度名\",\"deduction\":整数,\"message\":\"不引用原文的具体问题与可执行的整改建议\"}]}。"
                 "不要复述或引用文档原文。")


def build_review_prompt(file_class: str, filename: str) -> str:
    """Genre-aware two-stage review prompt. The model classifies the genre
    first, then scores against that genre's rubric — a test case is judged as
    a test case, not as a missing-abstract essay."""
    if file_class == "sheet":
        return ("你是企业知识库评审员。这是一份电子表格（内容为提取出的单元格文本，顺序可能与表格布局不同）。"
                "文体固定为「电子表格」。评分（总分100）：表头与字段命名清晰完整(0-30)、必要的说明或注释(0-25)、"
                "术语与格式一致性(0-25)、文件命名规范(0-20)。"
                "【不要】因缺少摘要、章节结构、版本历史等文档特有要素扣分。"
                "只返回 JSON：{\"genre\":\"电子表格\",\"score\":0-100,"
                "\"dimensions\":{\"表头与字段\":0-30,\"说明与注释\":0-25,\"一致性\":0-25,\"命名规范\":0-20},"
                "\"findings\":[{\"rule\":\"维度名\",\"deduction\":整数,\"message\":\"不引用原文的具体问题与整改建议\"}]}。"
                "不要复述或引用文档原文。文件名：" + filename)
    if file_class == "slide":
        return ("你是企业知识库评审员。这是一份演示文稿（内容为逐页提取的文字）。文体固定为「演示文稿」。"
                "评分（总分100）：逻辑主线清晰(0-30)、每页要点明确不堆砌(0-25)、标题与层次结构(0-25)、命名与用语规范(0-20)。"
                "【不要】因缺少摘要、版本历史等文档特有要素扣分。"
                "只返回 JSON：{\"genre\":\"演示文稿\",\"score\":0-100,"
                "\"dimensions\":{\"逻辑主线\":0-30,\"每页要点\":0-25,\"层次结构\":0-25,\"规范性\":0-20},"
                "\"findings\":[{\"rule\":\"维度名\",\"deduction\":整数,\"message\":\"不引用原文的具体问题与整改建议\"}]}。"
                "不要复述或引用文档原文。文件名：" + filename)
    return ("你是企业知识库评审员。请先判定文档文体，再按对应标准评分。\n"
            "第一步·文体判定（从下列选一）：规范制度 / 方案设计 / 测试用例 / 测试报告 / 操作手册 / 会议纪要 / 数据报告 / 其他知识文档\n"
            "第二步·评分（总分100 = 通用60 + 文体专属40）：\n"
            "通用四维（各15分）：要素完整（该文体应有的要素是否齐全）、准确清晰（表述明确、术语一致）、"
            "结构可读（组织合理、便于查阅）、规范性（命名、版本信息、格式统一）。\n"
            "文体专属要点（40分，按判定的文体取用）：\n" + GENRE_RUBRICS + "\n"
            + JSON_CONTRACT + "文件名：" + filename)


ADVISORY_GENRES = ("测试用例", "测试报告", "会议纪要", "数据报告")


async def model_score_content(config: dict[str, Any], content: str, filename: str,
                              file_class: str = "document") -> dict[str, Any] | None:
    """Invoke an OpenAI-compatible model without persisting or logging document text.

    The model receives a bounded temporary body only after the caller has enforced the
    explicit data-transfer policy. Its output is schema-checked before use.
    """
    key = resolve_model_key(config)
    if not key or not content:
        return None
    prompt = build_review_prompt(file_class, filename) + "\n正文：\n" + content[:60000]
    payload: dict[str, Any] = {"model": config["model_name"], "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": "Return strict JSON only."}, {"role": "user", "content": prompt}]}
    payload["temperature"] = config.get("temperature") if config.get("temperature") is not None else 0
    if config.get("thinking_mode") == "on":
        payload["enable_thinking"] = True
    elif config.get("thinking_mode") == "off":
        payload["enable_thinking"] = False
    try:
        async with httpx.AsyncClient(timeout=min(int(config.get("timeout_seconds", 30)), 120)) as client:
            response = await client.post(
                f"{config['base_url'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
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
        raw_dims = result.get("dimensions", {})
        dimensions = {}
        if isinstance(raw_dims, dict):
            for key, value in list(raw_dims.items())[:8]:
                try:
                    dimensions[str(key)[:16]] = max(0, min(100, round(float(value), 1)))
                except (TypeError, ValueError):
                    continue
        return {"score": score, "genre": str(result.get("genre", "") or "")[:16],
                "dimensions": dimensions, "findings": safe_findings}
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return None
