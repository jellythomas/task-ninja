#!/usr/bin/env python3
"""Task Ninja — FastAPI server + orchestrator."""

from __future__ import annotations

import subprocess
import sys

from pathlib import Path


def _check_python_version() -> None:
    """Ensure Python version is compatible (3.10+)."""
    if sys.version_info < (3, 10):  # noqa: UP036 — runtime guard, pyproject.toml not active yet
        print(f"[server] Python 3.10+ required (found {sys.version})", file=sys.stderr)
        if sys.platform == "win32":
            print("[server] Download from: https://www.python.org/downloads/", file=sys.stderr)
        elif sys.platform == "darwin":
            print("[server] Install with: brew install python@3.12", file=sys.stderr)
        else:
            print("[server] Install with: sudo apt install python3.12", file=sys.stderr)
        sys.exit(1)


def _check_git() -> None:
    """Check if git is available, suggest install if missing."""
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            return
    except FileNotFoundError:
        pass
    print("[server] WARNING: git not found — required for worktree-based ticket execution", file=sys.stderr)
    if sys.platform == "win32":
        print("[server] Download from: https://git-scm.com/download/win", file=sys.stderr)
    elif sys.platform == "darwin":
        print("[server] Install with: brew install git", file=sys.stderr)
    else:
        print("[server] Install with: sudo apt install git", file=sys.stderr)


def _check_node() -> None:
    """Check if Node.js is available, suggest install if missing."""
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            return
    except FileNotFoundError:
        pass
    print("[server] WARNING: Node.js not found — required for AI CLI agents (Claude Code, etc.)", file=sys.stderr)
    if sys.platform == "win32":
        print("[server] Download from: https://nodejs.org/", file=sys.stderr)
    elif sys.platform == "darwin":
        print("[server] Install with: brew install node", file=sys.stderr)
    else:
        print("[server] Install with: sudo apt install nodejs npm", file=sys.stderr)


def _ensure_config() -> None:
    """Create config.yaml with defaults if missing."""
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        return
    default_config = """# Task Ninja configuration
orchestrator:
  poll_interval: 5

claude:
  idle_timeout: 10

mcp:
  jira_status_mapping:
    planning: "In Progress"
    developing: "In Progress"
    review: "In Review"
    done: "Done"

git:
  worktree_dir: ".worktrees"
  branch_prefix: "feat"

database:
  path: "task_ninja.db"
"""
    config_path.write_text(default_config)
    print("[server] Created config.yaml with defaults", file=sys.stderr)


def _check_dependencies() -> None:
    """Auto-install missing dependencies from requirements.txt on first run."""
    req_file = Path(__file__).parent / "requirements.txt"
    if not req_file.exists():
        return
    import re

    from importlib.metadata import PackageNotFoundError, distribution

    missing = []
    for line in req_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"([a-zA-Z0-9_-]+)", line)
        if not match:
            continue
        pkg = match.group(1)
        try:
            distribution(pkg)
        except PackageNotFoundError:
            missing.append(line)
    if missing:
        print(f"[server] Missing dependencies: {', '.join(missing)}", file=sys.stderr)
        print("[server] Installing from requirements.txt...", file=sys.stderr)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file), "-q"],
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError:
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "-r", str(req_file), "-q", "--break-system-packages"],
                )
            except subprocess.CalledProcessError:
                print("[server] Auto-install failed. Please run manually:", file=sys.stderr)
                print(f"  pip3 install -r {req_file}", file=sys.stderr)
                return
        print("[server] Dependencies installed.", file=sys.stderr)


_check_python_version()
_check_dependencies()
_check_git()
_check_node()
_ensure_config()

import asyncio
import logging

from contextlib import asynccontextmanager

import uvicorn

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from api.error_handlers import register_error_handlers
from api.routers import analytics, health, profiles, runs, settings, terminals, tickets
from config import AppConfig
from engine.auth import AuthMiddleware
from engine.broadcaster import Broadcaster
from engine.claude_helper import ClaudeHelper
from engine.env_manager import generate_token, get_env, load_env, verify_token
from engine.jira_client import JiraClient
from engine.notifier import Notifier
from engine.orchestrator import Orchestrator
from engine.scheduler import RunScheduler
from engine.state import StateManager, init_db
from engine.terminal import TerminalManager
from models.ticket import RunStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server")

# Load .env first (creates file with defaults if missing)
env_config = load_env()

# Load typed config
_PROJECT_ROOT = Path(__file__).parent
app_config = AppConfig.load(_PROJECT_ROOT / "config.yaml")

# Resolve database path to absolute so workers in worktree dirs can find it
DB_PATH = app_config.resolve_db_path(_PROJECT_ROOT)


