"""Standard backtest command implementation."""

from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from dataclasses import replace
from datetime import date, time, timedelta
from pathlib import Path
import csv
import html
import json
import re
import shutil
import sqlite3

from alpaca_ma5_service.config import BASE_DIR, build_settings
from alpaca_ma5_service.console_notify import send_console_notification
from alpaca_ma5_service.envfile import load_env_file
from alpaca_ma5_service.final_strategy import (
    BUY_NOTIONAL_USD,
    BUY_SIGNAL_PARAMS,
    INITIAL_CASH,
    MAX_DAILY_BUYS,
    MAX_POSITIONS,
    OPTIMIZATION_RULES,
    SLIPPAGE_PCT,
    STOP_PARAMS,
    STRATEGY_DESCRIPTION,
    STRATEGY_NAME,
    TARGET_RETURN_PCT,
    TARGET_YEARS,
    WATCHLIST_SIGNAL_PARAMS,
)
from alpaca_ma5_service.watchlist import read_watch_codes, to_alpaca_symbol
from alpaca_ma5_service.watchlist_generator import load_tradable_symbols
from backtest.daily_sources import load_massive_api_keys
from backtest.engine import BacktestConfig, BacktestResult, run_backtest
from backtest.paths import OFFICIAL_DAILY_DB_PATH


def main() -> None:
    # ===== Click-run configuration =====
    years = TARGET_YEARS
    output_root = BASE_DIR / "backtest" / "output"
    run_root = output_root / "final_strategy_runs"
    html_report_name = "backtest_report.html"

    runs: dict[int, BacktestResult] = {}
    print("Final strategy annual backtest starting.", flush=True)
    print(f"Strategy: {STRATEGY_NAME}", flush=True)
    print(f"Years: {', '.join(str(year) for year in years)}", flush=True)
    print(f"Fixed buy notional: ${BUY_NOTIONAL_USD:,.2f}", flush=True)
    print(f"Max daily buys: {MAX_DAILY_BUYS}; max positions: {MAX_POSITIONS}", flush=True)

    for year in years:
        year_dir = run_root / str(year)
        config = build_final_strategy_config(year_dir, html_report_name, year=year)
        print(f"\n=== {year} replay ===", flush=True)
        print(f"Date range: {config.start_date} -> {config.end_date}", flush=True)
        print(f"Symbols: {len(config.symbols)}", flush=True)
        result = run_backtest(config)
        runs[year] = result
        print(
            f"{year}: equity=${result.stats.final_equity:,.2f} "
            f"return={result.stats.return_pct:.2%} "
            f"drawdown={result.stats.max_drawdown_pct:.2%} "
            f"orders={result.stats.order_count}",
            flush=True,
        )

    summary_csv = write_annual_summary_csv(output_root / "final_strategy_summary.csv", runs)
    annual_report = write_annual_report(output_root / "final_strategy_report.html", runs)
    main_report = publish_main_report(annual_report, output_root / "backtest_report.html")

    best_floor = min(result.stats.return_pct for result in runs.values())
    print("\nFinal strategy annual backtest finished.")
    print(f"Annual floor: {best_floor:.2%} (target {TARGET_RETURN_PCT:.2%})")
    print(f"Annual report: {annual_report}")
    print(f"Main report alias: {main_report}")
    print(f"Summary CSV: {summary_csv}")
    send_console_notification(
        (
            "Final strategy annual backtest finished. "
            f"Annual floor: {best_floor:.2%}; "
            f"report: {main_report}"
        ),
        context="backtest finished",
    )


