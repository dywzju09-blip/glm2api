"""查询智谱 CodingPlan 官方用量/额度（/api/monitor/usage/*）。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode


def _monitor_base(base_url: str) -> str:
    """从账号的 base_url 提取平台根地址（scheme + host）。"""
    base = (base_url or "").strip() or "https://open.bigmodel.cn"
    if "://" not in base:
        base = "https://" + base
    return base.split("/")[0] + "//" + base.split("/")[2] if len(base.split("/")) > 2 else base


def _get_json(url: str, api_key: str, timeout: int = 15) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={
        "Authorization": api_key,
        "Accept-Language": "zh-CN,zh",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _window_label(item: dict[str, Any]) -> str:
    unit, number = item.get("unit"), item.get("number")
    if unit == 3 and number == 5:
        return "5h"
    if unit == 6:
        return "月" if number == 1 else f"{number}月"
    if unit == 2:
        return f"{number}天"
    return f"{number}周期"


def fetch_plan_quota(api_key: str, base_url: str = "") -> dict[str, Any]:
    """拉取 CodingPlan 额度。成功返回 {level, windows[], updated_at}；失败抛异常。"""
    base = _monitor_base(base_url)
    data = _get_json(f"{base}/api/monitor/usage/quota/limit", api_key)
    payload = data.get("data") or {}
    windows = []
    for item in payload.get("limits", []):
        if not isinstance(item, dict):
            continue
        reset_ms = item.get("nextResetTime")
        windows.append({
            "label": _window_label(item),
            "used": int(item.get("currentValue") or 0),
            "total": int(item.get("usage") or 0),
            "remaining": int(item.get("remaining") or 0),
            "percent": float(item.get("percentage") or 0),
            "reset_at": round(reset_ms / 1000, 0) if reset_ms else None,
        })
    windows.sort(key=lambda w: 0 if w["label"] == "5h" else 1)
    return {"level": payload.get("level") or "", "windows": windows, "updated_at": time.time()}


def fetch_model_usage(api_key: str, base_url: str = "", hours: int = 24) -> dict[str, Any] | None:
    """拉取近 N 小时逐小时用量（best-effort，失败返回 None）。"""
    from datetime import datetime, timedelta
    try:
        base = _monitor_base(base_url)
        now = datetime.now()
        params = urlencode({
            "startTime": (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": now.strftime("%Y-%m-%d %H:%M:%S"),
        })
        data = _get_json(f"{base}/api/monitor/usage/model-usage?{params}", api_key)
        return data.get("data") or None
    except Exception:  # noqa: BLE001
        return None
