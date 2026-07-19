"""Official daily-history rebuild command implementation."""

from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca_ma5_service.config import BASE_DIR
from alpaca_ma5_service.trading_calendar import latest_trading_day_on_or_before
from backtest.daily_history_rebuild import (
    DailyHistoryRebuildConfig,
    run_daily_history_rebuild,
)
from backtest.paths import OFFICIAL_DAILY_DB_PATH


MARKET_TZ = ZoneInfo("America/New_York")


def main() -> None:
    default_start, default_end = default_two_year_window()
    parser = argparse.ArgumentParser(
        description="重建过去两年的全普通股日线 SQLite 行情库。"
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=default_start)
    parser.add_argument("--end-date", type=date.fromisoformat, default=default_end)
    parser.add_argument(
        "--database",
        type=Path,
        default=OFFICIAL_DAILY_DB_PATH,
    )
    parser.add_argument(
        "--staging",
        type=Path,
        default=BASE_DIR / "backtest" / "data" / "market_data.sqlite.rebuild",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=BASE_DIR / "backtest" / "output",
    )
    parser.add_argument("--batch-size", type=int, default=900)
    parser.add_argument("--http-workers", type=int, default=4)
    parser.add_argument(
        "--symbols",
        default="",
        help="仅用于烟测的逗号分隔代码；留空表示全普通股候选池。",
    )
    parser.add_argument(
        "--no-replace",
        action="store_true",
        help="完整后仍保留 staging，不原子替换正式库。",
    )
    args = parser.parse_args()

    args.database = args.database.resolve()
    args.staging = args.staging.resolve()
    args.output_dir = args.output_dir.resolve()
    symbols = tuple(
        value.strip().upper()
        for value in args.symbols.split(",")
        if value.strip()
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.output_dir / "daily_history_rebuild.log"

    def log(message: str) -> None:
        line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    result = run_daily_history_rebuild(
        DailyHistoryRebuildConfig(
            start_date=args.start_date,
            end_date=args.end_date,
            final_path=args.database,
            staging_path=args.staging,
            output_dir=args.output_dir,
            batch_size=args.batch_size,
            http_workers=args.http_workers,
            symbols_override=symbols,
            replace_on_complete=not args.no_replace,
        ),
        logger=log,
    )
    print("Daily history rebuild finished.")
    print(f"Database: {result.database_path}")
    print(
        "Candidate/observed symbols: "
        f"{result.candidate_symbols:,} / {result.observed_symbols:,}"
    )
    print(f"Trading sessions: {result.trading_sessions:,}")
    print(f"Rows: {result.total_rows:,}")
    print(f"Request pages: {result.request_pages:,}")
    print(f"Size: {result.database_bytes / 1024**3:.2f} GiB")
    print(f"Backup: {result.backup_path or '-'}")
    print(f"Manifest: {result.report_path}")


def default_two_year_window() -> tuple[date, date]:
    today_et = datetime.now(MARKET_TZ).date()
    start = subtract_years(today_et, 2)
    end = latest_trading_day_on_or_before(today_et - timedelta(days=1))
    return start, end


def subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, month=2, day=28)


if __name__ == "__main__":
    main()
