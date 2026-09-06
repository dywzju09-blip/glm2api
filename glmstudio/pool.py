"""账号池与调度器：网页账号（chatglm.cn）+ 官方 API Key 统一调度。"""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .db import Database
from .glmcompat.config import (
    AppConfig,
    BUILTIN_EXPOSED_MODELS,
    DEFAULT_ASSISTANT_ID,
    DEFAULT_IMAGE_ASSISTANT_ID,
    DEFAULT_IMAGE_MODEL_NAME,
    GUEST_REFRESH_TOKEN_MARKER,
    MODEL_VARIANT_EXCLUDED_MODELS,
)
from .glmcompat.model_variants import expand_model_variants, split_model_features
from .glmcompat.services.glm_auth import GLMAccessTokenManager
from .glmcompat.services.glm_client import GLMWebClient, UpstreamAPIError

logger = logging.getLogger("glmstudio.pool")

WEB_MODELS: list[str] = expand_model_variants(
    BUILTIN_EXPOSED_MODELS, excluded_models=MODEL_VARIANT_EXCLUDED_MODELS)


class _PersistingAuthManager(GLMAccessTokenManager):
    """refresh_token 轮换后写回数据库而不是 token.txt。"""

    def __init__(self, config: AppConfig, log: logging.Logger,
                 on_rotate: Callable[[str], None]) -> None:
        super().__init__(config=config, logger=log)
        self._on_rotate = on_rotate

    def _persist_refresh_token(self, account_index: int, refresh_token: str) -> None:
        self._on_rotate(refresh_token)


class _SingleAccountClient(GLMWebClient):
    """绑定单个网页账号的 GLM 客户端（跨账号故障转移由调度器负责）。"""

    def __init__(self, config: AppConfig, log: logging.Logger,
                 on_token_rotate: Callable[[str], None]) -> None:
        super().__init__(config=config, logger=log)
        self.auth = _PersistingAuthManager(config, log, on_token_rotate)


class OfficialClient:
    """智谱开放平台官方 API 直连（本身即 OpenAI 兼容）。"""

    def __init__(self, api_key: str, base_url: str, timeout: int) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(self, payload: dict[str, Any], stream: bool):
        body = {k: v for k, v in payload.items()
                if k not in ("web_search", "deep_research", "reasoning_effort")}
        data = __import__("json").dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
            },
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise UpstreamAPIError(
                exc.code, f"智谱官方 API HTTP {exc.code}" + (f" | {detail}" if detail else "")
            ) from exc


class AccountRuntime:
    def __init__(self, row: dict[str, Any], client: Any) -> None:
        self.row = row
        self.client = client  # _SingleAccountClient | OfficialClient

    @property
    def id(self) -> int:
        return int(self.row["id"])

    @property
    def type(self) -> str:
        return str(self.row["type"])

    @property
    def is_official(self) -> bool:
        return self.type == "official"


