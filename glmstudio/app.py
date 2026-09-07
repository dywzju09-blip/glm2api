"""FastAPI 应用组装。"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .admin_api import router as admin_router
from .db import open_database
from .pool import AccountPool
from .public_api import router as public_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("glmstudio")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _resolve_admin_key(db) -> str:
    env_key = os.environ.get("ADMIN_KEY", "").strip()
    if env_key:
        return env_key
    stored = db.get_meta("admin_key")
    if stored:
        return stored
    generated = f"glm-{secrets.token_hex(8)}"
    db.set_meta("admin_key", generated)
    return generated


def _start_maintenance(db, pool) -> None:
    def worker() -> None:
        import time
        while True:
            time.sleep(3600)
            try:
                removed = db.prune_events()
                if removed:
                    logger.info("清理过期用量事件 %s 条", removed)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理用量事件失败: %s", exc)

    def quota_worker() -> None:
        import time
        from . import quota as quota_mod
        while True:
            time.sleep(900)
            try:
                for row in db.list_accounts():
                    if row["type"] != "official" or row["status"] != "active":
                        continue
                    try:
                        data = quota_mod.fetch_plan_quota(row["secret"], row["base_url"])
                        db.save_quota(int(row["id"]), data)
                        pool.update_quota_cache(int(row["id"]), data)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("账号额度刷新失败 id=%s error=%s", row["id"], exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("额度刷新循环异常: %s", exc)

    threading.Thread(target=worker, daemon=True, name="maintenance").start()
    threading.Thread(target=quota_worker, daemon=True, name="quota-refresh").start()


def create_app() -> FastAPI:
    app = FastAPI(title="GLM Studio", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    db = open_database()
    pool = AccountPool(db)
    app.state.db = db
    app.state.pool = pool
    app.state.admin_key = _resolve_admin_key(db)

    app.include_router(public_router)
    app.include_router(admin_router)

    if STATIC_DIR.exists():
        app.mount("/admin/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")

        @app.get("/admin", include_in_schema=False)
        @app.get("/admin/{rest:path}", include_in_schema=False)
        def admin_index(rest: str = ""):
            target = STATIC_DIR / rest
            if rest and target.is_file() and target.parent == STATIC_DIR:
                return FileResponse(str(target))
            return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/admin")

    @app.get("/health", include_in_schema=False)
    def health():
        accounts = pool.all_runtimes()
        return {
            "status": "ok",
            "version": __version__,
            "accounts": len(accounts),
            "active_accounts": sum(1 for rt in accounts if rt.row["status"] == "active"),
            "open_mode": db.count_active_keys() == 0,
        }

    _start_maintenance(db, pool)
    logger.info("GLM Studio 就绪 版本=%s 账号数=%s", __version__, len(pool.all_runtimes()))
    return app
