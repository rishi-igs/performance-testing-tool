"""FastAPI application: a management layer around Apache JMeter."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .db import Database
from .routers import auth, debug, infra, projects, reports, scenarios, schedules, scripts, tests, users
from .services.auth import LoginThrottle
from .services.scheduler import Scheduler
from .services.test_executor import TestExecutor

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.db_path)
        interrupted = db.mark_interrupted() + db.mark_debug_runs_interrupted()
        if interrupted:
            logging.getLogger("perf").warning("marked %d interrupted test(s) as failed", interrupted)
        app.state.settings = settings
        app.state.db = db
        app.state.executor = TestExecutor(settings, db)
        app.state.login_throttle = LoginThrottle()
        app.state.autocorrelations = {}         # script id -> running "Correlate automatically" job
        app.state.scheduler = Scheduler(db, lambda scenario, who: scenarios.start_scenario_run(app.state, scenario, who),
                                        settings.scheduler_interval_seconds)
        app.state.scheduler.start()
        yield
        app.state.scheduler.stop()
        app.state.executor.shutdown()

    app = FastAPI(title="Performance Testing Tool", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok", "auth_required": bool(settings.api_key) or app.state.db.count_users() > 0}

    for module in (auth, users, projects, tests, reports, scripts, debug, scenarios, schedules, infra):
        app.include_router(module.router)
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")
    return app


app = create_app()
