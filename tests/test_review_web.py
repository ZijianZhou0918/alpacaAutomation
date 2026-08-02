from __future__ import annotations

import json
import socket
import subprocess
import threading
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

import open_daily_review
from alpaca_ma5_service.review_web import (
    bind_review_server,
    create_review_server,
    fetch_review_health,
    find_running_review_server,
    review_dashboard_url,
    review_server_ready,
    start_review_idle_monitor,
)


_AUTO_HOST = object()


class StubReviewAPI:
    """Contract-shaped stand-in; deliberately falsey to test explicit injection."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __bool__(self) -> bool:
        return False

    def list_review_dates(self, *, base_dir: Path) -> list[str]:
        self.calls.append(("dates", base_dir))
        return ["2026-07-11", "2026-07-10"]

    def build_daily_review(
        self,
        requested_date=None,
        *,
        include_broker: bool = False,
        base_dir: Path | None = None,
    ) -> dict:
        self.calls.append(("review", requested_date, include_broker, base_dir))
        if requested_date == "2026-07-01":
            raise FileNotFoundError(requested_date)
        if requested_date == "2026-07-02":
            raise PermissionError(requested_date)
        if requested_date == "2026-07-03":
            raise RuntimeError("secret backend detail")
        return {
            "schema_version": "1.0",
            "requested_date": requested_date,
            "review_date": requested_date or "2026-07-11",
            "broker": {"requested": include_broker},
        }

    def evidence_context(
        self,
        review_date: str,
        source_id: str,
        line: int,
        *,
        radius: int = 3,
        base_dir: Path | None = None,
    ) -> dict:
        self.calls.append(("evidence", review_date, source_id, line, radius, base_dir))
        return {"date": review_date, "source": source_id, "line": line, "radius": radius}


class StubActionAPI:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def action_status(self, base_dir: Path) -> dict:
        self.calls.append(("status", base_dir))
        return {"ok": True, "watchcode": {"ready": False}, "monitor_running": False, "generator_running": False, "pending_actions": []}

    def launch_action(self, action: str, *, base_dir: Path) -> dict:
        self.calls.append(("launch", action, base_dir))
        status = "stopped" if action == "stop-monitor" else "started"
        return {"ok": True, "status": status, "action": action, "message": status}


class ReviewWebHTTPTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        web_root = self.root / "web" / "review_dashboard"
        chart_root = self.root / "outputs" / "watchlist_charts"
        web_root.mkdir(parents=True)
        chart_root.mkdir(parents=True)
        (web_root / "index.html").write_text("<!doctype html><title>复盘</title>", encoding="utf-8")
        (web_root / "app.js").write_bytes(b"window.reviewReady = true;\n")
        (chart_root / "watch_code_daily_kline_latest.html").write_text(
            "<!doctype html><style>body{color:black}</style><script>window.chartReady=true</script>",
            encoding="utf-8",
        )
        (chart_root / "private.html").write_text("private", encoding="utf-8")
        (self.root / ".env").write_text("ALPACA_SECRET=never-serve-this", encoding="utf-8")

        self.api = StubReviewAPI()
        self.action_api = StubActionAPI()
        self.server = create_review_server(
            base_dir=self.root,
            host="127.0.0.1",
            port=0,
            review_api=self.api,
            action_api=self.action_api,
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)
        self.temporary.cleanup()

    def test_idle_monitor_closes_listener_after_requests_finish(self):
        idle_server = create_review_server(
            base_dir=self.root,
            host="127.0.0.1",
            port=0,
            review_api=self.api,
            action_api=self.action_api,
            idle_timeout_seconds=0.2,
        )
        idle_thread = threading.Thread(target=idle_server.serve_forever, daemon=True)
        idle_thread.start()
        stop_event = start_review_idle_monitor(idle_server)
        try:
            idle_thread.join(timeout=2.0)
            self.assertFalse(idle_thread.is_alive())
        finally:
            stop_event.set()
            idle_server.shutdown()
            idle_server.server_close()

    def request(self, method: str, target: str, *, headers=None, host=_AUTO_HOST):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3.0)
        connection.putrequest(method, target, skip_host=host is not _AUTO_HOST)
        if host is not _AUTO_HOST and host is not None:
            connection.putheader("Host", str(host))
        for key, value in (headers or {}).items():
            connection.putheader(key, value)
        connection.putheader("Connection", "close")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        result = response.status, response.headers, body
        connection.close()
        return result

    def request_json(self, target: str):
        status, headers, body = self.request("GET", target)
        return status, headers, json.loads(body.decode("utf-8"))

    def test_health_reports_exact_service_and_project_for_reuse(self):
        status, headers, payload = self.request_json("/api/review/health")

        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "ma5_daily_review")
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(Path(payload["project_root"]), self.root)
        self.assertEqual(payload["port"], self.port)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Python", headers["Server"])
        self.assertIsNone(headers["Access-Control-Allow-Origin"])

        fetched = fetch_review_health(self.port, timeout=1.0)
        self.assertEqual(fetched["project_root"], str(self.root))
        self.assertTrue(review_server_ready(self.port, base_dir=self.root))
        self.assertFalse(review_server_ready(self.port, base_dir=self.root / "other"))
        self.assertEqual(
            find_running_review_server(base_dir=self.root, port_start=self.port, port_end=self.port),
            self.port,
        )

    def test_contract_routes_call_injected_backend_with_expected_names_and_arguments(self):
        status, _headers, payload = self.request_json("/api/review/dates")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload, {"dates": ["2026-07-11", "2026-07-10"]})

        status, _headers, payload = self.request_json("/api/review?date=2026-07-10&broker=1")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload["review_date"], "2026-07-10")
        self.assertEqual(payload["broker"], {"requested": True})

        status, _headers, payload = self.request_json(
            "/api/review/evidence?date=2026-07-10&source=monitor%3AUS.HAO&line=42"
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload, {"date": "2026-07-10", "source": "monitor:US.HAO", "line": 42, "radius": 3})
        self.assertIn(("dates", self.root), self.api.calls)
        self.assertIn(("review", "2026-07-10", True, self.root), self.api.calls)
        self.assertIn(("evidence", "2026-07-10", "monitor:US.HAO", 42, 3, self.root), self.api.calls)

    def test_runtime_tasks_endpoint_is_read_only_and_root_scoped(self):
        runtime_dir = self.root / "outputs" / "monitor_runtime"
        runtime_dir.mkdir(parents=True)
        instance_id = "999999-unit"
        (runtime_dir / f"{instance_id}.log").write_text("hello dashboard\n", encoding="utf-8")
        (runtime_dir / f"{instance_id}.json").write_text(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "pid": 999999,
                    "task_name": "monitor_ma5",
                    "task_label": "盘中 MA5 盯盘",
                    "phase": "intraday",
                    "phase_label": "盘中监控",
                    "status": "finished",
                    "started_at": "2026-07-12T10:00:00+00:00",
                    "heartbeat_at": "2026-07-12T10:00:01+00:00",
                    "log_file": f"{instance_id}.log",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        status, headers, payload = self.request_json("/api/runtime/tasks")

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(payload["active_count"], 0)
        self.assertEqual(payload["tasks"][0]["instance_id"], instance_id)
        self.assertIn("hello dashboard", payload["tasks"][0]["log"])

    def test_dashboard_actions_require_confirmation_header_and_use_allowlist(self):
        status, _headers, payload = self.request_json("/api/actions/status")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(payload["watchcode"]["ready"])

        status, _headers, body = self.request("POST", "/api/actions/start-monitor")
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertIn(b"confirmation header", body)

        status, _headers, body = self.request(
            "POST",
            "/api/actions/start-monitor",
            headers={"X-MA5-Action": "1", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["action"], "start-monitor")
        self.assertIn(("launch", "start-monitor", self.root), self.action_api.calls)

        for target, action in [
            ("/api/actions/generate-premarket-watchcode", "generate-premarket-watchcode"),
            ("/api/actions/start-premarket-monitor", "start-premarket-monitor"),
        ]:
            status, _headers, body = self.request(
                "POST",
                target,
                headers={"X-MA5-Action": "1", "Sec-Fetch-Site": "same-origin"},
            )
            self.assertEqual(status, HTTPStatus.ACCEPTED)
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(payload["action"], action)
            self.assertIn(("launch", action, self.root), self.action_api.calls)

        status, _headers, body = self.request(
            "POST",
            "/api/actions/stop-monitor",
            headers={"X-MA5-Action": "1", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, HTTPStatus.OK)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["action"], "stop-monitor")
        self.assertIn(("launch", "stop-monitor", self.root), self.action_api.calls)

        status, headers, body = self.request("POST", "/api/review", headers={"X-MA5-Action": "1"})
        self.assertEqual(status, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(headers["Allow"], "GET, HEAD, POST")
        self.assertIn(b"method not allowed", body)

    def test_api_rejects_invalid_or_ambiguous_query_values(self):
        invalid_targets = [
            "/api/review?date=2026-02-30",
            "/api/review?date=2026-07-10&date=2026-07-11",
            "/api/review?broker=2",
            "/api/review?unknown=1",
            "/api/review/evidence?date=2026-07-10&source=.env&line=1",
            "/api/review/evidence?date=2026-07-10&source=..%2F.env&line=1",
            "/api/review/evidence?date=2026-07-10&source=monitor&line=0",
            "/api/review/evidence?date=2026-07-10&source=monitor&line=NaN",
            "/api/review?" + "&".join(f"x{index}=1" for index in range(9)),
        ]
        for target in invalid_targets:
            with self.subTest(target=target):
                status, _headers, payload = self.request_json(target)
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertFalse(payload["ok"])

    def test_backend_failures_map_to_safe_http_errors(self):
        expected = {
            "2026-07-01": HTTPStatus.NOT_FOUND,
            "2026-07-02": HTTPStatus.FORBIDDEN,
            "2026-07-03": HTTPStatus.INTERNAL_SERVER_ERROR,
        }
        for requested_date, expected_status in expected.items():
            with self.subTest(requested_date=requested_date):
                status, _headers, payload = self.request_json(f"/api/review?date={requested_date}")
                self.assertEqual(status, expected_status)
                self.assertFalse(payload["ok"])
                self.assertNotIn("secret backend detail", payload["error"])

    def test_static_and_api_responses_support_head_and_weak_etag_revalidation(self):
        status, headers, body = self.request("GET", "/app.js")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body, b"window.reviewReady = true;\n")
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertTrue(headers["ETag"].startswith('"'))
        self.assertIsNotNone(headers["Last-Modified"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

        status, head_headers, head_body = self.request("HEAD", "/app.js")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(head_body, b"")
        self.assertEqual(int(head_headers["Content-Length"]), len(body))
        self.assertEqual(head_headers["ETag"], headers["ETag"])

        status, cached_headers, cached_body = self.request(
            "GET",
            "/app.js",
            headers={"If-None-Match": f"W/{headers['ETag']}"},
        )
        self.assertEqual(status, HTTPStatus.NOT_MODIFIED)
        self.assertEqual(cached_body, b"")
        self.assertEqual(int(cached_headers["Content-Length"]), len(body))

    def test_only_approved_chart_names_are_served_read_only(self):
        status, headers, body = self.request("GET", "/charts/watch_code_daily_kline_latest.html")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIn(b"window.chartReady", body)
        self.assertIn("script-src 'self' 'unsafe-inline'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

        blocked = [
            "/charts/private.html",
            "/charts/../private.html",
            "/charts/%2e%2e%2fprivate.html",
            "/charts/watch_code_daily_kline_2026-99-99.html",
            "/%2e%2e%2f.env",
        ]
        for target in blocked:
            with self.subTest(target=target):
                status, _headers, payload = self.request_json(target)
                self.assertEqual(status, HTTPStatus.NOT_FOUND)
                self.assertNotIn("ALPACA_SECRET", payload["error"])

    def test_rejects_dns_rebinding_hosts_absolute_targets_and_writes(self):
        status, _headers, payload = self.request_json("/api/missing")
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.assertFalse(payload["ok"])

        status, _headers, body = self.request(
            "GET",
            "/api/review/health",
            host=f"attacker.example:{self.port}",
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn(b"invalid Host header", body)

        connection = HTTPConnection("127.0.0.1", self.port, timeout=3.0)
        connection.putrequest("GET", "/api/review/health", skip_host=True)
        connection.putheader("Host", f"127.0.0.1:{self.port}")
        connection.putheader("Host", f"localhost:{self.port}")
        connection.putheader("Connection", "close")
        connection.endheaders()
        response = connection.getresponse()
        duplicate_host_body = response.read()
        connection.close()
        self.assertEqual(response.status, HTTPStatus.BAD_REQUEST)
        self.assertIn(b"invalid Host header", duplicate_host_body)

        status, _headers, body = self.request(
            "GET",
            f"http://127.0.0.1:{self.port}/api/review/health",
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn(b"invalid request target", body)

        status, headers, body = self.request("POST", "/api/review")
        self.assertEqual(status, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(headers["Allow"], "GET, HEAD, POST")
        self.assertIn(b"method not allowed", body)


class ReviewServerBindingTests(TestCase):
    def test_binding_skips_an_occupied_port_and_url_formats_ipv6(self):
        with TemporaryDirectory() as tmp, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            first_port = int(occupied.getsockname()[1])
            end_port = min(65535, first_port + 20)
            if end_port == first_port:
                self.skipTest("ephemeral port was at the end of the valid range")

            server = bind_review_server(
                base_dir=Path(tmp),
                host="127.0.0.1",
                port_start=first_port,
                port_end=end_port,
                review_api=StubReviewAPI(),
            )
            try:
                self.assertGreater(int(server.server_address[1]), first_port)
            finally:
                server.server_close()

        self.assertEqual(review_dashboard_url(8788, host="::1"), "http://[::1]:8788/")


class DailyReviewLauncherTests(TestCase):
    def test_launcher_reuses_matching_server_without_spawning(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch.object(open_daily_review, "find_running_review_server", return_value=8791):
                with patch.object(open_daily_review, "fetch_review_health", return_value={"pid": 91, "started_at": "now"}):
                    with patch.object(open_daily_review, "write_review_server_state") as write_state:
                        with patch.object(open_daily_review.subprocess, "Popen") as popen:
                            url = open_daily_review.launch_daily_review(base_dir=root, open_browser=False)

            self.assertEqual(url, "http://127.0.0.1:8791/")
            popen.assert_not_called()
            self.assertEqual(write_state.call_args.args[0], root)
            self.assertEqual(write_state.call_args.args[1]["pid"], 91)

    def test_launcher_starts_hidden_service_with_redirected_utf8_logs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            child = MagicMock()
            child.pid = 1234
            with patch.object(open_daily_review, "find_running_review_server", return_value=None):
                with patch.object(open_daily_review, "wait_for_review_server", return_value=8792):
                    with patch.object(open_daily_review, "fetch_review_health", return_value={"pid": 1234}):
                        with patch.object(open_daily_review, "write_review_server_state"):
                            with patch.object(open_daily_review.subprocess, "Popen", return_value=child) as popen:
                                url = open_daily_review.launch_daily_review(base_dir=root, open_browser=False)

            self.assertEqual(url, "http://127.0.0.1:8792/")
            args, kwargs = popen.call_args
            self.assertEqual(args[0][1:3], ["-m", "alpaca_ma5_service.review_web"])
            self.assertEqual(kwargs["cwd"], str(root))
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(kwargs["env"]["PYTHONUNBUFFERED"], "1")
            self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertTrue(kwargs["stdout"].closed)
            self.assertTrue(kwargs["stderr"].closed)
            self.assertTrue(str(kwargs["stdout"].name).endswith("daily_review_server.out.log"))
            self.assertTrue(str(kwargs["stderr"].name).endswith("daily_review_server.err.log"))

    def test_launcher_terminates_only_its_child_when_readiness_times_out(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            child = MagicMock()
            child.pid = 1234
            child.poll.return_value = None
            child.wait.return_value = 0
            with patch.object(open_daily_review, "find_running_review_server", return_value=None):
                with patch.object(open_daily_review, "wait_for_review_server", return_value=None):
                    with patch.object(open_daily_review.subprocess, "Popen", return_value=child):
                        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                            open_daily_review.launch_daily_review(
                                base_dir=root,
                                timeout_seconds=0.0,
                                open_browser=False,
                            )

            child.terminate.assert_called_once_with()
            child.kill.assert_not_called()
