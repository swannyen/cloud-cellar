"""Environment variables and SSM secrets, cached per Lambda container."""
import os

import boto3

# Parameter names under PARAM_PREFIX (PLAN.md §1.5).
SECRET_NAMES = ("telegram-token", "anthropic-key", "webhook-secret")

_secrets = None


def table_name() -> str:
    return os.environ["TABLE_NAME"]


def claude_model() -> str:
    return os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")


def allowed_chat_ids() -> frozenset:
    raw = os.environ.get("ALLOWED_CHAT_IDS", "")
    return frozenset(int(part) for part in raw.split(",") if part.strip())


def secret(name: str) -> str:
    """Return one secret by its short name, e.g. secret("webhook-secret")."""
    global _secrets
    if _secrets is None:
        _secrets = _load_secrets()
    return _secrets[name]


def _load_secrets() -> dict:
    prefix = os.environ["PARAM_PREFIX"].rstrip("/")
    response = boto3.client("ssm").get_parameters(
        Names=[f"{prefix}/{name}" for name in SECRET_NAMES],
        WithDecryption=True,
    )
    missing = response.get("InvalidParameters")
    if missing:
        raise RuntimeError(f"Missing SSM parameters: {', '.join(missing)}")
    return {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in response["Parameters"]}
