from __future__ import annotations

import json


def short_error(exc: Exception) -> str:
    """把 Alpaca/网络异常压缩成一行可读消息，避免打印 traceback。"""
    text = str(exc).strip()
    if not text:
        return type(exc).__name__

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return f"{type(exc).__name__}: {text}"

    message = payload.get("message", text)
    code = payload.get("code")
    buying_power = payload.get("buying_power")
    if buying_power is not None:
        return f"{message} | code={code} buying_power={buying_power}"
    if code is not None:
        return f"{message} | code={code}"
    return str(message)
