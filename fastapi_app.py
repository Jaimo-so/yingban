from __future__ import annotations

from email.message import Message
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from server import AppContext, YingbanHandler


class _FastAPIBridgeHandler(YingbanHandler):
    """Run the proven request dispatcher without its legacy socket server.

    The product behavior remains centralized in ``YingbanHandler`` during the
    transport migration. FastAPI owns the live HTTP lifecycle; this adapter only
    supplies the request/response primitives that the existing dispatcher uses.
    """

    def __init__(
        self,
        app: AppContext,
        method: str,
        raw_path: str,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
        client_host: str,
    ) -> None:
        self.server = type("FastAPIServerContext", (), {"app": app})()
        self.command = method
        self.path = raw_path
        self.request_version = "HTTP/1.1"
        self.requestline = f"{method} {raw_path} HTTP/1.1"
        self.client_address = (client_host, 0)
        self.close_connection = True
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.headers = Message()
        for key, value in headers:
            self.headers.add_header(key.decode("latin-1"), value.decode("latin-1"))
        if self.headers.get("Content-Length") is None:
            self.headers["Content-Length"] = str(len(body))
        self.response_status = int(HTTPStatus.OK)
        self.response_headers: list[tuple[str, str]] = []

    def send_response(self, code: int | HTTPStatus, message: str | None = None) -> None:
        del message
        self.response_status = int(code)

    def send_header(self, keyword: str, value: str) -> None:
        self.response_headers.append((keyword, value))

    def end_headers(self) -> None:
        return

    def dispatch(self) -> None:
        if self.command == "GET":
            self.do_GET()
            return
        if self.command == "POST":
            self.do_POST()
            return
        if self.command == "DELETE":
            self.do_DELETE()
            return
        self._json({"error": "method not allowed"}, HTTPStatus.METHOD_NOT_ALLOWED)


def _safe_file(root: Path, relative: str) -> Path | None:
    candidate = (root / unquote(relative)).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root) or not candidate.is_file():
        return None
    return candidate


def _exported_frontend_file(app: AppContext, path: str) -> Path | None:
    root = app.settings.base_dir / "frontend" / "out"
    if not root.is_dir():
        return None
    page_names = {
        "/": "index.html",
        "/index.html": "index.html",
        "/admin": "admin.html",
        "/admin/": "admin.html",
        "/admin.html": "admin.html",
        "/share": "share.html",
        "/share/": "share.html",
        "/share.html": "share.html",
    }
    if path in page_names:
        direct = _safe_file(root, page_names[path])
        if direct is not None:
            return direct
        nested = page_names[path].removesuffix(".html") + "/index.html"
        return _safe_file(root, nested)
    if path.startswith("/_next/"):
        return _safe_file(root, path.lstrip("/"))
    return None


def _file_response(path: Path) -> Response:
    import mimetypes

    body = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Cache-Control": "no-store" if path.suffix == ".html" else "public, max-age=300",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
    }
    return Response(body, media_type=content_type, headers=headers)


def _dispatch_legacy(
    app: AppContext,
    method: str,
    raw_path: str,
    headers: list[tuple[bytes, bytes]],
    body: bytes,
    client_host: str,
) -> Response:
    handler = _FastAPIBridgeHandler(app, method, raw_path, headers, body, client_host)
    handler.dispatch()
    response = Response(content=handler.wfile.getvalue(), status_code=handler.response_status)
    response.raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in handler.response_headers
    ]
    return response


def create_fastapi_app(context: AppContext) -> FastAPI:
    application = FastAPI(
        title="影伴 API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.context = context

    @application.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )
    async def dispatch(request: Request, full_path: str) -> Response:
        del full_path
        if request.method == "GET":
            exported = _exported_frontend_file(context, request.url.path)
            if exported is not None:
                return await run_in_threadpool(_file_response, exported)
        body = await request.body()
        query = request.url.query
        raw_path = request.url.path + (f"?{query}" if query else "")
        client_host = request.client.host if request.client else "127.0.0.1"
        return await run_in_threadpool(
            _dispatch_legacy,
            context,
            request.method,
            raw_path,
            list(request.scope.get("headers", [])),
            body,
            client_host,
        )

    return application
