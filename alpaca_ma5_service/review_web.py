from __future__ import annotations

import argparse
import dataclasses
import errno
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SERVICE_NAME = "ma5_daily_review"
SCHEMA_VERSION = "1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT_START = 8788
DEFAULT_PORT_END = 8807
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_STATE_NAME = "review_dashboard_server.json"
MAX_REQUEST_TARGET_LENGTH = 4096
MAX_EVIDENCE_LINE = 10_000_000
MAX_QUERY_FIELDS = 8

_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_CHART_FILE_RE = re.compile(r"^watch_code_daily_kline_(?:latest|\d{4}-\d{2}-\d{2})\.html$")


def _review_data_api():
    """Import lazily so health/static pages remain available during data-source failures."""
    from . import review_data

    return review_data


def _dashboard_actions_api():
    from . import dashboard_actions

    return dashboard_actions


class ReviewHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, *, base_dir: Path):
        self.base_dir = base_dir.resolve()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        super().__init__(server_address, handler_class)

    def handle_error(self, request, client_address) -> None:
        _error_type, error, _traceback = sys.exc_info()
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def create_review_server(
    *,
    base_dir: Path | str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT_START,
    review_api=None,
    action_api=None,
) -> ReviewHTTPServer:
    """Create one bound server. Tests may use port=0 for an ephemeral port."""
    root = Path(base_dir or PROJECT_ROOT).resolve()
    handler = make_review_handler(root, review_api=review_api, action_api=action_api)
    return ReviewHTTPServer((host, int(port)), handler, base_dir=root)


def bind_review_server(
    *,
    base_dir: Path | str | None = None,
    host: str = DEFAULT_HOST,
    port_start: int = DEFAULT_PORT_START,
    port_end: int = DEFAULT_PORT_END,
    review_api=None,
    action_api=None,
) -> ReviewHTTPServer:
    """Bind the first available port in the configured, bounded range."""
    _validate_port_range(port_start, port_end)
    last_error: OSError | None = None
    for port in range(port_start, port_end + 1):
        try:
            return create_review_server(base_dir=base_dir, host=host, port=port, review_api=review_api, action_api=action_api)
        except OSError as exc:
            if not _is_address_in_use(exc):
                raise
            last_error = exc
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"No available review dashboard port in {port_start}-{port_end}{detail}")