def build_final_strategy_config(output_dir: Path, html_report_name: str, *, year: int | None = None) -> BacktestConfig:
    project_settings = build_settings(strategy_name=STRATEGY_NAME, buy_stock_count=MAX_DAILY_BUYS)
    local_env = load_env_file(BASE_DIR / ".env")

    data_cache_path = OFFICIAL_DAILY_DB_PATH
    data_cache_dir = data_cache_path.parent
    data_cache_name = data_cache_path.name
    stock_symbols, stock_pool_description, cached_daily_start_date, cached_daily_end_date = stock_pool_from_sqlite(data_cache_path)
    if not stock_symbols:
        print("Loading active/tradable common-stock pool for backtest fallback.", flush=True)
        stock_symbols = load_tradable_symbols(max_symbols=None)
        stock_pool_description = "Alpaca active/tradable common-stock pool"
        if not stock_symbols:
            stock_symbols = read_watch_codes(project_settings.watch_codes_file)
            stock_pool_description = "Fallback to current watch_codes.txt"

    normal_stock_symbol_pattern = r"^[A-Z]{1,4}$"
    before_filter_count = len(stock_symbols)
    stock_symbols = [
        symbol
        for symbol in stock_symbols
        if re.fullmatch(normal_stock_symbol_pattern, to_alpaca_symbol(symbol) or "")
    ]
    stock_pool_description = (
        f"{stock_pool_description}; symbol_pattern={normal_stock_symbol_pattern}; "
        f"filtered={before_filter_count}->{len(stock_symbols)}"
    )

    warmup_calendar_days = 45
    if year is None:
        end_date = cached_daily_end_date or date.today()
        start_date = end_date - timedelta(days=365)
        if cached_daily_start_date:
            start_date = max(start_date, cached_daily_start_date + timedelta(days=warmup_calendar_days))
    else:
        start_date, end_date = backtest_date_range_for_year(year, cached_daily_end_date)

    strategy_settings = replace(
        project_settings,
        output_dir=output_dir,
        state_file=output_dir / "state.json",
        buy_notional_usd=BUY_NOTIONAL_USD,
        max_daily_buys=MAX_DAILY_BUYS,
        stop_loss_pct=STOP_PARAMS["stop_loss_pct"],
        stop_loss_limit_pct=STOP_PARAMS["stop_loss_limit_pct"],
        take_profit_half_pct=STOP_PARAMS["take_profit_half_pct"],
        take_profit_sell_fraction=STOP_PARAMS["take_profit_sell_fraction"],
        take_profit_remainder_stop_pct=STOP_PARAMS["take_profit_remainder_stop_pct"],
        close_liquidation_start=time(15, 55),
        close_liquidation_end=time(16, 0),
    )

    return BacktestConfig(
        symbols=stock_symbols,
        start_date=start_date,
        end_date=end_date,
        timeframe="1Min",
        initial_cash=INITIAL_CASH,
        buy_notional_usd=BUY_NOTIONAL_USD,
        buy_position_pct=0.0,
        max_positions=MAX_POSITIONS,
        max_daily_buys=MAX_DAILY_BUYS,
        commission_per_order=0.0,
        slippage_pct=SLIPPAGE_PCT,
        allow_repeat_buys=False,
        allow_overnight_holding=False,
        allow_fractional_shares=project_settings.allow_fractional_shares,
        data_feed="sip",
        daily_data_source="alpaca",
        batch_size=100,
        data_chunk_days=14,
        use_data_cache=True,
        cache_daily_bars=True,
        cache_minute_bars=False,
        refresh_data_cache=False,
        data_cache_dir=data_cache_dir,
        data_cache_name=data_cache_name,
        warmup_calendar_days=warmup_calendar_days,
        market_timezone=project_settings.market_timezone,
        order_timeout_seconds=project_settings.order_cancel_after_seconds,
        report_max_points_per_series=5000,
        report_max_price_symbols=0,
        report_price_context_days=5,
        stock_pool_description=stock_pool_description,
        require_buy_day_open_below_signal_reference=False,
        output_dir=output_dir,
        html_report_name=html_report_name,
        strategy_settings=strategy_settings,
        watchlist_signal_params=dict(WATCHLIST_SIGNAL_PARAMS),
        buy_signal_params=dict(BUY_SIGNAL_PARAMS),
        sell_signal_params={
            "close_liquidation_start": strategy_settings.close_liquidation_start,
            "close_liquidation_end": strategy_settings.close_liquidation_end,
            "monitor_entry": f"monitor_ma5_forever(buy_stock_count={MAX_DAILY_BUYS})",
        },
        stop_params=dict(STOP_PARAMS),
        moomoo_host=project_settings.moomoo_host,
        moomoo_port=project_settings.moomoo_port,
        moomoo_security_firm=project_settings.moomoo_security_firm,
        moomoo_connect_timeout=project_settings.moomoo_connect_timeout,
        moomoo_opend_exe_path=project_settings.moomoo_opend_exe_path,
        moomoo_opend_startup_timeout=project_settings.moomoo_opend_startup_timeout,
        yahoo_request_sleep_seconds=0.05,
        yahoo_rate_limit_retry_seconds=10.0,
        yahoo_max_retries=3,
        massive_api_keys=load_massive_api_keys(local_env),
        massive_max_workers=6,
        massive_request_timeout_seconds=30.0,
        massive_retry_sleep_seconds=3.0,
        massive_max_retries=3,
        massive_progress_interval_seconds=10.0,
        massive_progress_interval_dates=20,
        massive_fallback_to_yahoo=True,
        strategy_name=STRATEGY_NAME,
        strategy_variant_name=f"{STRATEGY_NAME}_{year}" if year is not None else STRATEGY_NAME,
        strategy_variant_description=STRATEGY_DESCRIPTION,
        optimization_rules=dict(OPTIMIZATION_RULES),
        require_daily_cache_coverage=True,
        data_cache_read_only=True,
    )


