from __future__ import annotations

from dataclasses import replace

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.watchlist_charts import ensure_watchlist_chart_server_running, watchlist_chart_http_url
from alpaca_ma5_service.watchlist_generator import refresh_watchlist_chart_from_watch_codes


# 点箭头运行只改这里：premarket / intraday / afterhours，也支持 盘前 / 盘中 / 盘后。
CHART_SESSION = "intraday"

WATCH_FILE_BY_SESSION = {
    "premarket": "watch_codes_premarket.txt",
    "盘前": "watch_codes_premarket.txt",
    "intraday": "watch_codes.txt",
    "regular": "watch_codes.txt",
    "盘中": "watch_codes.txt",
    "afterhours": "watch_code_afterhours.txt",
    "盘后": "watch_code_afterhours.txt",
}


def chart_settings_for_session(settings, session: str = CHART_SESSION):
    """按盘前/盘中/盘后选择对应 watchcode 文件。"""
    key = (session or "intraday").strip().lower()
    watch_file = WATCH_FILE_BY_SESSION.get(key)
    if watch_file is None:
        allowed = "premarket, intraday, afterhours, 盘前, 盘中, 盘后"
        raise ValueError(f"CHART_SESSION 只能是 {allowed}，当前是 {session!r}")
    return replace(settings, watch_codes_file=settings.watch_codes_file.with_name(watch_file))


def refresh_current_watchcode_chart(
    session: str = CHART_SESSION,
    lookback_days: int = 60,
    batch_size: int = 100,
    feed: str = "sip",
) -> None:
    """按指定盘前/盘中/盘后 watchcode 文件刷新 daily K 线 HTML，不重新筛选股票。"""
    settings = chart_settings_for_session(build_settings(), session)
    print(f"Using watch codes file: {settings.watch_codes_file}", flush=True)
    chart_path = refresh_watchlist_chart_from_watch_codes(
        settings=settings,
        lookback_days=lookback_days,
        batch_size=batch_size,
        feed=feed,
    )
    server_port = ensure_watchlist_chart_server_running(settings)
    print(f"Watchlist chart page: {chart_path}", flush=True)
    print(f"Watchlist chart HTTP URL: {watchlist_chart_http_url(settings, port=server_port)}", flush=True)


if __name__ == "__main__":
    # 点箭头运行只刷新 CHART_SESSION 对应的 HTML 页面，不重新生成 watch code。
    refresh_current_watchcode_chart(session="盘中")
