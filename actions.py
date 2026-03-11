"""
Action implementations for the workflow engine.

Each action is an async function that receives a config dict and
the current execution context, and returns a result dict.
"""

from __future__ import annotations

import json
import logging
import re
from email.message import EmailMessage
from typing import Any, Callable, Coroutine

import httpx

logger = logging.getLogger("workflow.actions")

# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------

ActionFunc = Callable[[dict[str, Any], dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]

_REGISTRY: dict[str, ActionFunc] = {}


def register(name: str):
    """Decorator to register an action function."""
    def wrapper(fn: ActionFunc) -> ActionFunc:
        _REGISTRY[name] = fn
        return fn
    return wrapper


def get_action(name: str) -> ActionFunc:
    if name not in _REGISTRY:
        raise ValueError(f"Unknown action: {name}. Available: {list(_REGISTRY.keys())}")
    return _REGISTRY[name]


def list_actions() -> list[str]:
    return list(_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Template helper — resolve {{ variables }} from context
# ---------------------------------------------------------------------------

def _resolve(value: Any, context: dict[str, Any]) -> Any:
    """Recursively resolve {{ var }} placeholders in strings, lists, and dicts."""
    if isinstance(value, str):
        def _replacer(m: re.Match) -> str:
            key = m.group(1).strip()
            parts = key.split(".")
            obj: Any = context
            for part in parts:
                if isinstance(obj, dict):
                    obj = obj.get(part, m.group(0))
                else:
                    return m.group(0)
            return str(obj) if not isinstance(obj, str) else obj
        return re.sub(r"\{\{\s*(.+?)\s*\}\}", _replacer, value)
    elif isinstance(value, dict):
        return {k: _resolve(v, context) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve(v, context) for v in value]
    return value


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

@register("http_request")
async def http_request(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Make an HTTP request. Config: method, url, headers, body, timeout."""
    cfg = _resolve(config, context)
    method = cfg.get("method", "GET").upper()
    url = cfg["url"]
    headers = cfg.get("headers", {})
    body = cfg.get("body")
    timeout = cfg.get("timeout", 30)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            json=body if isinstance(body, (dict, list)) else None,
            content=body if isinstance(body, str) else None,
        )

    # Try to parse JSON response
    try:
        response_data = response.json()
    except (json.JSONDecodeError, ValueError):
        response_data = response.text

    result = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": response_data,
    }

    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

    logger.info("HTTP %s %s -> %d", method, url, response.status_code)
    return result


@register("send_email")
async def send_email(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """
    Send an email via SMTP. Config: smtp_host, smtp_port, username, password,
    from_addr, to, subject, body, use_tls.

    In demo mode (no smtp_host), logs the email instead of sending.
    """
    cfg = _resolve(config, context)

    to_addr = cfg["to"]
    subject = cfg.get("subject", "(no subject)")
    body = cfg.get("body", "")
    from_addr = cfg.get("from_addr", "noreply@example.com")
    smtp_host = cfg.get("smtp_host")

    if not smtp_host:
        # Demo mode: log instead of send
        logger.info("DEMO email -> %s | Subject: %s", to_addr, subject)
        return {"sent": True, "demo": True, "to": to_addr, "subject": subject}

    import aiosmtplib

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    await aiosmtplib.send(
        msg,
        hostname=smtp_host,
        port=cfg.get("smtp_port", 587),
        username=cfg.get("username"),
        password=cfg.get("password"),
        start_tls=cfg.get("use_tls", True),
    )

    logger.info("Email sent to %s", to_addr)
    return {"sent": True, "to": to_addr, "subject": subject}


@register("slack_notify")
async def slack_notify(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """
    Post a message to Slack via incoming webhook.
    Config: webhook_url, message, channel (optional).

    In demo mode (no webhook_url), logs the message.
    """
    cfg = _resolve(config, context)
    webhook_url = cfg.get("webhook_url")
    message = cfg.get("message", "Workflow notification")
    channel = cfg.get("channel")

    payload: dict[str, Any] = {"text": message}
    if channel:
        payload["channel"] = channel

    if not webhook_url:
        logger.info("DEMO Slack -> %s", message[:120])
        return {"sent": True, "demo": True, "message": message}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json=payload)

    if resp.status_code != 200:
        raise RuntimeError(f"Slack webhook returned {resp.status_code}: {resp.text}")

    logger.info("Slack notification sent")
    return {"sent": True, "status_code": resp.status_code}


@register("transform")
async def transform(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """
    Transform data using a mapping specification.
    Config: mapping (dict of output_key -> template_string or value),
            source (optional, dot-path to pull from context).
    """
    cfg = _resolve(config, context)
    mapping = cfg.get("mapping", {})
    source_path = cfg.get("source")

    # Get source data
    source_data = context
    if source_path:
        for part in source_path.split("."):
            source_data = source_data.get(part, {}) if isinstance(source_data, dict) else {}

    # Build output by resolving each mapping value
    output = {}
    for key, template in mapping.items():
        output[key] = _resolve(template, context)

    logger.info("Transform produced %d fields", len(output))
    return output


@register("condition")
async def condition(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """
    Evaluate a condition and return pass/fail.
    Config: expression (Python-safe expression), on_false ("skip_rest" | "continue").

    The expression can reference context values using dot notation.
    Supported operators: ==, !=, >, <, >=, <=, in, not in, and, or.
    """
    cfg = _resolve(config, context)
    expression = cfg["expression"]
    on_false = cfg.get("on_false", "skip_rest")

    # Build a safe evaluation namespace from flattened context
    namespace: dict[str, Any] = {}
    _flatten(context, namespace)

    try:
        result = _safe_eval(expression, namespace)
    except Exception as e:
        logger.warning("Condition eval error: %s (expression: %s)", e, expression)
        result = False

    logger.info("Condition '%s' -> %s", expression, result)
    return {"result": bool(result), "expression": expression, "on_false": on_false}


@register("validate")
async def validate(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """
    Validate input data against required fields and optional type checks.
    Config: required_fields (list of field names), field_types (dict of field -> type name).
    """
    cfg = _resolve(config, context)
    required = cfg.get("required_fields", [])
    field_types = cfg.get("field_types", {})

    input_data = context.get("input", {})
    errors: list[str] = []

    for field in required:
        if field not in input_data or input_data[field] in (None, "", []):
            errors.append(f"Missing required field: {field}")

    type_map = {"str": str, "int": int, "float": (int, float), "list": list, "dict": dict, "bool": bool}
    for field, expected in field_types.items():
        if field in input_data and input_data[field] is not None:
            expected_type = type_map.get(expected)
            if expected_type and not isinstance(input_data[field], expected_type):
                errors.append(f"Field '{field}' expected {expected}, got {type(input_data[field]).__name__}")

    if errors:
        raise ValueError(f"Validation failed: {'; '.join(errors)}")

    logger.info("Validation passed for %d fields", len(required))
    return {"valid": True, "fields_checked": len(required) + len(field_types)}


@register("log")
async def log_action(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Log a message. Config: message, level (info/warning/error)."""
    cfg = _resolve(config, context)
    message = cfg.get("message", "Log entry")
    level = cfg.get("level", "info")
    getattr(logger, level, logger.info)("WORKFLOW LOG: %s", message)
    return {"logged": True, "message": message}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten(d: dict, out: dict, prefix: str = ""):
    """Flatten nested dict for safe eval namespace."""
    for k, v in d.items():
        key = f"{prefix}{k}" if not prefix else f"{prefix}__{k}"
        if isinstance(v, dict):
            _flatten(v, out, key)
        else:
            out[key] = v
        # Also keep top-level keys accessible
        if not prefix:
            out[k] = v


def _safe_eval(expression: str, namespace: dict[str, Any]) -> bool:
    """Evaluate a simple boolean expression safely."""
    allowed_names = {
        "True": True, "False": False, "None": None,
        "len": len, "str": str, "int": int, "float": float, "bool": bool,
    }
    allowed_names.update(namespace)
    # Only allow safe builtins
    return eval(expression, {"__builtins__": {}}, allowed_names)  # noqa: S307
