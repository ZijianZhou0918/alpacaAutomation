from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from .watchlist import normalize_symbol


DEFAULT_OPEND_EXE_PATH = r"%APPDATA%\moomoo_OpenD\moomoo_OpenD.exe"
LOCAL_OPEND_HOSTS = {"127.0.0.1", "localhost", "::1"}
SNAPSHOT_PRICE_FIELDS = [
    "last_price",
    "nominal_price",
    "pre_price",
    "after_price",
    "overnight_price",
    "bid_price",
    "ask_price",
]
SNAPSHOT_OPEN_FIELDS = ["open_price", "open", "day_open", "regular_open_price"]


class MoomooQuoteError(RuntimeError):
    """Moomoo OpenD 连接或快照读取失败。"""


@dataclass(frozen=True)
class PriceQuote:
    price: float
    source: str
    today_open: float = 0.0
    today_open_source: str = ""
    as_of: datetime | None = None


class MoomooRealtimePriceSource:
    """通过 Moomoo OpenD 快照接口读取美股实时价。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        security_firm: str = "FUTUINC",
        connect_timeout: float = 3.0,
        opend_exe_path: str | None = DEFAULT_OPEND_EXE_PATH,
        opend_startup_timeout: float = 30.0,
    ):
        self.host = host
        self.port = port
        self.security_firm = security_firm
        self.connect_timeout = connect_timeout
        self.opend_exe_path = opend_exe_path
        self.opend_startup_timeout = opend_startup_timeout
        self.mm = None
        self.quote_ctx = None
        self._last_snapshot_time = 0.0

    def latest_price(self, symbol: str) -> float:
        """返回当前价；失败时会重连一次再试。"""
        return self.latest_price_quote(symbol).price

    def latest_price_quote(self, symbol: str) -> PriceQuote:
        """返回当前价，并标出具体来自哪个 Moomoo 快照字段。"""
        code = normalize_symbol(symbol)
        try:
            return self._latest_price_quote_once(code)
        except Exception:
            self.close()
            return self._latest_price_quote_once(code)

    def _latest_price_quote_once(self, code: str) -> PriceQuote:
        self._connect()
        self._throttle_snapshot()
        ret, data = self.quote_ctx.get_market_snapshot([code])
        if ret != self.mm.RET_OK:
            raise MoomooQuoteError(f"获取 {code} Moomoo 快照失败：{data}")
        price, field = snapshot_price_with_field(data)
        if price <= 0:
            raise MoomooQuoteError(f"{code} Moomoo 快照没有有效价格")
        today_open, open_field = snapshot_open_with_field(data)
        open_source = f"moomoo_snapshot:{open_field}" if today_open > 0 else ""
        return PriceQuote(price, f"moomoo_snapshot:{field}", today_open, open_source, snapshot_update_time(data))

    def _connect(self) -> None:
        if self.quote_ctx is not None:
            return
        self._ensure_opend_reachable()
        self.mm = load_moomoo_module()
        self.quote_ctx = self.mm.OpenQuoteContext(
            host=self.host,
            port=self.port,
            security_firm=enum_value(self.mm, "SecurityFirm", self.security_firm),
        )

    def _ensure_opend_reachable(self) -> None:
        if opend_reachable(self.host, self.port, self.connect_timeout):
            return
        if self.host not in LOCAL_OPEND_HOSTS:
            raise MoomooQuoteError(f"无法连接 Moomoo OpenD {self.host}:{self.port}")

        opend_path = Path(os.path.expandvars(self.opend_exe_path or "")).expanduser()
        if not opend_path.is_file():
            raise MoomooQuoteError(f"无法连接 Moomoo OpenD，且启动路径不存在：{opend_path}")

        print(f"Moomoo OpenD 未启动，正在打开 {opend_path} ...", flush=True)
        subprocess.Popen([str(opend_path)], cwd=str(opend_path.parent))
        deadline = time.monotonic() + max(self.opend_startup_timeout, 0.0)
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if opend_reachable(self.host, self.port, self.connect_timeout):
                return
        raise MoomooQuoteError(f"Moomoo OpenD 在 {self.opend_startup_timeout:.1f}s 内未就绪")

    def _throttle_snapshot(self) -> None:
        # 复用 StockAPI 的限频思路，避免监控循环把 OpenD 快照接口打满。
        elapsed = time.monotonic() - self._last_snapshot_time
        if elapsed < 0.05:
            time.sleep(0.05 - elapsed)
        self._last_snapshot_time = time.monotonic()

    def close(self) -> None:
        if self.quote_ctx is not None:
            try:
                self.quote_ctx.close()
            finally:
                self.quote_ctx = None


def load_moomoo_module() -> Any:
    try:
        import moomoo as mm
    except ImportError as exc:
        raise MoomooQuoteError("缺少 moomoo SDK，请运行：python -m pip install moomoo-api") from exc
    mm.SysConfig.enable_console_log(False)
    mm.SysConfig.set_all_thread_daemon(True)
    return mm


def enum_value(mm: Any, enum_name: str, member_name: str) -> Any:
    enum_cls = getattr(mm, enum_name)
    try:
        return getattr(enum_cls, member_name.upper())
    except AttributeError as exc:
        choices = ", ".join(name for name in dir(enum_cls) if name.isupper())
        raise MoomooQuoteError(f"{enum_name}.{member_name} 不存在，可选值：{choices}") from exc


def opend_reachable(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def snapshot_price(snapshot: Any) -> float:
    return snapshot_price_with_field(snapshot)[0]


def snapshot_price_with_field(snapshot: Any) -> tuple[float, str]:
    if snapshot is None or getattr(snapshot, "empty", False):
        return 0.0, ""
    return snapshot_price_from_row_with_field(snapshot.iloc[0])


def snapshot_price_from_row(row: Any) -> float:
    return snapshot_price_from_row_with_field(row)[0]


def snapshot_price_from_row_with_field(row: Any) -> tuple[float, str]:
    for field in SNAPSHOT_PRICE_FIELDS:
        value = numeric(row.get(field, 0.0))
        if value > 0:
            return value, field
    return 0.0, ""


def snapshot_open_with_field(snapshot: Any) -> tuple[float, str]:
    if snapshot is None or getattr(snapshot, "empty", False):
        return 0.0, ""
    return snapshot_open_from_row_with_field(snapshot.iloc[0])


def snapshot_open_from_row(row: Any) -> float:
    return snapshot_open_from_row_with_field(row)[0]


def snapshot_open_from_row_with_field(row: Any) -> tuple[float, str]:
    for field in SNAPSHOT_OPEN_FIELDS:
        value = numeric(row.get(field, 0.0))
        if value > 0:
            return value, field
    return 0.0, ""


def snapshot_update_time(snapshot: Any) -> datetime | None:
    """读取 Moomoo 快照更新时间，用来避免上一交易日价格误触发。"""
    if snapshot is None or getattr(snapshot, "empty", False):
        return None
    return snapshot_update_time_from_row(snapshot.iloc[0])


def snapshot_update_time_from_row(row: Any) -> datetime | None:
    value = row.get("update_time", None)
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def numeric(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
