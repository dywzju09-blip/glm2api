"""SQLite 存储层：账号池、对外 Key、用量事件、系统设置。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('web','guest','official')),
  secret TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  priority INTEGER NOT NULL DEFAULT 0,
  weight INTEGER NOT NULL DEFAULT 1,
  note TEXT NOT NULL DEFAULT '',
  base_url TEXT NOT NULL DEFAULT '',
  limit_5h INTEGER NOT NULL DEFAULT 0,
  limit_7d INTEGER NOT NULL DEFAULT 0,
  limit_daily INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0,
  cooldown_until REAL NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  last_used_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS client_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  daily_limit INTEGER NOT NULL DEFAULT 0,
  rpm_limit INTEGER NOT NULL DEFAULT 0,
  allowed_models TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  last_used_at REAL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  account_id INTEGER,
  account_name TEXT NOT NULL DEFAULT '',
  key_id INTEGER,
  key_name TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  stream INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'success',
  error TEXT NOT NULL DEFAULT '',
  client_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_account ON usage_events(account_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_key ON usage_events(key_id, ts);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "strategy": "round_robin",          # round_robin | priority
    "request_timeout": 120,
    "max_concurrency_per_account": 5,
    "auto_disable": True,               # 连续硬失败后自动禁用
    "auto_disable_threshold": 3,
    "prefer_official": False,           # 模型同时可路由时优先走官方 API
    "official_models": [
        "glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4.5-flash",
        "glm-4-plus", "glm-4-air", "glm-4-flash", "glm-4-flashx",
        "glm-4-long", "glm-4v-plus", "glm-4v-flash",
        "cogview-4-250304", "cogview-3-flash",
    ],
    "official_base_url": "https://open.bigmodel.cn/api/paas/v4",
    "delete_conversation": True,
    "busy_max_retries": 8,
    "busy_retry_interval": 2.0,
}

ACCOUNT_FIELDS = (
    "id", "name", "type", "secret", "status", "priority", "weight", "note",
    "base_url", "limit_5h", "limit_7d", "limit_daily", "fail_count",
    "cooldown_until", "last_error", "last_used_at", "created_at", "updated_at",
)
KEY_FIELDS = (
    "id", "key", "name", "status", "daily_limit", "rpm_limit",
    "allowed_models", "note", "last_used_at", "created_at",
)


class Database:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "glmstudio.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- 基础 ----------
    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---------- 设置 ----------
    def get_settings(self) -> dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        for row in self.query("SELECT key, value FROM settings"):
            try:
                merged[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                continue
        return merged

    def save_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.get_settings()
        for key, value in updates.items():
            if key not in DEFAULT_SETTINGS:
                continue
            current[key] = value
            self.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        return current

    def get_meta(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------- 账号 ----------
    def list_accounts(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM accounts ORDER BY priority ASC, id ASC")

    def get_account(self, account_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM accounts WHERE id=?", (account_id,))

    def create_account(self, data: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO accounts(name, type, secret, status, priority, weight, note,"
                " base_url, limit_5h, limit_7d, limit_daily, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    data["name"], data["type"], data.get("secret", ""),
                    data.get("status", "active"), data.get("priority", 0),
                    data.get("weight", 1), data.get("note", ""),
                    data.get("base_url", ""), data.get("limit_5h", 0),
                    data.get("limit_7d", 0), data.get("limit_daily", 0), now, now,
                ),
            )
            self._conn.commit()
            account_id = int(cur.lastrowid)
        return self.get_account(account_id)  # type: ignore[return-value]

    def update_account(self, account_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "type", "secret", "status", "priority", "weight", "note",
                   "base_url", "limit_5h", "limit_7d", "limit_daily"}
        sets, params = [], []
        for field in allowed:
            if field in data:
                sets.append(f"{field}=?")
                params.append(data[field])
        if "status" in data and data["status"] == "active":
            sets += ["fail_count=0", "cooldown_until=0", "last_error=''"]
        if sets:
            sets.append("updated_at=?")
            params.append(time.time())
            params.append(account_id)
            self.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", tuple(params))
        return self.get_account(account_id)

    def delete_account(self, account_id: int) -> None:
        self.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def touch_account(self, account_id: int) -> None:
        self.execute("UPDATE accounts SET last_used_at=? WHERE id=?", (time.time(), account_id))

    def account_failure(self, account_id: int, fail_count: int, cooldown_until: float,
                        last_error: str, disable: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE accounts SET fail_count=?, cooldown_until=?, last_error=?,"
                " status=CASE WHEN ? THEN 'disabled' ELSE status END, updated_at=?"
                " WHERE id=?",
                (fail_count, cooldown_until, last_error[:500], 1 if disable else 0,
                 time.time(), account_id),
            )
            self._conn.commit()

    def account_success(self, account_id: int, new_secret: str | None) -> None:
        with self._lock:
            if new_secret:
                self._conn.execute(
                    "UPDATE accounts SET fail_count=0, cooldown_until=0, last_error='', secret=?,"
                    " last_used_at=?, updated_at=? WHERE id=?",
                    (new_secret, time.time(), time.time(), account_id),
                )
            else:
                self._conn.execute(
                    "UPDATE accounts SET fail_count=0, cooldown_until=0, last_error='',"
                    " last_used_at=?, updated_at=? WHERE id=?",
                    (time.time(), time.time(), account_id),
                )
            self._conn.commit()

    # ---------- 对外 Key ----------
    def list_keys(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM client_keys ORDER BY id DESC")

    def get_key_by_value(self, key: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM client_keys WHERE key=? AND status='active'", (key,))

    def count_active_keys(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS c FROM client_keys WHERE status='active'")
        return int(row["c"]) if row else 0

    def create_key(self, data: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO client_keys(key, name, status, daily_limit, rpm_limit,"
                " allowed_models, note, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (data["key"], data["name"], data.get("status", "active"),
                 data.get("daily_limit", 0), data.get("rpm_limit", 0),
                 data.get("allowed_models", ""), data.get("note", ""), now),
            )
            self._conn.commit()
        return self.query_one("SELECT * FROM client_keys WHERE key=?", (data["key"],))  # type: ignore[return-value]

    def update_key(self, key_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "status", "daily_limit", "rpm_limit", "allowed_models", "note"}
        sets, params = [], []
        for field in allowed:
            if field in data:
                sets.append(f"{field}=?")
                params.append(data[field])
        if sets:
            params.append(key_id)
            self.execute(f"UPDATE client_keys SET {', '.join(sets)} WHERE id=?", tuple(params))
        return self.query_one("SELECT * FROM client_keys WHERE id=?", (key_id,))

    def delete_key(self, key_id: int) -> None:
        self.execute("DELETE FROM client_keys WHERE id=?", (key_id,))

    def touch_key(self, key_id: int) -> None:
        self.execute("UPDATE client_keys SET last_used_at=? WHERE id=?", (time.time(), key_id))

    # ---------- 用量事件 ----------
    def add_event(self, data: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO usage_events(ts, account_id, account_name, key_id, key_name, model,"
            " channel, stream, prompt_tokens, completion_tokens, total_tokens, latency_ms,"
            " status, error, client_ip) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                data.get("ts", time.time()), data.get("account_id"),
                data.get("account_name", ""), data.get("key_id"),
                data.get("key_name", ""), data.get("model", ""),
                data.get("channel", ""), 1 if data.get("stream") else 0,
                data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
                data.get("total_tokens", 0), data.get("latency_ms", 0),
                data.get("status", "success"), data.get("error", "")[:500],
                data.get("client_ip", ""),
            ),
        )

    def prune_events(self, keep_seconds: float = 8 * 86400) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM usage_events WHERE ts < ?", (time.time() - keep_seconds,))
            self._conn.commit()
            return cur.rowcount or 0

    # ---------- 用量统计 ----------
    def account_window_usage(self, account_id: int) -> dict[str, int]:
        now = time.time()
        out = {"req_5h": 0, "tokens_5h": 0, "req_7d": 0, "tokens_7d": 0,
               "req_today": 0, "tokens_today": 0, "ok_7d": 0, "err_7d": 0}
        for row in self.query(
            "SELECT"
            " SUM(CASE WHEN ts>? THEN 1 ELSE 0 END) AS req_5h,"
            " SUM(CASE WHEN ts>? THEN total_tokens ELSE 0 END) AS tokens_5h,"
            " SUM(CASE WHEN ts>? THEN 1 ELSE 0 END) AS req_7d,"
            " SUM(CASE WHEN ts>? THEN total_tokens ELSE 0 END) AS tokens_7d,"
            " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok_7d,"
            " SUM(CASE WHEN status!='success' THEN 1 ELSE 0 END) AS err_7d"
            " FROM usage_events WHERE account_id=? AND ts>?",
            (now - 5 * 3600, now - 5 * 3600, now - 7 * 86400, now - 7 * 86400,
             account_id, now - 7 * 86400),
        ):
            out.update({k: int(row[k] or 0) for k in
                        ("req_5h", "tokens_5h", "req_7d", "tokens_7d", "ok_7d", "err_7d")})
        day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        for row in self.query(
            "SELECT COUNT(*) AS c, SUM(total_tokens) AS t FROM usage_events"
            " WHERE account_id=? AND ts>? AND status='success'",
            (account_id, day_start),
        ):
            out["req_today"] = int(row["c"] or 0)
            out["tokens_today"] = int(row["t"] or 0)
        return out

    def key_usage_today(self, key_id: int) -> dict[str, int]:
        day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        row = self.query_one(
            "SELECT COUNT(*) AS c, SUM(total_tokens) AS t FROM usage_events"
            " WHERE key_id=? AND ts>? AND status='success'", (key_id, day_start))
        return {"req_today": int(row["c"] or 0) if row else 0,
                "tokens_today": int(row["t"] or 0) if row else 0}

    def key_usage_window(self, key_id: int, seconds: float) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS c FROM usage_events WHERE key_id=? AND ts>?",
            (key_id, time.time() - seconds))
        return int(row["c"] or 0) if row else 0

    def overview_usage(self) -> dict[str, Any]:
        now = time.time()
        day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        out: dict[str, Any] = {}
        row = self.query_one(
            "SELECT COUNT(*) AS c, SUM(total_tokens) AS t FROM usage_events"
            " WHERE ts>? AND status='success'", (day_start,))
        out["today_requests"] = int(row["c"] or 0) if row else 0
        out["today_tokens"] = int(row["t"] or 0) if row else 0
        row = self.query_one(
            "SELECT SUM(total_tokens) AS t, COUNT(*) AS c,"
            " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok"
            " FROM usage_events WHERE ts>?", (now - 7 * 86400,))
        out["week_tokens"] = int(row["t"] or 0) if row else 0
        out["week_requests"] = int(row["c"] or 0) if row else 0
        out["week_ok"] = int(row["ok"] or 0) if row else 0
        row = self.query_one(
            "SELECT COUNT(*) AS c, SUM(total_tokens) AS t FROM usage_events"
            " WHERE ts>?", (now - 5 * 3600,))
        out["hour5_requests"] = int(row["c"] or 0) if row else 0
        out["hour5_tokens"] = int(row["t"] or 0) if row else 0
        return out

    def list_events(self, limit: int = 100, offset: int = 0,
                    status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM usage_events"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return self.query(sql, tuple(params))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_database() -> Database:
    data_dir = os.environ.get("DATA_DIR", "data")
    return Database(data_dir)
