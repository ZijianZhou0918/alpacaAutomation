from __future__ import annotations

import json
import urllib.parse
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from ._bootstrap import ensure_local_venv
except ImportError:
    from _bootstrap import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.errors import short_error
from alpaca_ma5_service.watchlist_charts import (
    CHART_FILE,
    content_type_for_path,
    delete_watch_codes_from_watchlist,
    local_lan_ips,
)
from alpaca_ma5_service.watchlist_generator import refresh_watchlist_chart_from_watch_codes


def serve_watchlist_charts(file_name: str = "watch_codes.txt") -> None:
    """按指定观察池文件刷新并服务图表页面，同时提供 StockAPI 风格删除 API。"""
    settings = settings_for_watch_file(build_settings(), file_name)
    chart_dir = (settings.output_dir / "watchlist_charts").resolve()
    chart_path = chart_dir / CHART_FILE
    refresh_chart_page(settings, require_existing=False)
    if not chart_path.exists():
        raise FileNotFoundError(f"Chart page not found: {chart_path}")

    port = int(settings.watchlist_chart_lan_port)

    class WatchlistChartHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def send_json_response(self, status: int, payload: dict) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/watchlist/health":
                self.send_json_response(
                    200,
                    {
                        "ok": True,
                        "service": "watchlist_chart",
                        "port": port,
                        "chart_dir": str(chart_dir),
                        "watch_codes_file": str(settings.watch_codes_file.resolve()),
                    },
                )
                return
            if parsed.path == "/api/watchlist/delete":
                self.send_json_response(405, {"ok": False, "error": "delete endpoint requires POST", "codes": []})
                return

            requested = parsed.path.lstrip("/") or CHART_FILE
            target = (chart_dir / requested).resolve()
            if chart_dir not in [target, *target.parents] or not target.exists() or target.is_dir():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type_for_path(target))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(target.read_bytes())

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/api/watchlist/delete":
                self.send_json_response(404, {"ok": False, "error": f"unknown endpoint: {parsed.path}", "codes": []})
                return

            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                self.send_json_response(400, {"ok": False, "error": f"invalid json: {exc}", "codes": []})
                return

            codes = payload.get("codes")
            if codes is None:
                codes = [payload.get("code")]
            result = delete_watch_codes_from_watchlist(settings, codes)
            if result.get("removed"):
                refresh_chart_page(settings, require_existing=True, result=result)
            print(f"Watchlist delete request: codes={result.get('codes')} removed={result.get('removed')} paths={result.get('paths')}", flush=True)
            self.send_json_response(200, result)

    class ReusableThreadingHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True

    server = ReusableThreadingHTTPServer(("0.0.0.0", port), WatchlistChartHandler)
    print(f"Using watch codes file: {settings.watch_codes_file}", flush=True)
    print(f"Serving chart directory: {chart_dir}", flush=True)
    print(f"Local PC URL: http://127.0.0.1:{port}/{CHART_FILE}", flush=True)
    for ip in local_lan_ips():
        print(f"Phone LAN URL: http://{ip}:{port}/{CHART_FILE}", flush=True)
    server.serve_forever()


def refresh_chart_page(settings, *, require_existing: bool, result: dict | None = None) -> None:
    """服务启动和删除后都按指定观察池文件刷新；失败时不影响已有页面继续访问。"""
    try:
        refresh_watchlist_chart_from_watch_codes(settings)
        if result is not None:
            result["chart_refreshed"] = True
    except Exception as exc:
        chart_path = settings.output_dir / "watchlist_charts" / CHART_FILE
        if not require_existing and not chart_path.exists():
            raise
        message = short_error(exc)
        print(f"图表按 {settings.watch_codes_file.name} 刷新失败，继续使用现有页面：{message}", flush=True)
        if result is not None:
            result["chart_refreshed"] = False
            result["chart_refresh_error"] = message


def settings_for_watch_file(settings, file_name: str):
    """Switch watch_codes_file within the canonical WatchCode data directory."""
    name = (file_name or "watch_codes.txt").strip()
    path = Path(name)
    if path.name != name or path.is_absolute():
        raise ValueError(f"file_name 只能是 WatchCode 数据目录下的文件名：{file_name}")
    return replace(settings, watch_codes_file=settings.watch_codes_file.with_name(name))


if __name__ == "__main__":
    serve_watchlist_charts("watch_codes.txt")
    # watch_code_afterhours.txt
