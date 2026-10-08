from __future__ import annotations


import json


import getpass


import os


from pathlib import Path


import ssl


import time


import http.client


import subprocess


import urllib.error


import urllib.request


class AIResponseError(RuntimeError):
    """Safe, content-free diagnostic for malformed successful API responses."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)



def get_api_key(settings: dict) -> str | None:
    key_name = settings.get("api_key_env", "OPENAI_API_KEY")
    api_key = os.getenv(key_name)
    if api_key:
        return api_key
    key_file = settings.get("api_key_file")
    if key_file:
        try:
            file_key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            file_key = ""
        if file_key:
            return file_key
    service = settings.get("keychain_service")
    if not service or os.name != "posix" or not os.path.exists("/usr/bin/security"):
        return None
    account = settings.get("keychain_account") or getpass.getuser()
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None



def safe_api_error(exc):
    """Expose a bounded API error code, never the response message or request."""
    code = ""
    try:
        if exc.fp is None:
            return f"HTTP_{exc.code}"
        error = json.loads(exc.read(8192)).get("error", {})
        value = error.get("code") or error.get("type") or ""
        if isinstance(value, str) and value.replace("_", "").isalnum() and len(value) <= 80:
            code = ":" + value
    except (ValueError, AttributeError, TypeError):
        pass
    return f"HTTP_{exc.code}{code}"



def web_search_enabled(settings):
    return settings.get('web_search_enabled', False) is True



def request_response(payload, settings):
    from .agent_control import require_enabled
    require_enabled(settings.get("_agent_control_config", {}))
    if not web_search_enabled(settings) and any(
            isinstance(tool, dict) and str(tool.get('type', '')).startswith('web_search')
            for tool in payload.get('tools', [])):
        raise AIResponseError('WEB_SEARCH_DISABLED')
    from .runtime import SCOPE
    from .resources import safe_stage
    scope = SCOPE.get()
    runtime = settings.get('_runtime') or scope.get('runtime')
    stage = safe_stage(settings.get('_work_stage', scope.get('stage')))
    if runtime and (not scope.get('_measurement') or stage != scope.get('stage')):
        with runtime.measure(stage, settings.get('_work_role', scope.get('role', 'collector')),
                             {'category': settings.get('_work_category', scope.get('category', 'fresh'))}):
            return _request_response(payload, settings)
    return _request_response(payload, settings)



def _request_response(payload, settings):
    api_key = get_api_key(settings)
    if not api_key:
        raise AIResponseError("CREDENTIALS_MISSING")
    req = urllib.request.Request("https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    for attempt in range(1):
        from .agent_control import require_enabled
        require_enabled(settings.get("_agent_control_config", {}))
        from .runtime import SCOPE
        runtime = settings.get("_runtime") or SCOPE.get().get('runtime')
        call_id = runtime.reserve(payload, {**settings, '_transport_attempt': int(settings.get('_transport_attempt', attempt)), '_request_bytes': len(req.data)}) if runtime else None
        call_started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=int(settings.get("timeout_seconds", 45)), context=ssl.create_default_context()) as response:
                raw = response.read()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AIResponseError("INVALID_RESPONSE_JSON") from exc
            if not isinstance(parsed, dict):
                raise AIResponseError('INVALID_RESPONSE_JSON')
            if runtime:
                runtime.finish(call_id, parsed, time.perf_counter() - call_started, response_bytes=len(raw))
            return parsed
        except urllib.error.HTTPError as exc:
            code = safe_api_error(exc)
            status = exc.code
            if exc.fp is not None:
                exc.close()
            if runtime:
                runtime.finish(call_id, None, time.perf_counter() - call_started, AIResponseError(code))
            from .runtime import account_unavailable, BudgetDeferred
            if runtime and account_unavailable(code):
                raise BudgetDeferred('account', runtime.account_cooldown_seconds) from None
            raise AIResponseError(code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.RemoteDisconnected, http.client.IncompleteRead) as exc:
            reason = getattr(exc, "reason", exc)
            code = "TLS_CERTIFICATE_ERROR" if isinstance(reason, ssl.SSLCertVerificationError) else (
                "NETWORK_TIMEOUT" if isinstance(reason, TimeoutError) else "NETWORK_CONNECTION_ERROR")
            if runtime:
                runtime.finish(call_id, None, time.perf_counter() - call_started, AIResponseError(code))
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise AIResponseError("TLS_CERTIFICATE_ERROR") from exc
            raise AIResponseError("NETWORK_TIMEOUT" if isinstance(reason, TimeoutError) else "NETWORK_CONNECTION_ERROR") from None
        except Exception as exc:
            if runtime:
                runtime.finish(call_id, None, time.perf_counter() - call_started, exc)
            raise


