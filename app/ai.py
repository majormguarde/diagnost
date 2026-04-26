from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class AiError(RuntimeError):
    pass


def openai_chat_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    timeout_s: int = 45,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """Minimal OpenAI Chat Completions call via urllib (no extra deps)."""
    if not api_key:
        raise AiError("OPENAI_API_KEY не задан.")
    if not base_url:
        raise AiError("OPENAI_BASE_URL не задан.")
    if not model:
        raise AiError("OPENAI_MODEL не задан.")

    url = f"{base_url}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # OpenAI-compatible routers (router.ai/OpenRouter) can use these optional headers.
    if isinstance(extra_headers, dict):
        for k, v in extra_headers.items():
            vv = str(v or "").strip()
            if vv:
                headers[str(k)] = vv

    req = urllib.request.Request(
        url,
        method="POST",
        data=data,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise AiError(f"OpenAI HTTP {getattr(e, 'code', '?')}: {body or str(e)}") from e
    except urllib.error.URLError as e:
        raise AiError(f"OpenAI network error: {e}") from e

    try:
        obj = json.loads(raw)
        return (
            obj["choices"][0]["message"]["content"]
            if obj.get("choices")
            else ""
        ) or ""
    except Exception as e:
        raise AiError(f"Не удалось разобрать ответ OpenAI: {raw[:4000]}") from e

