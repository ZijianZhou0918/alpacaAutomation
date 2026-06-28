from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.watchlist_charts import ensure_watchlist_chart_server_running, watchlist_chart_http_url
from alpaca_ma5_service.watchlist_generator import refresh_watchlist_chart_from_watch_codes


def refresh_current_watchcode_chart(
    lookback_days: int = 60,
    batch_size: int = 100,
    feed: str = "sip",
) -> None:
    """按当前 watch_codes.txt 刷新 daily K 线 HTML，不重新筛选股票。"""
    settings = build_settings()
    chart_path = refresh_watchlist_chart_from_watch_codes(
        settings=settings,
        lookback_days=lookback_days,
        batch_size=batch_size,
        feed=feed,
    )
    ensure_watchlist_chart_server_running(settings)
    print(f"Watchlist chart page: {chart_path}", flush=True)
    print(f"Watchlist chart HTTP URL: {watchlist_chart_http_url(settings)}", flush=True)


if __name__ == "__main__":
    # 点箭头运行只刷新当前 watch_codes.txt 对应的 HTML 页面，不重新生成 watch code。
    refresh_current_watchcode_chart()