def make_review_handler(base_dir: Path, *, review_api=None, action_api=None):
    root = base_dir.resolve()
    web_root = (root / "web" / "review_dashboard").resolve()
    chart_root = (root / "outputs" / "watchlist_charts").resolve()

    class ReviewRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MA5Review/1.0"
        sys_version = ""

        def version_string(self) -> str:
            return self.server_version

        def do_GET(self) -> None:
            self._dispatch(send_body=True)

        def do_HEAD(self) -> None:
            self._dispatch(send_body=False)

        def do_POST(self) -> None:
            self._dispatch_post()

        def do_PUT(self) -> None:
            self._method_not_allowed()

        def do_PATCH(self) -> None:
            self._method_not_allowed()

        def do_DELETE(self) -> None:
            self._method_not_allowed()

        def do_OPTIONS(self) -> None:
            self._method_not_allowed()

        def _dispatch(self, *, send_body: bool) -> None:
            parsed = self._validated_request_target(send_body=send_body)
            if parsed is None:
                return

            path = parsed.path or "/"
            try:
                if path == "/api/review/health":
                    self._require_query_keys(parsed.query, set())
                    self._serve_health(send_body=send_body)
                    return
                if path == "/api/review/dates":
                    self._require_query_keys(parsed.query, set())
                    self._serve_dates(send_body=send_body)
                    return
                if path == "/api/review":
                    self._serve_review(parsed.query, send_body=send_body)
                    return
                if path == "/api/review/evidence":
                    self._serve_evidence(parsed.query, send_body=send_body)
                    return
                if path == "/api/runtime/tasks":
                    self._require_query_keys(parsed.query, set())
                    self._serve_runtime_tasks(send_body=send_body)
                    return
                if path == "/api/actions/status":
                    self._require_query_keys(parsed.query, set())
                    self._serve_action_status(send_body=send_body)
                    return
                if path.startswith("/api/"):
                    self._json_error(HTTPStatus.NOT_FOUND, "API endpoint not found", send_body=send_body)
                    return
                if path.startswith("/charts/"):
                    self._serve_chart(path.removeprefix("/charts/"), send_body=send_body)
                    return
                self._serve_static(path, send_body=send_body)
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc) or "invalid request", send_body=send_body)
            except FileNotFoundError:
                self._json_error(HTTPStatus.NOT_FOUND, "requested data was not found", send_body=send_body)
            except PermissionError:
                self._json_error(HTTPStatus.FORBIDDEN, "requested data is not accessible", send_body=send_body)
            except Exception as exc:
                print(f"Review dashboard request failed: {type(exc).__name__}: {exc}", flush=True)
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "review data is temporarily unavailable", send_body=send_body)

        def _dispatch_post(self) -> None:
            parsed = self._validated_request_target(send_body=True)
            if parsed is None:
                return
            path = parsed.path or "/"
            actions = {
                "/api/actions/generate-watchcode": "generate-watchcode",
                "/api/actions/start-monitor": "start-monitor",
                "/api/actions/generate-premarket-watchcode": "generate-premarket-watchcode",
                "/api/actions/start-premarket-monitor": "start-premarket-monitor",
                "/api/actions/stop-monitor": "stop-monitor",
            }
            action = actions.get(path)
            if action is None:
                self._method_not_allowed()
                return
            try:
                self._require_query_keys(parsed.query, set())
                self._require_local_action_request()
                api = action_api if action_api is not None else _dashboard_actions_api()
                payload = api.launch_action(action, base_dir=root)
                response_status = HTTPStatus.ACCEPTED if payload.get("status") == "started" else HTTPStatus.OK
                self._send_json(response_status, payload, send_body=True)
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc) or "invalid action request", send_body=True)
            except PermissionError as exc:
                self._json_error(HTTPStatus.FORBIDDEN, str(exc) or "action request is not allowed", send_body=True)
            except Exception as exc:
                print(f"Review dashboard action failed: {type(exc).__name__}: {exc}", flush=True)
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "dashboard action could not be started", send_body=True)

        def _validated_request_target(self, *, send_body: bool):
            if len(self.path) > MAX_REQUEST_TARGET_LENGTH:
                self._json_error(HTTPStatus.REQUEST_URI_TOO_LONG, "request target is too long", send_body=send_body)
                return None
            host_headers = self.headers.get_all("Host", [])
            if len(host_headers) > 1 or not _host_header_allowed(
                host_headers[0] if host_headers else None,
                server_host=str(self.server.server_address[0]),
                server_port=int(self.server.server_address[1]),
                require_host=self.request_version == "HTTP/1.1",
            ):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid Host header", send_body=send_body)
                return None
            try:
                parsed = urllib.parse.urlsplit(self.path)
            except ValueError:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid request target", send_body=send_body)
                return None
            if parsed.scheme or parsed.netloc or parsed.fragment:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid request target", send_body=send_body)
                return None
            return parsed

        def _require_local_action_request(self) -> None:
            try:
                if not ipaddress.ip_address(str(self.client_address[0])).is_loopback:
                    raise PermissionError("actions are only available from this computer")
            except ValueError as exc:
                raise PermissionError("actions are only available from this computer") from exc
            if self.headers.get("X-MA5-Action") != "1":
                raise PermissionError("missing dashboard action confirmation header")
            fetch_site = self.headers.get("Sec-Fetch-Site")
            if fetch_site and fetch_site != "same-origin":
                raise PermissionError("cross-origin action request is not allowed")
            raw_length = self.headers.get("Content-Length", "0")
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if content_length != 0:
                raise ValueError("action request body must be empty")

        def _serve_health(self, *, send_body: bool) -> None:
            host, port = self.server.server_address[:2]
            payload = {
                "ok": True,
                "service": SERVICE_NAME,
                "schema_version": SCHEMA_VERSION,
                "project_root": str(root),
                "web_root": str(web_root),
                "chart_root": str(chart_root),
                "host": str(host),
                "port": int(port),
                "pid": os.getpid(),
                "started_at": getattr(self.server, "started_at", None),
            }
            self._send_json(HTTPStatus.OK, payload, send_body=send_body)

        def _serve_dates(self, *, send_body: bool) -> None:
            api = review_api if review_api is not None else _review_data_api()
            dates = api.list_review_dates(base_dir=root)
            self._send_json(HTTPStatus.OK, {"dates": dates}, send_body=send_body)

        def _serve_runtime_tasks(self, *, send_body: bool) -> None:
            from .monitor_runtime import read_monitor_tasks

            self._send_json(HTTPStatus.OK, read_monitor_tasks(root), send_body=send_body)

        def _serve_action_status(self, *, send_body: bool) -> None:
            api = action_api if action_api is not None else _dashboard_actions_api()
            self._send_json(HTTPStatus.OK, api.action_status(root), send_body=send_body)

        def _serve_review(self, query: str, *, send_body: bool) -> None:
            params = self._require_query_keys(query, {"date", "broker"})
            requested_date = self._optional_single(params, "date")
            if requested_date is not None:
                requested_date = _validated_iso_date(requested_date, field_name="date")
            broker_value = self._optional_single(params, "broker") or "0"
            if broker_value not in {"0", "1"}:
                raise ValueError("broker must be 0 or 1")

            api = review_api if review_api is not None else _review_data_api()
            payload = api.build_daily_review(
                requested_date,
                include_broker=broker_value == "1",
                base_dir=root,
            )
            self._send_json(HTTPStatus.OK, payload, send_body=send_body)

        def _serve_evidence(self, query: str, *, send_body: bool) -> None:
            params = self._require_query_keys(query, {"date", "source", "line"})
            review_date = _validated_iso_date(self._required_single(params, "date"), field_name="date")
            source_id = self._required_single(params, "source")
            if not _SOURCE_ID_RE.fullmatch(source_id) or source_id.casefold() == ".env":
                raise ValueError("source must be a valid evidence source id")
            raw_line = self._required_single(params, "line")
            try:
                line = int(raw_line)
            except ValueError as exc:
                raise ValueError("line must be an integer") from exc
            if line < 1 or line > MAX_EVIDENCE_LINE:
                raise ValueError(f"line must be between 1 and {MAX_EVIDENCE_LINE}")

            api = review_api if review_api is not None else _review_data_api()
            payload = api.evidence_context(
                review_date,
                source_id,
                line,
                radius=3,
                base_dir=root,
            )
            self._send_json(HTTPStatus.OK, payload, send_body=send_body)

        def _serve_chart(self, raw_name: str, *, send_body: bool) -> None:
            try:
                name = urllib.parse.unquote(raw_name, errors="strict")
            except (UnicodeDecodeError, ValueError):
                self._json_error(HTTPStatus.NOT_FOUND, "chart not found", send_body=send_body)
                return
            if not _CHART_FILE_RE.fullmatch(name) or Path(name).name != name:
                self._json_error(HTTPStatus.NOT_FOUND, "chart not found", send_body=send_body)
                return
            target = _safe_child_file(chart_root, name)
            if target is None or not target.is_file():
                self._json_error(HTTPStatus.NOT_FOUND, "chart not found", send_body=send_body)
                return
            self._send_file(target, cache_control="no-cache", csp_kind="chart", send_body=send_body)

        def _serve_static(self, raw_path: str, *, send_body: bool) -> None:
            try:
                decoded = urllib.parse.unquote(raw_path, errors="strict")
            except (UnicodeDecodeError, ValueError):
                self._json_error(HTTPStatus.NOT_FOUND, "page not found", send_body=send_body)
                return
            relative = "index.html" if decoded == "/" else decoded.lstrip("/")
            target = _safe_child_file(web_root, relative)
            if target is None or not target.is_file():
                self._json_error(HTTPStatus.NOT_FOUND, "page not found", send_body=send_body)
                return
            self._send_file(target, cache_control="no-cache", csp_kind="app", send_body=send_body)

        def _send_file(self, path: Path, *, cache_control: str, csp_kind: str, send_body: bool) -> None:
            try:
                body = path.read_bytes()
                modified_at = path.stat().st_mtime
            except FileNotFoundError:
                self._json_error(HTTPStatus.NOT_FOUND, "file not found", send_body=send_body)
                return
            self._send_bytes(
                HTTPStatus.OK,
                body,
                content_type=_content_type(path),
                cache_control=cache_control,
                csp_kind=csp_kind,
                send_body=send_body,
                last_modified=modified_at,
            )

        def _send_json(self, status: HTTPStatus, payload: Any, *, send_body: bool, extra_headers=None) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=_json_default,
            ).encode("utf-8")
            self._send_bytes(
                status,
                body,
                content_type="application/json; charset=utf-8",
                cache_control="no-store, max-age=0",
                csp_kind="api",
                send_body=send_body,
                extra_headers=extra_headers,
            )

        def _json_error(self, status: HTTPStatus, message: str, *, send_body: bool) -> None:
            self._send_json(status, {"ok": False, "error": message}, send_body=send_body)

        def _send_bytes(
            self,
            status: HTTPStatus,
            body: bytes,
            *,
            content_type: str,
            cache_control: str,
            csp_kind: str,
            send_body: bool,
            last_modified: float | None = None,
            extra_headers=None,
        ) -> None:
            etag = _body_etag(body)
            response_status = status
            if status == HTTPStatus.OK and _etag_matches(self.headers.get("If-None-Match", ""), etag):
                response_status = HTTPStatus.NOT_MODIFIED

            self.send_response(response_status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            self.send_header("ETag", etag)
            if last_modified is not None:
                self.send_header("Last-Modified", _http_date(last_modified))
            self._send_security_headers(csp_kind)
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(str(key), str(value))
            # For HEAD and 304, Content-Length describes the selected GET
            # representation even though no response body is transmitted.
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body and response_status != HTTPStatus.NOT_MODIFIED and body:
                self.wfile.write(body)

        def _send_security_headers(self, csp_kind: str) -> None:
            if csp_kind == "api":
                csp = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            elif csp_kind == "chart":
                csp = (
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                )
            else:
                csp = (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
                )
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")

        def _method_not_allowed(self) -> None:
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"ok": False, "error": "method not allowed"},
                send_body=True,
                extra_headers={"Allow": "GET, HEAD, POST"},
            )

        @staticmethod
        def _require_query_keys(query: str, allowed: set[str]) -> dict[str, list[str]]:
            params = urllib.parse.parse_qs(
                query,
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=MAX_QUERY_FIELDS,
            )
            unknown = sorted(set(params) - allowed)
            if unknown:
                raise ValueError(f"unknown query parameter: {unknown[0]}")
            for key, values in params.items():
                if len(values) != 1:
                    raise ValueError(f"query parameter {key} must appear once")
            return params

        @staticmethod
        def _optional_single(params: dict[str, list[str]], key: str) -> str | None:
            values = params.get(key)
            if not values or values[0] == "":
                return None
            return values[0]

        @staticmethod
        def _required_single(params: dict[str, list[str]], key: str) -> str:
            values = params.get(key)
            if not values or not values[0]:
                raise ValueError(f"query parameter {key} is required")
            return values[0]

    return ReviewRequestHandler