class AccountPool:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.settings = db.get_settings()
        self._lock = threading.RLock()
        self._runtimes: dict[int, AccountRuntime] = {}
        self._cursor: dict[str, int] = {"web": 0, "official": 0}
        self._upstream_sig: tuple | None = None
        self.reload()

    def _upstream_signature(self) -> tuple:
        """这些设置参与上游客户端构建，变化时需要重建全部客户端。"""
        s = self.settings
        return (
            int(s["request_timeout"]),
            max(1, int(s["max_concurrency_per_account"])),
            int(s["busy_max_retries"]),
            float(s["busy_retry_interval"]),
            bool(s["delete_conversation"]),
            str(s["official_base_url"]),
        )

    # ---------- 配置 ----------
    def _upstream_config(self, secret: str, is_guest: bool) -> AppConfig:
        s = self.settings
        token = GUEST_REFRESH_TOKEN_MARKER if is_guest else secret
        return AppConfig(
            env_file_path=Path("/dev/null"),
            env_file_created=False,
            token_file_path=Path("/dev/null"),
            host="0.0.0.0",
            port=0,
            api_prefix="/v1",
            log_level="INFO",
            debug_dump_all=False,
            request_timeout=int(s["request_timeout"]),
            glm_base_url="https://chatglm.cn/chatglm",
            glm_use_guest_refresh_token=is_guest,
            glm_refresh_token=token,
            glm_refresh_tokens=[token],
            glm_assistant_id=DEFAULT_ASSISTANT_ID,
            glm_image_assistant_id=DEFAULT_IMAGE_ASSISTANT_ID,
            glm_image_model_name=DEFAULT_IMAGE_MODEL_NAME,
            glm_user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"),
            glm_delete_conversation=bool(s["delete_conversation"]),
            glm_max_concurrency=max(1, int(s["max_concurrency_per_account"])),
            glm_queue_wait_timeout=600,
            glm_busy_max_retries=int(s["busy_max_retries"]),
            glm_busy_retry_interval=float(s["busy_retry_interval"]),
            glm_guest_max_retries=3,
            blocked_tool_names=[],
            exposed_models=WEB_MODELS,
            model_aliases={name: name for name in WEB_MODELS},
            server_api_keys=[],
            admin_key="",
            cors_allow_origin="*",
        )

    def reload(self) -> None:
        self.settings = self.db.get_settings()
        sig = self._upstream_signature()
        rebuild_all = sig != self._upstream_sig
        rows = self.db.list_accounts()
        new: dict[int, AccountRuntime] = {}
        with self._lock:
            for row in rows:
                old = self._runtimes.get(int(row["id"]))
                secret_changed = rebuild_all or old is None \
                    or old.row["secret"] != row["secret"] \
                    or old.row["type"] != row["type"] or old.row["base_url"] != row["base_url"]
                if old is not None and not secret_changed:
                    old.row = row
                    new[old.id] = old
                    continue
                try:
                    client = self._build_client(row)
                    new[int(row["id"])] = AccountRuntime(row, client)
                except Exception as exc:
                    logger.error("构建账号客户端失败 id=%s name=%s error=%s",
                                 row["id"], row["name"], exc)
            self._runtimes = new
            self._upstream_sig = sig

    def _build_client(self, row: dict[str, Any]) -> Any:
        account_id = int(row["id"])
        if row["type"] == "official":
            base = row["base_url"].strip() or str(self.settings["official_base_url"])
            return OfficialClient(row["secret"], base, int(self.settings["request_timeout"]))
        is_guest = row["type"] == "guest" or not row["secret"].strip()

        def on_token_rotate(new_token: str) -> None:
            try:
                self.db.execute(
                    "UPDATE accounts SET secret=?, updated_at=? WHERE id=?",
                    (new_token, time.time(), account_id))
            except Exception as exc:
                logger.warning("refresh_token 写回数据库失败 account=%s error=%s",
                               account_id, exc)

        config = self._upstream_config(row["secret"], is_guest)
        return _SingleAccountClient(config, logger, on_token_rotate)

    # ---------- 选择 ----------
    def _eligible(self, rt: AccountRuntime) -> bool:
        row = rt.row
        if row["status"] != "active":
            return False
        if float(row["cooldown_until"] or 0) > time.time():
            return False
        usage = self.db.account_window_usage(rt.id)
        if int(row["limit_daily"] or 0) > 0 and usage["req_today"] >= int(row["limit_daily"]):
            return False
        if int(row["limit_5h"] or 0) > 0 and usage["tokens_5h"] >= int(row["limit_5h"]):
            return False
        if int(row["limit_7d"] or 0) > 0 and usage["tokens_7d"] >= int(row["limit_7d"]):
            return False
        return True

    def candidates(self, channel: str | None, exclude: set[int],
                   max_count: int = 3) -> list[AccountRuntime]:
        """按策略返回可用账号候选，channel: web|official|None(全部)。"""
        with self._lock:
            pool = [rt for rt in self._runtimes.values()
                    if rt.id not in exclude
                    and (channel is None or (rt.is_official == (channel == "official")))
                    and self._eligible(rt)]
        if not pool:
            return []
        pool.sort(key=lambda rt: (int(rt.row["priority"]), rt.id))
        strategy = self.settings.get("strategy", "round_robin")
        if strategy == "priority":
            return pool[:max_count]
        # 轮询：同优先级内按 cursor 起始旋转
        channel_key = channel or "all"
        start = self._cursor.get(channel_key, 0) % len(pool)
        ordered = pool[start:] + pool[:start]
        with self._lock:
            self._cursor[channel_key] = start + 1
        return ordered[:max_count]

    def runtime(self, account_id: int) -> AccountRuntime | None:
        with self._lock:
            return self._runtimes.get(account_id)

    def all_runtimes(self) -> list[AccountRuntime]:
        with self._lock:
            return list(self._runtimes.values())

    # ---------- 结果反馈 ----------
    def mark_success(self, account_id: int, new_secret: str | None = None) -> None:
        self.db.account_success(account_id, new_secret)
        rt = self.runtime(account_id)
        if rt:
            rt.row["fail_count"] = 0
            rt.row["cooldown_until"] = 0
            rt.row["last_error"] = ""

    def mark_failure(self, account_id: int, exc: Exception) -> str:
        """返回失败类别: auth | busy | network | unknown。"""
        rt = self.runtime(account_id)
        row = rt.row if rt else None
        if row is None:
            return "unknown"
        status_code = getattr(exc, "status_code", None)
        message = str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            status_code = exc.code
        is_auth = status_code in (401, 403) or "token" in message.lower()
        is_busy = status_code == 429 or "忙碌" in message or "请等待" in message
        is_network = isinstance(exc, (urllib.error.URLError, TimeoutError)) or \
            ("timed out" in message.lower())

        fail_count = int(row.get("fail_count") or 0) + 1
        now = time.time()
        if is_auth:
            category, cooldown = "auth", 600.0
        elif is_busy:
            # 官方通道的 429 是并发超限（瞬时），冷却时间远小于网页通道的忙碌
            category, cooldown = "busy", 15.0 if rt.is_official else 60.0
        elif is_network:
            category, cooldown = "network", 30.0
        else:
            category, cooldown = "unknown", 30.0

        threshold = int(self.settings.get("auto_disable_threshold", 3))
        disable = bool(self.settings.get("auto_disable", True)) and \
            category == "auth" and fail_count >= threshold
        self.db.account_failure(account_id, fail_count, now + cooldown,
                                message, disable)
        row["fail_count"] = fail_count
        row["cooldown_until"] = now + cooldown
        row["last_error"] = message[:200]
        if disable:
            row["status"] = "disabled"
            logger.error("账号连续认证失败已自动禁用 id=%s name=%s", account_id, row["name"])
        logger.warning("账号请求失败 id=%s name=%s category=%s fail=%s error=%s",
                       account_id, row["name"], category, fail_count, message[:200])
        return category

    # ---------- 模型路由 ----------
    def route_channel(self, model: str) -> str | None:
        base, _ = split_model_features(model or "")
        official_models = {str(m).lower() for m in self.settings.get("official_models", [])}
        web_hit = base.lower() in {m.lower() for m in WEB_MODELS}
        official_hit = (model or "").lower() in official_models
        has_web = any(not rt.is_official for rt in self.all_runtimes())
        has_official = any(rt.is_official for rt in self.all_runtimes())
        prefer_official = bool(self.settings.get("prefer_official", False))

        channels: list[str] = []
        if web_hit and has_web:
            channels.append("web")
        if official_hit and has_official:
            channels.append("official")
        if not channels:
            # 模型未匹配任何清单：按可用通道兜底（web 会透传给上游）
            if prefer_official:
                channels = [c for c in ("official", "web") if
                            (c == "official") == has_official or (c == "web") == has_web]
            else:
                channels = [c for c in ("web", "official") if
                            (c == "web") == has_web or (c == "official") == has_official]
        elif prefer_official and channels[0] == "web" and "official" in channels:
            channels = ["official", "web"]
        return channels[0] if channels else None

    def test_account(self, account_id: int) -> dict[str, Any]:
        """轻量连通性测试：web 刷新 access_token；官方 key 发一条最小请求。"""
        rt = self.runtime(account_id)
        if rt is None:
            return {"ok": False, "message": "账号不存在或客户端未初始化"}
        try:
            if rt.is_official:
                client: OfficialClient = rt.client
                import json as _json
                import urllib.error as _urlerror
                candidates = ["glm-4-flash"] + list(self.settings.get("official_models") or [])
                last_msg = ""
                for model in dict.fromkeys(candidates):
                    request = urllib.request.Request(
                        f"{client.base_url}/chat/completions",
                        data=_json.dumps({
                            "model": model,
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": 1, "stream": False,
                        }).encode("utf-8"),
                        method="POST",
                        headers={"Authorization": f"Bearer {client.api_key}",
                                 "Content-Type": "application/json"},
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=30) as resp:
                            _json.loads(resp.read().decode("utf-8"))
                        return {"ok": True,
                                "message": f"官方 API Key 可用（{model} 测试通过）"}
                    except _urlerror.HTTPError as exc:
                        detail = ""
                        try:
                            body = exc.read().decode("utf-8", errors="ignore")
                            payload = _json.loads(body)
                            detail = str((payload.get("error") or {}).get("message", body))[:120]
                        except Exception:  # noqa: BLE001
                            pass
                        last_msg = f"{model}: HTTP {exc.code}" + (f" {detail}" if detail else "")
                return {"ok": False, "message": f"全部模型测试失败；最后错误 → {last_msg}"}
            token = rt.client.auth.get_access_token_for_account(0)
            if token:
                return {"ok": True, "message": "refresh_token 有效，access_token 获取成功"}
            return {"ok": False, "message": "未获取到 access_token"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)[:300]}


def generate_api_key() -> str:
    import secrets
    return f"sk-glm-{secrets.token_hex(16)}"


def random_jitter(base: float) -> float:
    return base * (0.8 + random.random() * 0.4)
