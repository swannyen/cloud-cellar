"""Claude Messages API over urllib, returning schema-shaped JSON (PLAN.md §5.1)."""
import json
import time
import urllib.error
import urllib.request

import config

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
TIMEOUT_SECONDS = 20          # for the whole call, including the one retry
MAX_TOKENS = 2048             # room for a long shopping-trip message
RETRY_STATUSES = (429, 500, 529)  # rate limited, server error, overloaded
RETRY_PAUSE_SECONDS = 1


class ClaudeError(Exception):
    """The call failed or produced no usable JSON. The message never includes the API key."""


def complete_json(system: str, user: str, schema: dict):
    """Return (data, message): the JSON the model produced and the raw API message."""
    message = _post({
        "model": config.claude_model(),
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "disabled"},
        "output_config": {
            "effort": "low",
            "format": {"type": "json_schema", "schema": schema},
        },
        "system": system,
        "messages": [{"role": "user", "content": user}],
    })
    # A refusal or a cut-off answer may not match the schema.
    stop_reason = message.get("stop_reason")
    if stop_reason in ("refusal", "max_tokens"):
        raise ClaudeError(f"stop_reason {stop_reason}")
    text = "".join(
        block.get("text", "") for block in message.get("content", []) if block.get("type") == "text"
    )
    try:
        return json.loads(text), message
    except ValueError:
        raise ClaudeError("output was not valid JSON") from None


def _post(body: dict) -> dict:
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": config.secret("anthropic-key"),
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
    )
    deadline = time.monotonic() + TIMEOUT_SECONDS
    for attempt in (1, 2):
        remaining = deadline - time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=max(remaining, 1)) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            can_retry = attempt == 1 and remaining > RETRY_PAUSE_SECONDS + 2
            if err.code in RETRY_STATUSES and can_retry:
                time.sleep(RETRY_PAUSE_SECONDS)
                continue
            raise ClaudeError(f"HTTP {err.code} {_error_detail(err)}") from None
        except OSError as err:  # URLError, timeouts, connection resets
            raise ClaudeError(type(err).__name__) from None


def _error_detail(err: urllib.error.HTTPError) -> str:
    """The API explains a failed call as {"error": {"type": ..., "message": ...}}."""
    try:
        error = json.load(err).get("error") or {}
        return f"{error.get('type', '')}: {error.get('message', '')}"
    except ValueError:
        return ""
