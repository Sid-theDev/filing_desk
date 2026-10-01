"""Talking to the model: one function, call_model(), over OpenRouter's chat completions API.

Standard library only. OpenRouter speaks the OpenAI message format, so swapping models is a
change to FILING_DESK_MODEL in .env, nothing else.
"""

import json
import os
import time
import urllib.error
import urllib.request
from functools import lru_cache

from . import config

API_URL = "https://openrouter.ai/api/v1/chat/completions"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"
RETRY_STATUS = {429, 500, 502, 503, 504}


class ModelError(Exception):
    """The model call failed (no key, network, or an error from OpenRouter)."""


def default_model() -> str:
    config.load_env()
    return os.environ.get("FILING_DESK_MODEL") or config.DEFAULT_MODEL


def call_model(messages: list[dict], tools: list[dict], model: str, tool_choice: str = "auto") -> dict:
    """Send the conversation, return {'message', 'usage', 'finish_reason'}."""
    config.load_env()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ModelError("OPENROUTER_API_KEY is not set. Add it to .env (see .env.example).")

    body = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "max_tokens": config.MAX_OUTPUT_TOKENS,
        "usage": {"include": True},
        # Cheapest provider first, and only providers that support every parameter sent (tools included).
        "provider": {"sort": "price", "require_parameters": True},
    }
    # Reasoning models such as GPT-5.6 Luna reject temperature; sending it would match no provider.
    if "temperature" in supported_parameters(model):
        body["temperature"] = config.TEMPERATURE
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "Filing Desk",
        },
    )
    data = send_with_retries(request)
    if "error" in data:
        raise ModelError(f"OpenRouter error: {data['error']}")

    choice = data["choices"][0]
    reply = choice["message"]
    message = {"role": "assistant", "content": reply.get("content") or ""}
    if reply.get("tool_calls"):
        message["tool_calls"] = reply["tool_calls"]
    if reply.get("reasoning_details"):
        message["reasoning_details"] = reply["reasoning_details"]  # some models need their reasoning sent back

    usage = data.get("usage") or {}
    return {
        "message": message,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens") or 0,
            "completion_tokens": usage.get("completion_tokens") or 0,
            "cost_usd": usage.get("cost") or 0.0,
        },
        "finish_reason": choice.get("finish_reason"),
        "provider": data.get("provider"),
    }


@lru_cache(maxsize=None)
def supported_parameters(model: str) -> frozenset[str]:
    """Request parameters at least one provider of this model accepts. Empty if the lookup fails."""
    request = urllib.request.Request(ENDPOINTS_URL.format(model=model), headers={"User-Agent": "filing-desk"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            endpoints = json.loads(response.read().decode("utf-8"))["data"]["endpoints"]
    except Exception:  # an unknown model or a network hiccup: send only parameters every model accepts
        return frozenset()
    return frozenset(name for endpoint in endpoints for name in endpoint.get("supported_parameters") or [])


def send_with_retries(request: urllib.request.Request, attempts: int = 3) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            if error.code in RETRY_STATUS and attempt < attempts:
                time.sleep(2 * attempt)
                continue
            raise ModelError(f"OpenRouter returned HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < attempts:
                time.sleep(2 * attempt)
                continue
            raise ModelError(f"Could not reach OpenRouter: {error}") from error
    raise ModelError("unreachable")
