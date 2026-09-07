"""管理面板 API：账号池、对外 Key、用量、日志、设置。"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from . import __version__
from . import quota as quota_mod
from .db import Database
from .pool import AccountPool, WEB_MODELS, generate_api_key

logger = logging.getLogger("glmstudio.admin")

router = APIRouter(prefix="/admin/api")


def _admin(request: Request) -> str:
    key = request.headers.get("x-admin-key", "")
    expected = request.app.state.admin_key
    if not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="管理密钥错误")
    return key


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 12:
        return secret[:3] + "***"
    return f"{secret[:8]}...{secret[-4:]} ({len(secret)}字符)"


@router.post("/login")
def login(request: Request, payload: dict = Body(...)):
    key = str(payload.get("key", ""))
    expected = request.app.state.admin_key
    if not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="管理密钥错误")
    return {"ok": True, "version": __version__}


def _parse_quota(row: dict[str, Any]) -> dict[str, Any] | None:
    import json as _json
    raw = str(row.get("quota_json") or "")
    if not raw:
        return None
    try:
        data = _json.loads(raw)
        return data if isinstance(data, dict) and data.get("windows") else None
    except (ValueError, TypeError):
        return None


def _refresh_quota(db: Database, pool: AccountPool, row: dict[str, Any]) -> str:
    """拉取官方账号的 CodingPlan 真实额度并存库，返回错误信息（空=成功）。"""
    if row["type"] != "official":
        return ""
    try:
        data = quota_mod.fetch_plan_quota(row["secret"], row["base_url"])
        db.save_quota(int(row["id"]), data)
        pool.update_quota_cache(int(row["id"]), data)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"额度拉取失败: {str(exc)[:150]}"


def _account_view(db: Database, pool: AccountPool, row: dict[str, Any]) -> dict[str, Any]:
    usage = db.account_window_usage(int(row["id"]))
    total_7d = usage["ok_7d"] + usage["err_7d"]
    cooldown_left = max(0.0, float(row["cooldown_until"] or 0) - time.time())
    limited = (int(row["limit_daily"] or 0) > 0 and usage["req_today"] >= int(row["limit_daily"])) or \
              (int(row["limit_5h"] or 0) > 0 and usage["tokens_5h"] >= int(row["limit_5h"])) or \
              (int(row["limit_7d"] or 0) > 0 and usage["tokens_7d"] >= int(row["limit_7d"])) or \
              (row["type"] == "official" and AccountPool.quota_exhausted(row))
    if row["status"] != "active":
        state = "disabled"
    elif cooldown_left > 0:
        state = "cooldown"
    elif limited:
        state = "exhausted"
    else:
        state = "active"
    return {
        **{k: row[k] for k in (
            "id", "name", "type", "status", "priority", "weight", "note", "base_url",
            "limit_5h", "limit_7d", "limit_daily", "fail_count", "last_error",
            "last_used_at", "created_at")},
        "secret_preview": _mask_secret(str(row["secret"])),
        "quota": _parse_quota(row),
        "state": state,
        "cooldown_left": round(cooldown_left),
        "usage": usage,
        "success_rate_7d": round(usage["ok_7d"] * 100 / total_7d, 1) if total_7d else None,
    }


@router.get("/overview")
def overview(request: Request):
    _admin(request)
    state = request.app.state
    db: Database = state.db
    pool: AccountPool = state.pool
    accounts = db.list_accounts()
    usage = db.overview_usage()
    week_requests = usage.get("week_requests", 0) or 0
    return {
        "version": __version__,
        "usage": usage,
        "success_rate_7d": round(usage["week_ok"] * 100 / week_requests, 1) if week_requests else None,
        "counts": {
            "accounts_total": len(accounts),
            "accounts_active": sum(1 for a in accounts if a["status"] == "active"),
            "web_accounts": sum(1 for a in accounts if a["type"] != "official"),
            "official_accounts": sum(1 for a in accounts if a["type"] == "official"),
            "keys_total": len(db.list_keys()),
            "keys_active": db.count_active_keys(),
            "open_mode": db.count_active_keys() == 0,
        },
        "daily": db.daily_usage(14),
        "models": {"web": WEB_MODELS, "official": pool.settings.get("official_models", [])},
    }


# ---------------- 账号 ----------------
@router.get("/accounts")
def list_accounts(request: Request):
    _admin(request)
    state = request.app.state
    return {"accounts": [_account_view(state.db, state.pool, row)
                         for row in state.db.list_accounts()]}


@router.post("/accounts")
def create_account(request: Request, payload: dict = Body(...)):
    _admin(request)
    state = request.app.state
    name = str(payload.get("name", "")).strip()
    account_type = str(payload.get("type", "")).strip()
    if not name:
        raise HTTPException(400, "请填写账号名称")
    if account_type not in ("web", "guest", "official"):
        raise HTTPException(400, "账号类型必须是 web / guest / official")
    secret = str(payload.get("secret", "")).strip()
    if account_type == "web" and not secret:
        raise HTTPException(400, "网页账号必须填写 refresh_token")
    if account_type == "official" and not secret:
        raise HTTPException(400, "官方账号必须填写 API Key")
    row = state.db.create_account({
        "name": name, "type": account_type, "secret": secret,
        "priority": int(payload.get("priority", 0) or 0),
        "weight": int(payload.get("weight", 1) or 1),
        "note": str(payload.get("note", "") or ""),
        "base_url": str(payload.get("base_url", "") or "").strip(),
        "limit_5h": int(payload.get("limit_5h", 0) or 0),
        "limit_7d": int(payload.get("limit_7d", 0) or 0),
        "limit_daily": int(payload.get("limit_daily", 0) or 0),
    })
    if account_type == "official":
        _refresh_quota(state.db, state.pool, row)  # 新增官方账号时立即拉取 CodingPlan 真实额度
        row = state.db.get_account(int(row["id"])) or row
    state.pool.reload()
    logger.info("新增账号 id=%s name=%s type=%s", row["id"], name, account_type)
    return {"account": _account_view(state.db, state.pool, row)}


@router.patch("/accounts/{account_id}")
def update_account(request: Request, account_id: int, payload: dict = Body(...)):
    _admin(request)
    state = request.app.state
    if state.db.get_account(account_id) is None:
        raise HTTPException(404, "账号不存在")
    data: dict[str, Any] = {}
    for field in ("name", "note", "base_url"):
        if field in payload:
            data[field] = str(payload[field]).strip()
    for field in ("priority", "weight", "limit_5h", "limit_7d", "limit_daily"):
        if field in payload:
            data[field] = int(payload[field] or 0)
    if "secret" in payload and str(payload.get("secret", "")).strip():
        data["secret"] = str(payload["secret"]).strip()
    if "type" in payload and str(payload["type"]).strip() in ("web", "guest", "official"):
        data["type"] = str(payload["type"]).strip()
    if "status" in payload and str(payload["status"]).strip() in ("active", "disabled"):
        data["status"] = str(payload["status"]).strip()
    row = state.db.update_account(account_id, data)
    state.pool.reload()
    return {"account": _account_view(state.db, state.pool, row)}


@router.delete("/accounts/{account_id}")
def delete_account(request: Request, account_id: int):
    _admin(request)
    state = request.app.state
    if state.db.get_account(account_id) is None:
        raise HTTPException(404, "账号不存在")
    state.db.delete_account(account_id)
    state.pool.reload()
    return {"ok": True}


@router.post("/accounts/{account_id}/test")
def test_account(request: Request, account_id: int):
    _admin(request)
    state = request.app.state
    if state.db.get_account(account_id) is None:
        raise HTTPException(404, "账号不存在")
    result = state.pool.test_account(account_id)
    row = state.db.get_account(account_id)
    if row and row["type"] == "official":
        quota_err = _refresh_quota(state.db, state.pool, row)
        if quota_err:
            result.setdefault("message", "")
            result["message"] = (str(result.get("message", "")) + "；" + quota_err).strip("；")
    return result


@router.post("/accounts/{account_id}/quota")
def refresh_quota(request: Request, account_id: int):
    _admin(request)
    state = request.app.state
    row = state.db.get_account(account_id)
    if row is None:
        raise HTTPException(404, "账号不存在")
    err = _refresh_quota(state.db, state.pool, row)
    if err:
        raise HTTPException(502, err)
    row = state.db.get_account(account_id)
    return {"ok": True, "quota": _parse_quota(row or {})}


# ---------------- 对外 Key ----------------
@router.get("/keys")
def list_keys(request: Request):
    _admin(request)
    state = request.app.state
    keys = []
    for row in state.db.list_keys():
        usage = state.db.key_usage_today(int(row["id"]))
        keys.append({
            **{k: row[k] for k in (
                "id", "key", "name", "status", "daily_limit", "rpm_limit",
                "allowed_models", "note", "last_used_at", "created_at")},
            "usage_today": usage,
        })
    return {"keys": keys}


@router.post("/keys")
def create_key(request: Request, payload: dict = Body(...)):
    _admin(request)
    state = request.app.state
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "请填写 Key 名称")
    row = state.db.create_key({
        "key": generate_api_key(),
        "name": name,
        "daily_limit": int(payload.get("daily_limit", 0) or 0),
        "rpm_limit": int(payload.get("rpm_limit", 0) or 0),
        "allowed_models": str(payload.get("allowed_models", "") or "").strip(),
        "note": str(payload.get("note", "") or ""),
    })
    return {"key": row}


@router.patch("/keys/{key_id}")
def update_key(request: Request, key_id: int, payload: dict = Body(...)):
    _admin(request)
    state = request.app.state
    if state.db.query_one("SELECT id FROM client_keys WHERE id=?", (key_id,)) is None:
        raise HTTPException(404, "Key 不存在")
    data: dict[str, Any] = {}
    for field in ("name", "allowed_models", "note"):
        if field in payload:
            data[field] = str(payload[field]).strip()
    for field in ("daily_limit", "rpm_limit"):
        if field in payload:
            data[field] = int(payload[field] or 0)
    if "status" in payload and str(payload["status"]).strip() in ("active", "disabled"):
        data["status"] = str(payload["status"]).strip()
    row = state.db.update_key(key_id, data)
    return {"key": row}


@router.delete("/keys/{key_id}")
def delete_key(request: Request, key_id: int):
    _admin(request)
    state = request.app.state
    if state.db.query_one("SELECT id FROM client_keys WHERE id=?", (key_id,)) is None:
        raise HTTPException(404, "Key 不存在")
    state.db.delete_key(key_id)
    return {"ok": True}


# ---------------- 日志 ----------------
@router.get("/logs")
def list_logs(request: Request, limit: int = 100, offset: int = 0, status: str = ""):
    _admin(request)
    state = request.app.state
    limit = max(1, min(limit, 500))
    events = state.db.list_events(limit=limit, offset=offset,
                                  status=status.strip() or None)
    return {"events": events}


# ---------------- 设置 ----------------
@router.get("/settings")
def get_settings(request: Request):
    _admin(request)
    return {"settings": request.app.state.pool.settings,
            "web_models": WEB_MODELS}


@router.put("/settings")
def put_settings(request: Request, payload: dict = Body(...)):
    _admin(request)
    state = request.app.state
    updates = {k: v for k, v in payload.items() if k not in ("admin_key",)}
    settings = state.db.save_settings(updates)
    state.pool.reload()
    return {"settings": settings}