def stock_pool_from_sqlite(data_cache_path: Path) -> tuple[list[str], str, date | None, date | None]:
    if not data_cache_path.exists():
        return [], "", None, None

    read_only_uri = f"{data_cache_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(read_only_uri, uri=True) as conn:
        symbols = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT symbol
                FROM daily_bars
                ORDER BY symbol
                """
            )
        ]
        min_date_text, max_date_text = conn.execute("SELECT MIN(bar_date), MAX(bar_date) FROM daily_bars").fetchone()
        cached_daily_start_date = date.fromisoformat(min_date_text) if min_date_text else None
        cached_daily_end_date = date.fromisoformat(max_date_text) if max_date_text else None
        if cached_daily_end_date:
            expected_end_key = (cached_daily_end_date + timedelta(days=1)).isoformat()
            covered_start_text = conn.execute(
                """
                SELECT MAX(start_key)
                FROM fetch_ranges
                WHERE kind = 'daily'
                  AND end_key >= ?
                """,
                (expected_end_key,),
            ).fetchone()[0]
            if covered_start_text:
                covered_start_date = date.fromisoformat(covered_start_text)
                cached_daily_start_date = max(cached_daily_start_date or covered_start_date, covered_start_date)
    return symbols, f"Local SQLite stock pool: {data_cache_path}", cached_daily_start_date, cached_daily_end_date


def backtest_date_range_for_year(year: int, cached_daily_end_date: date | None, *, today: date | None = None) -> tuple[date, date]:
    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)
    current_date = today or date.today()
    if year >= current_date.year:
        latest_complete_date = current_date - timedelta(days=1)
        if cached_daily_end_date is not None:
            latest_complete_date = min(latest_complete_date, cached_daily_end_date)
        end_date = min(end_date, latest_complete_date)
    return start_date, end_date


def write_annual_summary_csv(path: Path, runs: dict[int, BacktestResult]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "year",
                "start_date",
                "end_date",
                "period",
                "strategy",
                "target_return_pct",
                "return_pct",
                "final_equity",
                "max_drawdown_pct",
                "order_count",
                "buy_order_count",
                "sell_order_count",
                "minute_bar_count",
                "report_path",
                "rules",
            ],
        )
        writer.writeheader()
        for year, result in sorted(runs.items()):
            writer.writerow(
                {
                    "year": year,
                    "start_date": result.config.start_date,
                    "end_date": result.config.end_date,
                    "period": period_label(year, result),
                    "strategy": STRATEGY_NAME,
                    "target_return_pct": TARGET_RETURN_PCT,
                    "return_pct": result.stats.return_pct,
                    "final_equity": result.stats.final_equity,
                    "max_drawdown_pct": result.stats.max_drawdown_pct,
                    "order_count": result.stats.order_count,
                    "buy_order_count": result.stats.buy_order_count,
                    "sell_order_count": result.stats.sell_order_count,
                    "minute_bar_count": result.minute_bar_count,
                    "report_path": result.report_path,
                    "rules": json.dumps(strategy_rule_summary(), ensure_ascii=False, default=str),
                }
            )
    return path


def write_annual_report(path: Path, runs: dict[int, BacktestResult]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    annual_floor = min(result.stats.return_pct for result in runs.values())
    target_hit = all(result.stats.return_pct >= TARGET_RETURN_PCT for result in runs.values())
    rows = []
    for year, result in sorted(runs.items()):
        detail_url = relative_report_path(path, result.report_path)
        rows.append(
            "<tr>"
            f"<td>{year}</td>"
            f"<td>{result.config.start_date}</td>"
            f"<td>{result.config.end_date}</td>"
            f"<td>{html.escape(period_label(year, result))}</td>"
            f"<td>{money(result.stats.final_equity)}</td>"
            f"<td>{pct(result.stats.return_pct)}</td>"
            f"<td>{pct(result.stats.max_drawdown_pct)}</td>"
            f"<td>{result.stats.order_count}</td>"
            f"<td>{result.minute_bar_count:,}</td>"
            f"<td><a href='{html.escape(detail_url)}'>detail</a></td>"
            "</tr>"
        )
    rules = json.dumps(strategy_rule_summary(), ensure_ascii=False, indent=2, default=str)
    status = "PASS" if target_hit else "FAIL"
    path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Final Strategy Annual Backtest</title>
  <style>
    body {{ font-family: Arial, "Microsoft YaHei", sans-serif; margin: 24px; color: #18202a; }}
    h1, h2 {{ margin: 0 0 12px; }}
    section {{ margin: 24px 0; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border: 1px solid #d7dde6; padding: 7px 8px; text-align: right; white-space: nowrap; }}
    th {{ background: #f3f6fa; }}
    td:first-child, th:first-child {{ text-align: left; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }}
    .metric {{ border: 1px solid #d7dde6; border-radius: 6px; padding: 10px; background: #fbfcfe; }}
    .metric strong {{ display: block; margin-top: 4px; font-size: 18px; }}
    .note {{ color: #5d6876; font-size: 13px; line-height: 1.45; }}
    pre {{ background: #f7f9fc; border: 1px solid #d7dde6; padding: 12px; overflow: auto; }}
    a {{ color: #0f5f9f; text-decoration: none; }}
  </style>
</head>
<body>
  <h1>Final Strategy Annual Backtest</h1>
  <p class="note">This report only runs the final selected strategy. It does not include optimizer variants. Current or future years are clipped to the latest complete local SQLite daily data date.</p>
  <section>
    <h2>Summary</h2>
    <div class="grid">
      <div class="metric">Status<strong>{status}</strong></div>
      <div class="metric">Period floor<strong>{pct(annual_floor)}</strong></div>
      <div class="metric">Target<strong>{pct(TARGET_RETURN_PCT)} per listed period</strong></div>
      <div class="metric">Fixed buy<strong>{money(BUY_NOTIONAL_USD)}</strong></div>
    </div>
  </section>
  <section>
    <h2>Strategy</h2>
    <p><strong>{html.escape(STRATEGY_NAME)}</strong></p>
    <p class="note">{html.escape(STRATEGY_DESCRIPTION)}</p>
    <pre>{html.escape(rules)}</pre>
  </section>
  <section>
    <h2>Annual Results</h2>
    <table>
      <thead><tr><th>year</th><th>start</th><th>end</th><th>period</th><th>final equity</th><th>return</th><th>max drawdown</th><th>orders</th><th>1Min bars</th><th>detail</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </section>
  <section>
    <h2>Risk</h2>
    <p class="note">The selected strategy still depends on the historical dataset and zero-slippage assumption. Revalidate non-zero slippage separately before live use.</p>
  </section>
</body>
</html>
""",
        encoding="utf-8",
    )
    return path


def publish_main_report(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return target


def period_label(year: int, result: BacktestResult) -> str:
    if result.config.start_date > date(year, 1, 1) or result.config.end_date < date(year, 12, 31):
        return "partial"
    return "full"


def strategy_rule_summary() -> dict[str, object]:
    return {
        "watchlist_signal_params": WATCHLIST_SIGNAL_PARAMS,
        "buy_signal_params": BUY_SIGNAL_PARAMS,
        "stop_params": STOP_PARAMS,
        "config": {
            "initial_cash": INITIAL_CASH,
            "buy_notional_usd": BUY_NOTIONAL_USD,
            "max_daily_buys": MAX_DAILY_BUYS,
            "max_positions": MAX_POSITIONS,
            "slippage_pct": SLIPPAGE_PCT,
        },
        "optimization_rules": OPTIMIZATION_RULES,
    }


def relative_report_path(base_report: Path, detail_report: Path) -> str:
    try:
        return detail_report.relative_to(base_report.parent).as_posix()
    except ValueError:
        return detail_report.as_posix()


def money(value: float) -> str:
    return f"${value:,.2f}"


def pct(value: float) -> str:
    return f"{value:.2%}"


if __name__ == "__main__":
    main()