def review_server_ready(
    port: int,
    *,
    base_dir: Path | str | None = None,
    host: str = DEFAULT_HOST,
    timeout: float = 0.5,
) -> bool:
    payload = fetch_review_health(port, host=host, timeout=timeout)
    if not payload or payload.get("service") != SERVICE_NAME or not payload.get("ok"):
        return False
    actual_root = payload.get("project_root")
    if not actual_root:
        return False
    try:
        return Path(str(actual_root)).resolve() == Path(base_dir or PROJECT_ROOT).resolve()
    except OSError:
        return False


def fetch_review_health(port: int, *, host: str = DEFAULT_HOST, timeout: float = 0.5) -> dict[str, Any] | None:
    probe_host = _url_host(host)
    request = urllib.request.Request(
        f"http://{probe_host}:{int(port)}/api/review/health",
        headers={"Accept": "application/json", "Connection": "close"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != HTTPStatus.OK:
                return None
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                return None
            payload = json.loads(raw.decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError):
        return None


def find_running_review_server(
    *,
    base_dir: Path | str | None = None,
    host: str = DEFAULT_HOST,
    port_start: int = DEFAULT_PORT_START,
    port_end: int = DEFAULT_PORT_END,
) -> int | None:
    _validate_port_range(port_start, port_end)
    for port in range(port_start, port_end + 1):
        # Windows 对关闭端口的 urllib 探测有时会耗完整超时；先用极短 TCP
        # 探测跳过未监听端口，让第一次双击启动保持接近即时。
        if not _tcp_port_open(host, port, timeout=0.06):
            continue
        if review_server_ready(port, base_dir=base_dir, host=host, timeout=0.2):
            return port
    return None


def wait_for_review_server(
    *,
    base_dir: Path | str | None = None,
    host: str = DEFAULT_HOST,
    port_start: int = DEFAULT_PORT_START,
    port_end: int = DEFAULT_PORT_END,
    timeout_seconds: float = 10.0,
) -> int | None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() <= deadline:
        port = find_running_review_server(
            base_dir=base_dir,
            host=host,
            port_start=port_start,
            port_end=port_end,
        )
        if port is not None:
            return port
        time.sleep(0.15)
    return None


def review_dashboard_url(port: int, *, host: str = DEFAULT_HOST) -> str:
    return f"http://{_url_host(host)}:{int(port)}/"


def server_state_path(base_dir: Path | str | None = None) -> Path:
    return Path(base_dir or PROJECT_ROOT).resolve() / "outputs" / SERVER_STATE_NAME


def write_review_server_state(base_dir: Path | str | None, payload: dict[str, Any]) -> Path:
    path = server_state_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {
        **payload,
        "service": SERVICE_NAME,
        "project_root": str(Path(base_dir or PROJECT_ROOT).resolve()),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def serve_review_dashboard(
    *,
    base_dir: Path | str | None = None,
    host: str = DEFAULT_HOST,
    port_start: int = DEFAULT_PORT_START,
    port_end: int = DEFAULT_PORT_END,
) -> None:
    root = Path(base_dir or PROJECT_ROOT).resolve()
    server = bind_review_server(base_dir=root, host=host, port_start=port_start, port_end=port_end)
    actual_port = int(server.server_address[1])
    url = review_dashboard_url(actual_port, host=host)
    write_review_server_state(
        root,
        {
            "host": host,
            "port": actual_port,
            "url": url,
            "pid": os.getpid(),
            "started_at": server.started_at,
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    print(f"MA5 daily review service: {url}", flush=True)
    print(f"Project root: {root}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def _safe_child_file(root: Path, relative: str) -> Path | None:
    if not relative or "\x00" in relative or "\\" in relative:
        return None
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        return None
    if any(":" in part for part in parts):
        return None
    try:
        target = (root / relative).resolve()
    except OSError:
        return None
    if target == root or root not in target.parents:
        return None
    return target


def _validated_iso_date(value: str, *, field_name: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field_name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid date") from exc


def _content_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    explicit = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }
    if suffix in explicit:
        return explicit[suffix]
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _json_default(value: Any):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _body_etag(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()}"'


def _etag_matches(header_value: str, etag: str) -> bool:
    if not header_value:
        return False
    for raw_candidate in header_value.split(","):
        candidate = raw_candidate.strip()
        if candidate == "*":
            return True
        # If-None-Match uses weak comparison for GET/HEAD, so W/"..." must
        # match the same opaque tag emitted by this server.
        if candidate[:2].casefold() == "w/":
            candidate = candidate[2:].lstrip()
        if candidate == etag:
            return True
    return False


def _http_date(timestamp: float) -> str:
    from email.utils import formatdate

    return formatdate(timestamp, usegmt=True)


def _probe_host(host: str) -> str:
    return "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host


def _tcp_port_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((_probe_host(host), int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _url_host(host: str) -> str:
    value = _probe_host(host)
    return f"[{value}]" if ":" in value and not value.startswith("[") else value


def _host_header_allowed(
    header_value: str | None,
    *,
    server_host: str,
    server_port: int,
    require_host: bool,
) -> bool:
    """Allow local/IP origins while rejecting DNS-rebinding hostnames."""
    if not header_value:
        return not require_host
    value = header_value.strip()
    if not value or value != header_value or any(char in value for char in "\r\n\t /\\@?#,"):
        return False

    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return False
        name = value[1:closing]
        suffix = value[closing + 1 :]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:].isdigit():
                return False
            port_text = suffix[1:]
        else:
            port_text = ""
    else:
        if value.count(":") > 1:
            return False
        name, separator, port_text = value.rpartition(":")
        if not separator:
            name, port_text = value, ""
        elif not name or not port_text.isdigit():
            return False

    if port_text and (not 1 <= int(port_text) <= 65535 or int(port_text) != int(server_port)):
        return False
    normalized_name = name.rstrip(".").casefold()
    if not normalized_name:
        return False

    try:
        requested_ip = ipaddress.ip_address(normalized_name)
    except ValueError:
        requested_ip = None
    try:
        bound_ip = ipaddress.ip_address(server_host)
    except ValueError:
        bound_ip = None

    if requested_ip is not None:
        if bound_ip is None or bound_ip.is_unspecified:
            return True
        if bound_ip.is_loopback:
            return requested_ip.is_loopback
        return requested_ip == bound_ip

    if normalized_name == "localhost" or normalized_name.endswith(".localhost"):
        return bound_ip is None or bound_ip.is_loopback or bound_ip.is_unspecified
    return bound_ip is None and normalized_name == server_host.rstrip(".").casefold()


def _validate_port_range(port_start: int, port_end: int) -> None:
    if not (1 <= int(port_start) <= int(port_end) <= 65535):
        raise ValueError("invalid review dashboard port range")


def _is_address_in_use(exc: OSError) -> bool:
    return exc.errno in {errno.EADDRINUSE, errno.EACCES} or getattr(exc, "winerror", None) in {10013, 10048}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the local MA5 daily review dashboard")
    parser.add_argument("--base-dir", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port-start", type=int, default=DEFAULT_PORT_START)
    parser.add_argument("--port-end", type=int, default=DEFAULT_PORT_END)
    args = parser.parse_args(argv)
    serve_review_dashboard(
        base_dir=args.base_dir,
        host=args.host,
        port_start=args.port_start,
        port_end=args.port_end,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
