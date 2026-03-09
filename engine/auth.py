"""Authentication middleware for Task Ninja."""

from fastapi import Request, WebSocket, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from engine.env_manager import get_env, verify_token

# Paths that don't require auth
PUBLIC_PATHS = {"/", "/login", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/", "/assets/", "/js/")


def _is_remote_access_enabled() -> bool:
    return get_env("TASK_NINJA_REMOTE_ACCESS", "false").lower() == "true"


def _check_token(token: str) -> bool:
    """Validate bearer token against the stored hash."""
    return verify_token(token)


class AuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer token when remote access is enabled."""

    async def dispatch(self, request: Request, call_next):
        # Skip auth if remote access is disabled
        if not _is_remote_access_enabled():
            return await call_next(request)

        path = request.url.path

        # Allow public paths
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # Allow the login/auth check endpoint
        if path == "/api/auth/login" or path == "/api/auth/status":
            return await call_next(request)

        # Check Authorization header
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if _check_token(token):
                return await call_next(request)

        # Check query param (for SSE/EventSource which can't set headers)
        token_param = request.query_params.get("token", "")
        if token_param and _check_token(token_param):
            return await call_next(request)

        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Authentication required"},
        )


def verify_ws_token(websocket: WebSocket) -> bool:
    """Verify WebSocket connection token. Returns True if auth passes."""
    if not _is_remote_access_enabled():
        return True

    # Check query param
    token = websocket.query_params.get("token", "")
    if token and _check_token(token):
        return True

    # Check header (some WS clients support this)
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return _check_token(auth_header[7:])

    return False
