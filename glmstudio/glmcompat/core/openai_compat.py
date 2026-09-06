"""OpenAI API compatibility helpers.

Provides:
- OpenAI-format IDs (chatcmpl-, resp_, msg_, fc_, call_)
- System fingerprint (model + date hash)
- OpenAI-format error response envelopes
- Standard response parameter defaults
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any


# ---------------------------------------------------------------------------
# ID generators — match OpenAI's format
# ---------------------------------------------------------------------------


def gen_chatcmpl_id() -> str:
    """OpenAI chat completion ID: chatcmpl-<24 hex chars>."""
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def gen_response_id() -> str:
    """OpenAI Responses API ID: resp_<24 hex chars>."""
    return f"resp_{uuid.uuid4().hex[:24]}"


def gen_message_id() -> str:
    """OpenAI message ID: msg_<24 hex chars>."""
    return f"msg_{uuid.uuid4().hex[:24]}"


def gen_function_call_id() -> str:
    """OpenAI function call ID: call_<24 hex chars>."""
    return f"call_{uuid.uuid4().hex[:24]}"


def gen_request_id() -> str:
    """OpenAI request ID: req_<24 hex chars>."""
    return f"req_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# System fingerprint
# ---------------------------------------------------------------------------


def system_fingerprint(model: str = "") -> str:
    """Return a system fingerprint in OpenAI's format: fp_<8 hex chars>.

    Based on model name + current date, so it changes daily (matching
    OpenAI's "deployment version" semantics) but is stable within a day
    for the same model.
    """
    if model:
        date_str = time.strftime("%Y%m%d")
        base = hashlib.md5(f"{model}:{date_str}".encode("utf-8")).hexdigest()[:8]
        return f"fp_{base}"
    # Fallback: process-level hash
    import os
    import platform
    salt = f"{platform.python_version()}-{platform.system()}-{os.getpid()}"
    return f"fp_{hashlib.md5(salt.encode('utf-8')).hexdigest()[:8]}"


# ---------------------------------------------------------------------------
# Error envelopes — match OpenAI's exact format
# ---------------------------------------------------------------------------


def make_error(
    message: str,
    *,
    error_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-format error envelope."""
    err: dict[str, Any] = {
        "message": message,
        "type": error_type,
        "param": param,
        "code": code,
    }
    if request_id:
        err["request_id"] = request_id
    return {"error": err}


# Standard error types
ERROR_INVALID_REQUEST = "invalid_request_error"
ERROR_AUTHENTICATION = "authentication_error"
ERROR_NOT_FOUND = "not_found_error"
ERROR_RATE_LIMIT = "rate_limit_exceeded"
ERROR_SERVER = "server_error"
ERROR_UPSTREAM = "upstream_error"

# Common error codes
CODE_MODEL_NOT_FOUND = "model_not_found"
CODE_INVALID_API_KEY = "invalid_api_key"
CODE_RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"


# ---------------------------------------------------------------------------
# HTTP status mapping
# ---------------------------------------------------------------------------

HTTP_STATUS_FOR_ERROR_TYPE: dict[str, int] = {
    ERROR_INVALID_REQUEST: 400,
    ERROR_AUTHENTICATION: 401,
    ERROR_NOT_FOUND: 404,
    ERROR_RATE_LIMIT: 429,
    ERROR_SERVER: 500,
    ERROR_UPSTREAM: 502,
}


def status_for_error_type(error_type: str, default: int = 400) -> int:
    return HTTP_STATUS_FOR_ERROR_TYPE.get(error_type, default)


# ---------------------------------------------------------------------------
# Common parameter defaults for chat completions
# ---------------------------------------------------------------------------

DEFAULT_TEMPERATURE = 1.0
DEFAULT_TOP_P = 1.0
DEFAULT_MAX_TOKENS = None
DEFAULT_N = 1
DEFAULT_STREAM = False


def now_timestamp() -> int:
    """Unix timestamp (seconds) for `created` field."""
    return int(time.time())
