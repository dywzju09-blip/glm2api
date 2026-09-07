"""对外 OpenAI/Anthropic 兼容接口：/v1/chat/completions、/v1/messages、/v1/models、/v1/images/generations。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Generator

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .db import Database
from .glmcompat.services.anthropic_adapter import (
    AnthropicStreamAccumulator,
    anthropic_to_openai,
    openai_to_anthropic_response,
)
from .glmcompat.services.glm_client import UpstreamAPIError
from .glmcompat.services.responses_adapter import (
    ResponsesStreamAccumulator,
    openai_to_responses,
    responses_to_openai,
)
from .pool import WEB_MODELS, AccountPool, AccountRuntime, OfficialClient

logger = logging.getLogger("glmstudio.api")

router = APIRouter()

MAX_ATTEMPTS = 4


def _error_response(message: str, status_code: int, error_type: str = "upstream_error") -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": status_code}},
    )


def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or None


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


class KeyContext:
    def __init__(self, key_row: dict[str, Any] | None, open_mode: bool) -> None:
        self.row = key_row
        self.open_mode = open_mode

    @property
    def id(self) -> int | None:
        return int(self.row["id"]) if self.row else None

    @property
    def name(self) -> str:
        return str(self.row["name"]) if self.row else ("开放模式" if self.open_mode else "未知")


def authenticate(db: Database, request: Request) -> tuple[KeyContext | None, JSONResponse | None]:
    if db.count_active_keys() == 0:
        return KeyContext(None, open_mode=True), None
    key = _extract_key(request)
    if not key:
        return None, _error_response("缺少 API Key，请在 Authorization: Bearer 或 x-api-key 中提供", 401, "auth_error")
    row = db.get_key_by_value(key)
    if row is None:
        return None, _error_response("API Key 无效或已禁用", 401, "auth_error")
    return KeyContext(row, open_mode=False), None


def enforce_key_limits(db: Database, ctx: KeyContext) -> JSONResponse | None:
    if ctx.row is None:
        return None
    rpm = int(ctx.row["rpm_limit"] or 0)
    daily = int(ctx.row["daily_limit"] or 0)
    if rpm > 0 and db.key_usage_window(ctx.id, 60) >= rpm:
        return _error_response(f"已达到该 Key 的 RPM 限制 ({rpm})", 429, "rate_limit")
    if daily > 0 and db.key_usage_today(ctx.id)["req_today"] >= daily:
        return _error_response(f"已达到该 Key 的每日请求限制 ({daily})", 429, "rate_limit")
    return None


def model_allowed(ctx: KeyContext, model: str) -> bool:
    if ctx.row is None:
        return True
    allowed = str(ctx.row["allowed_models"] or "").strip()
    if not allowed:
        return True
    names = {item.strip().lower() for item in allowed.split(",") if item.strip()}
    return (model or "").lower() in names


def _invoke_chat(rt: AccountRuntime, payload: dict[str, Any], stream: bool):
    """在指定账号上执行 chat 调用，返回 (result, usage_extractor)。"""
    if rt.is_official:
        client: OfficialClient = rt.client
        response = client.chat(payload, stream)
        if stream:
            return response, "official_stream"
        raw = response.read().decode("utf-8", errors="ignore")
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("error"):
            raise UpstreamAPIError(502, str(data["error"]), data)
        return data, "json"
    client = rt.client
    if stream:
        return client.stream_chat_completion(payload), "web_stream"
    result, _conv = client.chat_completion(payload)
    return result, "json"


def call_with_failover(pool: AccountPool, payload: dict[str, Any], stream: bool,
                       channel: str | None = None):
    """跨账号故障转移执行 chat；成功返回 (runtime, result, kind)。"""
    exclude: set[int] = set()
    last_exc: Exception | None = None
    for _ in range(MAX_ATTEMPTS):
        wanted = channel or pool.route_channel(str(payload.get("model", "")))
        candidates = pool.candidates(wanted, exclude, max_count=1)
        if not candidates and wanted:
            fallback = "official" if wanted == "web" else "web"
            candidates = pool.candidates(fallback, exclude, max_count=1)
        if not candidates:
            candidates = pool.candidates(None, exclude, max_count=1)
        if not candidates:
            break
        rt = candidates[0]
        try:
            result, kind = _invoke_chat(rt, payload, stream)
            return rt, result, kind
        except UpstreamAPIError as exc:
            # 客户端参数错误（如 max_tokens 非法）：任何账号都会同样报错，
            # 不计账号失败、不切换，直接透传给调用方
            if pool.is_client_error(exc):
                raise
            status = getattr(exc, "status_code", None)
            # 官方通道的 429 若是并发超限（瞬时性的）：退避后在原账号重试，
            # 等并发的其他请求完成腾出槽位；若是额度耗尽则重试无意义，直接切换账号。
            if status == 429 and rt.is_official and not pool.is_quota_error(exc):
                for attempt in range(3):
                    time.sleep(1.5)
                    try:
                        result, kind = _invoke_chat(rt, payload, stream)
                        logger.info("官方通道并发超限重试成功 attempt=%s account=%s", attempt + 1, rt.id)
                        return rt, result, kind
                    except UpstreamAPIError as retry_exc:
                        exc = retry_exc
                        if getattr(retry_exc, "status_code", None) != 429:
                            break
                pool.mark_failure(rt.id, exc)
                exclude.add(rt.id)
                last_exc = exc
                logger.warning("官方通道重试后仍失败 account=%s error=%s", rt.id, str(exc)[:200])
                continue
            pool.mark_failure(rt.id, exc)
            exclude.add(rt.id)
            last_exc = exc
            logger.warning("账号调用失败，切换下一个 id=%s error=%s", rt.id, str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 — 需要按任意上游错误切换账号
            pool.mark_failure(rt.id, exc)
            exclude.add(rt.id)
            last_exc = exc
            logger.warning("账号调用失败，切换下一个 id=%s error=%s", rt.id, str(exc)[:200])
    if last_exc is not None:
        raise last_exc
    raise UpstreamAPIError(503, "账号池中没有可用账号（全部禁用、冷却中或超出限额）")


class _UsageRecorder:
    """从 SSE 字节流中抓取 usage 字段，请求结束时写用量事件。"""

    def __init__(self, db: Database, pool: AccountPool, rt: AccountRuntime,
                 ctx: KeyContext, model: str, stream: bool, ip: str, start: float) -> None:
        self.db = db
        self.pool = pool
        self.rt = rt
        self.ctx = ctx
        self.model = model
        self.stream = stream
        self.ip = ip
        self.start = start
        self.usage: dict[str, int] = {}
        self.done = False

    def scan(self, chunk: bytes) -> None:
        try:
            text = chunk.decode("utf-8", errors="ignore")
        except Exception:
            return
        if '"usage"' not in text:
            return
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict):
                self.usage = {k: int(v or 0) for k, v in usage.items()
                              if k in ("prompt_tokens", "completion_tokens", "total_tokens")}

    def finish(self, status: str = "success", error: str = "") -> None:
        if self.done:
            return
        self.done = True
        latency_ms = int((time.time() - self.start) * 1000)
        prompt_tokens = self.usage.get("prompt_tokens", 0)
        completion_tokens = self.usage.get("completion_tokens", 0)
        total = self.usage.get("total_tokens") or (prompt_tokens + completion_tokens)
        self.db.add_event({
            "account_id": self.rt.id, "account_name": self.rt.row["name"],
            "key_id": self.ctx.id, "key_name": self.ctx.name,
            "model": self.model, "channel": "official" if self.rt.is_official else "web",
            "stream": self.stream, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "total_tokens": total,
            "latency_ms": latency_ms, "status": status, "error": error,
            "client_ip": self.ip,
        })
        if status == "success":
            self.pool.mark_success(self.rt.id)
        else:
            self.pool.db.touch_account(self.rt.id)
        if self.ctx.id:
            self.db.touch_key(self.ctx.id)


def _stream_response(kind: str, raw, recorder: _UsageRecorder) -> StreamingResponse:
    def generate() -> Generator[bytes, None, None]:
        status, error = "success", ""
        try:
            if kind == "web_stream":
                for chunk in raw:
                    recorder.scan(chunk)
                    yield chunk
            else:  # official_stream: urllib 响应对象
                buffer = b""
                while True:
                    piece = raw.read(4096)
                    if not piece:
                        break
                    buffer += piece
                    while b"\n\n" in buffer:
                        block, buffer = buffer.split(b"\n\n", 1)
                        recorder.scan(block)
                        yield block + b"\n\n"
                if buffer.strip():
                    recorder.scan(buffer)
                    yield buffer + b"\n\n"
        except Exception as exc:  # noqa: BLE001
            status, error = "error", str(exc)
            logger.warning("流式转发中断 error=%s", error[:200])
            yield f'data: {json.dumps({"error": {"message": error, "type": "upstream_error"}})}\n\n'.encode()
            yield b"data: [DONE]\n\n"
        finally:
            recorder.finish(status, error)
            try:
                if kind == "web_stream":
                    raw.close()
                else:
                    raw.close()
            except Exception:
                pass

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/v1/models")
def list_models(request: Request):
    state = request.app.state
    ctx, err = authenticate(state.db, request)
    if err:
        return err
    pool: AccountPool = state.pool
    models: list[str] = []
    has_web = any(not rt.is_official for rt in pool.all_runtimes())
    has_official = any(rt.is_official for rt in pool.all_runtimes())
    if has_web:
        models.extend(WEB_MODELS)
    if has_official:
        for name in pool.settings.get("official_models", []):
            if name not in models:
                models.append(name)
    if not models:
        models = list(WEB_MODELS)
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "glm-studio"}
                                       for m in models]}


@router.post("/v1/chat/completions")
def chat_completions(request: Request, payload: dict = Body(...)):
    state = request.app.state
    db: Database = state.db
    pool: AccountPool = state.pool
    start = time.time()

    ctx, err = authenticate(db, request)
    if err:
        return err
    limit_err = enforce_key_limits(db, ctx)
    if limit_err:
        return limit_err

    model = str(payload.get("model", "")).strip()
    if not model:
        return _error_response("缺少 model 字段", 400, "invalid_request")
    if not model_allowed(ctx, model):
        return _error_response(f"该 Key 无权使用模型 {model}", 403, "permission_error")
    stream = bool(payload.get("stream"))

    try:
        rt, result, kind = call_with_failover(pool, payload, stream)
    except UpstreamAPIError as exc:
        db.add_event({"key_id": ctx.id, "key_name": ctx.name, "model": model,
                      "status": "error", "error": str(exc), "latency_ms": int((time.time() - start) * 1000),
                      "client_ip": _client_ip(request)})
        return _error_response(str(exc), exc.status_code if exc.status_code else 502)
    except Exception as exc:  # noqa: BLE001
        db.add_event({"key_id": ctx.id, "key_name": ctx.name, "model": model,
                      "status": "error", "error": str(exc), "latency_ms": int((time.time() - start) * 1000),
                      "client_ip": _client_ip(request)})
        return _error_response(f"上游调用失败: {exc}", 502)

    recorder = _UsageRecorder(db, pool, rt, ctx, model, stream, _client_ip(request), start)
    if stream:
        return _stream_response(kind, result, recorder)

    usage = result.get("usage") if isinstance(result, dict) else None
    if isinstance(usage, dict):
        recorder.usage = {k: int(v or 0) for k, v in usage.items()
                          if k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    recorder.finish()
    return JSONResponse(result)


@router.post("/v1/messages")
def anthropic_messages(request: Request, payload: dict = Body(...)):
    state = request.app.state
    db: Database = state.db
    pool: AccountPool = state.pool
    start = time.time()

    ctx, err = authenticate(db, request)
    if err:
        return err
    limit_err = enforce_key_limits(db, ctx)
    if limit_err:
        return limit_err

    openai_payload = anthropic_to_openai(payload)
    model = str(openai_payload.get("model", "")).strip()
    if not model_allowed(ctx, model):
        return JSONResponse(status_code=403, content={
            "type": "error", "error": {"type": "permission_error",
                                       "message": f"该 Key 无权使用模型 {model}"}})
    stream = bool(payload.get("stream"))

    try:
        rt, result, kind = call_with_failover(pool, openai_payload, stream)
    except Exception as exc:  # noqa: BLE001
        db.add_event({"key_id": ctx.id, "key_name": ctx.name, "model": model,
                      "status": "error", "error": str(exc), "latency_ms": int((time.time() - start) * 1000),
                      "client_ip": _client_ip(request)})
        return JSONResponse(status_code=502, content={
            "type": "error", "error": {"type": "upstream_error", "message": str(exc)[:500]}})

    requested_model = str(payload.get("model", model))
    recorder = _UsageRecorder(db, pool, rt, ctx, model, stream, _client_ip(request), start)
    if stream:
        accumulator = AnthropicStreamAccumulator(requested_model)
        source_kind = kind

        def generate() -> Generator[bytes, None, None]:
            status, error = "success", ""
            try:
                if source_kind == "web_stream":
                    for chunk in result:
                        recorder.scan(chunk)
                        for event in accumulator.feed_chunk(chunk):
                            yield event.encode("utf-8")
                else:
                    buffer = b""
                    while True:
                        piece = result.read(4096)
                        if not piece:
                            break
                        buffer += piece
                        while b"\n\n" in buffer:
                            block, buffer = buffer.split(b"\n\n", 1)
                            recorder.scan(block)
                            for event in accumulator.feed_chunk(block + b"\n\n"):
                                yield event.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                status, error = "error", str(exc)
                yield f'event: error\ndata: {json.dumps({"type": "error", "error": {"type": "upstream_error", "message": error}})}\n\n'.encode()
            finally:
                recorder.finish(status, error)
                try:
                    result.close()
                except Exception:
                    pass

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    anthropic_result = openai_to_anthropic_response(result, requested_model)
    usage = anthropic_result.get("usage", {})
    recorder.usage = {"prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                      "completion_tokens": int(usage.get("output_tokens", 0) or 0)}
    recorder.finish()
    return JSONResponse(anthropic_result)


@router.post("/v1/responses")
def openai_responses(request: Request, payload: dict = Body(...)):
    """OpenAI Responses 协议（Codex 等客户端使用）。"""
    state = request.app.state
    db: Database = state.db
    pool: AccountPool = state.pool
    start = time.time()

    ctx, err = authenticate(db, request)
    if err:
        return err
    limit_err = enforce_key_limits(db, ctx)
    if limit_err:
        return limit_err

    openai_payload = responses_to_openai(payload)
    model = str(openai_payload.get("model", "")).strip()
    if not model_allowed(ctx, model):
        return _error_response(f"该 Key 无权使用模型 {model}", 403, "permission_error")
    stream = bool(payload.get("stream"))

    try:
        rt, result, kind = call_with_failover(pool, openai_payload, stream)
    except Exception as exc:  # noqa: BLE001
        db.add_event({"key_id": ctx.id, "key_name": ctx.name, "model": model,
                      "status": "error", "error": str(exc), "latency_ms": int((time.time() - start) * 1000),
                      "client_ip": _client_ip(request)})
        return _error_response(f"上游调用失败: {exc}", 502)

    requested_model = str(payload.get("model", model))
    recorder = _UsageRecorder(db, pool, rt, ctx, model, stream, _client_ip(request), start)
    if stream:
        accumulator = ResponsesStreamAccumulator(requested_model)
        source_kind = kind

        def generate() -> Generator[bytes, None, None]:
            status, error = "success", ""
            try:
                for event in accumulator.start_response():
                    yield event.encode("utf-8")
                if source_kind == "web_stream":
                    for chunk in result:
                        recorder.scan(chunk)
                        for event in accumulator.feed_chunk(chunk):
                            yield event.encode("utf-8")
                else:
                    buffer = b""
                    while True:
                        piece = result.read(4096)
                        if not piece:
                            break
                        buffer += piece
                        while b"\n\n" in buffer:
                            block, buffer = buffer.split(b"\n\n", 1)
                            recorder.scan(block)
                            for event in accumulator.feed_chunk(block + b"\n\n"):
                                yield event.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                status, error = "error", str(exc)
                yield f'data: {json.dumps({"type": "error", "message": error})}\n\n'.encode()
            finally:
                recorder.finish(status, error)
                try:
                    result.close()
                except Exception:
                    pass

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    responses_result = openai_to_responses(result, requested_model)
    usage = responses_result.get("usage") or {}
    recorder.usage = {"prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                      "completion_tokens": int(usage.get("output_tokens", 0) or 0)}
    recorder.finish()
    return JSONResponse(responses_result)


@router.post("/v1/images/generations")
def images_generations(request: Request, payload: dict = Body(...)):
    state = request.app.state
    db: Database = state.db
    pool: AccountPool = state.pool
    start = time.time()

    ctx, err = authenticate(db, request)
    if err:
        return err

    exclude: set[int] = set()
    for _ in range(MAX_ATTEMPTS):
        candidates = [rt for rt in pool.candidates("web", exclude, max_count=1)]
        if not candidates:
            return _error_response("没有可用的网页账号用于绘图", 503)
        rt = candidates[0]
        try:
            result = rt.client.generate_images(payload)
            recorder = _UsageRecorder(db, pool, rt, ctx,
                                      str(payload.get("model", "glm-image-1")),
                                      False, _client_ip(request), start)
            recorder.finish()
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            pool.mark_failure(rt.id, exc)
            exclude.add(rt.id)
    return _error_response("绘图请求失败：所有网页账号均不可用", 502)