def create_app() -> FastAPI:
    """Application factory — wires up lifespan, middleware, routers, error handlers."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # --- Startup ---
        init_db(DB_PATH)
        logger.info("Database initialized at %s", DB_PATH)

        # Build shared singletons and attach to app.state for DI
        _state = StateManager(DB_PATH)
        _broadcaster = Broadcaster()
        _orchestrator = Orchestrator(_state, _broadcaster, app_config.raw)
        _claude_helper = ClaudeHelper("claude")
        _jira_client = JiraClient()
        _terminal_manager = TerminalManager()
        _notifier = Notifier(_state)
        _orchestrator.notifier = _notifier
        _run_scheduler = RunScheduler(_state, _orchestrator.start)

        app.state.state = _state
        app.state.broadcaster = _broadcaster
        app.state.orchestrator = _orchestrator
        app.state.claude_helper = _claude_helper
        app.state.jira_client = _jira_client
        app.state.terminal_manager = _terminal_manager
        app.state.notifier = _notifier
        app.state.run_scheduler = _run_scheduler
        app.state.config = app_config.raw

        # Restore orchestrator state from DB on startup
        _runs = await _state.list_runs()
        for run in _runs:
            if run.status == RunStatus.RUNNING:
                logger.info("Restoring running state for run %s", run.id)
                await _orchestrator.start(run.id)
                break
            elif run.status in (RunStatus.PAUSED, RunStatus.COMPLETED):
                _orchestrator._run_id = run.id
                logger.info("Restored run_id %s (status: %s)", run.id, run.status)
                break

        _run_scheduler.start()
        await _run_scheduler.load_existing_schedules()

        yield

        # --- Shutdown ---
        _run_scheduler.stop()
        _orchestrator.watchdog.cancel_all()
        _terminal_manager.close_all()

    _app = FastAPI(title="Task Ninja", lifespan=lifespan)
    _app.add_middleware(AuthMiddleware)

    register_error_handlers(_app)

    # Routers
    _app.include_router(health.router)
    _app.include_router(runs.router)
    _app.include_router(tickets.router)
    _app.include_router(terminals.router)
    _app.include_router(profiles.router)
    _app.include_router(settings.router)
    _app.include_router(analytics.router)

    # --- Auth ---

    @_app.post("/api/auth/login")
    async def auth_login(req: dict):
        """Validate token and return success."""
        token = req.get("token", "")
        if verify_token(token):
            return {"status": "ok"}
        raise HTTPException(401, "Invalid token")

    @_app.get("/api/auth/status")
    async def auth_status():
        """Check if auth is required."""
        remote = get_env("TASK_NINJA_REMOTE_ACCESS", "false").lower() == "true"
        return {"auth_required": remote}

    # --- Tailscale ---

    @_app.get("/api/tailscale/status")
    async def tailscale_status():
        """Check Tailscale installation and connection status."""
        import json as _json
        import shutil

        result = {"installed": False, "running": False, "ip": None, "url": None}
        if not shutil.which("tailscale"):
            return result
        result["installed"] = True
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale",
                "status",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                ts = _json.loads(stdout)
                if ts.get("Self", {}).get("Online"):
                    result["running"] = True
                    addrs = ts["Self"].get("TailscaleIPs", [])
                    ipv4 = next((a for a in addrs if "." in a), None)
                    if ipv4:
                        port = int(get_env("TASK_NINJA_PORT") or "8420")
                        result["ip"] = ipv4
                        result["url"] = f"http://{ipv4}:{port}"
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        return result

    @_app.post("/api/tailscale/install")
    async def tailscale_install():
        """Install Tailscale via package manager."""
        import platform
        import shutil

        if shutil.which("tailscale"):
            return {"status": "already_installed"}
        system = platform.system().lower()
        if system == "darwin":
            cmd = ["brew", "install", "--cask", "tailscale"]
        elif system == "linux":
            cmd = ["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"]
        else:
            raise HTTPException(
                400, f"Auto-install not supported on {system}. Install manually from https://tailscale.com/download"
            )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            raise HTTPException(500, f"Install failed: {stderr.decode()[-500:]}")
        return {
            "status": "installed",
            "message": "Tailscale installed. Open the Tailscale app to log in, then check status again.",
        }

    @_app.post("/api/tailscale/up")
    async def tailscale_up():
        """Start Tailscale (tailscale up)."""
        import shutil

        if not shutil.which("tailscale"):
            raise HTTPException(400, "Tailscale is not installed")
        proc = await asyncio.create_subprocess_exec(
            "tailscale",
            "up",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            err = stderr.decode()
            if "login" in err.lower() or "auth" in err.lower():
                return {"status": "needs_login", "message": "Open the Tailscale app to log in first."}
            raise HTTPException(500, f"Failed to start: {err[-500:]}")
        return {"status": "started"}

    # --- Static UI ---

    @_app.get("/")
    async def serve_ui():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    # --- SSE Stream ---

    from sse_starlette.sse import EventSourceResponse

    @_app.get("/api/stream/{run_id}")
    async def stream(run_id: str):
        """SSE endpoint for real-time board updates."""
        _broadcaster: Broadcaster = _app.state.broadcaster
        queue = _broadcaster.subscribe(run_id)

        async def event_generator():
            try:
                while True:
                    message = await queue.get()
                    yield {"data": message}
            except asyncio.CancelledError:
                _broadcaster.unsubscribe(run_id, queue)

        return EventSourceResponse(event_generator())

    return _app


# Module-level app instance for uvicorn
app = create_app()


# --- Entry point ---
if __name__ == "__main__":
    if "--regenerate-token" in sys.argv:
        raw_token = generate_token()
        print("")
        print("  New auth token generated:")
        print("")
        print(f"    {raw_token}")
        print("")
        print("  Save this token — it is shown once and never stored on disk.")
        print("  The hash has been written to .env.")
        print("")
        sys.exit(0)

    remote = get_env("TASK_NINJA_REMOTE_ACCESS", "false").lower() == "true"
    host = get_env("TASK_NINJA_HOST", "127.0.0.1")
    if remote:
        host = "0.0.0.0"
    port = int(get_env("TASK_NINJA_PORT", "8420"))
    if remote:
        logger.info("Remote access ENABLED — auth required")
        logger.info("Token is hashed — use your saved token to log in")
    logger.info("Starting at http://%s:%d", host, port)
    uvicorn.run("server:app", host=host, port=port, reload=False)
