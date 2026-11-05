"""Structured, bounded provider diagnostics; never keep response text or URLs."""
import json
import re
import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


SAFE_CODES = frozenset({'Forbidden.UserPermissionDenied','Forbidden.AccessDenied','Forbidden.UserAccessDenied',
    'Forbidden.UserNotInAppScope','Forbidden.UserNotInWhiteList','permissionDenied','accessDenied',
    'Forbidden.Unknown','Throttling.User','Throttling.Api','Throttling','Forbidden.RateLimit',
    'TooManyRequests','SystemError','InternalError','InvalidAccessToken','InvalidParameter'})
PERMISSION_CODES = frozenset({'Forbidden.UserPermissionDenied','Forbidden.AccessDenied','Forbidden.UserAccessDenied',
    'Forbidden.UserNotInAppScope','Forbidden.UserNotInWhiteList','permissionDenied','accessDenied'})


def _identifier(value, secrets=(), *, request=False):
    if not isinstance(value, str) or not value or len(value)>512 or any(s and s in value for s in secrets):
        return None, None
    phone = re.search(r'1[3-9]\d{9}', value)
    trusted_trace = re.fullmatch(r'(?i)(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})', value)
    if not phone and ((request and trusted_trace) or (not request and value in SAFE_CODES)):
        return value, None
    return None, hashlib.sha256(value.encode()).hexdigest()


def retry_after(value):
    if not value:
        return None
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            moment = parsedate_to_datetime(value)
            delay = (moment.astimezone(timezone.utc)-datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    if not 0 <= delay <= 86400:
        return None
    return max(1, int(delay))


def from_response(response, *, endpoint, secrets=()):
    from .integrations import IntegrationError
    try:
        payload = response.json()
    except ValueError:
        payload = None
    body = payload if isinstance(payload, dict) else {}
    provider, provider_hash = _identifier(body.get('code'), secrets)
    request_id, request_hash = _identifier(body.get('requestid', body.get('requestId', response.headers.get('x-acs-request-id'))), secrets, request=True)
    status = response.status_code
    raw_code = body.get('code')
    lowered = raw_code.lower() if isinstance(raw_code,str) and len(raw_code)<=512 and not any(s and s in raw_code for s in secrets) else ''
    throttled = status == 429 or any(word in lowered for word in ('throttl', 'toomanyrequests', 'frequencylimit', 'ratelimit'))
    permission = provider in PERMISSION_CODES
    if throttled:
        code, message = 'dingtalk_rate_limited', '钉钉接口限流，稍后按断点重试。'
    elif status >= 500 or status in (408, 425):
        code, message = 'dingtalk_service_unavailable', '钉钉接口暂时不可用。'
    elif status == 401:
        code, message = 'dingtalk_authentication_failed', '钉钉应用身份校验失败。'
    elif status == 403 and permission:
        code, message = 'dingtalk_permission_denied', '钉钉返回明确的权限或访问范围错误。'
    elif status == 403:
        code, message = 'dingtalk_forbidden', '钉钉拒绝请求，尚不能判定为权限不足。'
    else:
        code, message = 'dingtalk_request_failed', '钉钉接口请求失败。'
    error = IntegrationError(code, message, status)
    error.retryable = throttled or status >= 500 or status in (408, 425) or code == 'dingtalk_forbidden'
    error.retry_after = retry_after(response.headers.get('retry-after'))
    error.diagnostic = {'kind': 'dingtalk', 'status': status, 'endpoint': endpoint,
                        'providerCode': provider, 'requestId': request_id,
                        'retryAfter': error.retry_after, 'retryable': error.retryable,
                        'responseKind': 'json' if isinstance(payload, dict) else 'non_json'}
    if provider_hash:error.diagnostic['providerCodeHash']=provider_hash
    if request_hash:error.diagnostic['requestIdHash']=request_hash
    return error


def diagnostic_detail(error):
    from .integrations import IntegrationError
    if isinstance(error, IntegrationError):
        value = getattr(error, 'diagnostic', {'kind': 'integration', 'code': error.code, 'status': error.status_code})
    else:
        original = getattr(error, 'orig', None)
        args = getattr(original, 'args', ())
        value = {'kind': 'database' if original else 'execution', 'exception': type(error).__name__}
        if args and isinstance(args[0], int):
            value['errno'] = args[0]
        # Extract only the column name, never error arguments/SQL/parameters.
        if len(args) > 1 and isinstance(args[1], str):
            match = re.search(r"column ['`]([A-Za-z_][A-Za-z0-9_]*)['`]", args[1])
            if match:
                value['column'] = match[1][:64]
    result = json.dumps(value, separators=(',', ':'), ensure_ascii=True)
    if len(result) > 512:
        value.pop('requestId', None)
        value.pop('requestIdHash', None)
        result = json.dumps(value, separators=(',', ':'), ensure_ascii=True)
    return result if len(result) <= 512 else '{"kind":"diagnostic_too_large"}'
