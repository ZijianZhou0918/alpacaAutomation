from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .alpaca_connection import build_trading_connection, load_alpaca_credentials
from .config import Settings, build_settings
from .errors import short_error


@dataclass(frozen=True)
class DailyBar:
    """策略筛选只需要的日线字段。"""

    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class WatchCandidate:
    """满足选股规则后写入 watch_codes 的候选股票。"""

    symbol: str
    signal_date: date
    gain_pct: float
    upper_shadow_pct: float
    ma5: float
    ma10: float
    ma20: float
    open: float
    high: float
    close: float


def generate_watch_codes(
    settings: Settings | None = None,
    symbols: list[str] | None = None,
    max_symbols: int | None = None,
    lookback_days: int = 60,
    batch_size: int = 100,
    feed: str = "sip",
) -> list[WatchCandidate]:
    """只用 Alpaca 美股股票日线数据按策略生成 watch_codes.txt。"""
    settings = settings or build_settings()
    now_et = datetime.now(ZoneInfo(settings.market_timezone))
    symbol_pool = symbols or load_tradable_symbols(max_symbols=max_symbols)
    if max_symbols is not None:
        symbol_pool = symbol_pool[:max_symbols]

    print(f"开始生成 watch_codes：symbols={len(symbol_pool)} feed={feed}", flush=True)
    bars_by_symbol = fetch_daily_bars(symbol_pool, now_et, lookback_days, batch_size, feed)
    candidates = screen_candidates(bars_by_symbol, now_et)
    validate_candidates(candidates)
    write_watch_codes(settings.watch_codes_file, candidates)
    write_candidate_report(settings.output_dir, candidates)
    print(f"生成完成：{len(candidates)} 个候选，已写入 {settings.watch_codes_file}", flush=True)
    return candidates


def load_tradable_symbols(max_symbols: int | None = None) -> list[str]:
    """从 Alpaca assets 只读取 active/tradable 的美股股票池，不包含期权/crypto。"""
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    client = build_trading_connection().client
    # 明确限定 US_EQUITY，避免期权、crypto 或其他资产混入选股池。
    request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    assets = client.get_all_assets(request)
    symbols = sorted(
        str(getattr(asset, "symbol", "")).upper()
        for asset in assets
        if getattr(asset, "tradable", False) and getattr(asset, "symbol", "")
    )
    return symbols[:max_symbols] if max_symbols is not None else symbols


def fetch_daily_bars(
    symbols: list[str],
    now_et: datetime,
    lookback_days: int,
    batch_size: int,
    feed: str,
) -> dict[str, list[DailyBar]]:
    """分批读取 Alpaca 美股日线 bar，失败批次打印原因后继续。"""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    api_key, secret_key = load_alpaca_credentials()
    client = StockHistoricalDataClient(api_key, secret_key)
    end = request_end_datetime(now_et)
    start = end - timedelta(days=lookback_days)
    bars_by_symbol: dict[str, list[DailyBar]] = {}

    for batch in batched(symbols, batch_size):
        try:
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                limit=batch_size * lookback_days,
                # 使用拆股调整后的日线，避免合股/拆股股票的均线和常见图表严重不一致。
                adjustment=Adjustment.SPLIT,
                # 全部使用 SIP 全市场日线，避免 IEX 局部成交导致均线失真。
                feed=DataFeed(feed),
            )
            raw_bars = client.get_stock_bars(request).data
        except Exception as exc:
            print(f"日线读取失败，跳过 {batch[0]}...{batch[-1]}：{short_error(exc)}", flush=True)
            continue

        for symbol, bars in raw_bars.items():
            bars_by_symbol[symbol.upper()] = [daily_bar_from_alpaca(symbol.upper(), bar, now_et) for bar in bars]

    return bars_by_symbol


def screen_candidates(bars_by_symbol: dict[str, list[DailyBar]], now_et: datetime) -> list[WatchCandidate]:
    """对每个股票使用最近已收盘交易日作为 signal_date 做策略筛选。"""
    signal_date = latest_completed_signal_date(bars_by_symbol, now_et)
    if signal_date is None:
        return []

    candidates: list[WatchCandidate] = []
    for symbol, bars in bars_by_symbol.items():
        candidate = evaluate_watch_candidate(symbol, bars, now_et, signal_date)
        if candidate:
            candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item.gain_pct, item.upper_shadow_pct), reverse=True)


def latest_completed_signal_date(bars_by_symbol: dict[str, list[DailyBar]], now_et: datetime) -> date | None:
    """从全部日线里找最近一个已收盘交易日，作为统一 signal_date。"""
    dates = [
        bar.date
        for bars in bars_by_symbol.values()
        for bar in bars
        if is_completed_bar(bar, now_et)
    ]
    return max(dates) if dates else None


def evaluate_watch_candidate(symbol: str, bars: list[DailyBar], now_et: datetime, signal_date: date) -> WatchCandidate | None:
    """检查单个股票是否满足涨幅、上影线、均线多头和 open>MA5。"""
    completed = [bar for bar in sorted(bars, key=lambda item: item.date) if is_completed_bar(bar, now_et)]
    signal_index = next((index for index, bar in enumerate(completed) if bar.date == signal_date), None)
    if signal_index is None or signal_index < 19:
        return None

    signal = completed[signal_index]
    previous = completed[signal_index - 1]
    if previous.close <= 0:
        return None

    closes20 = [bar.close for bar in completed[signal_index - 19 : signal_index + 1]]
    ma5 = average(closes20[-5:])
    ma10 = average(closes20[-10:])
    ma20 = average(closes20)
    gain_pct = signal.close / previous.close - 1.0

    # 上影线用前一日收盘价作分母，和当天涨幅口径保持一致。
    upper_shadow_pct = (signal.high - max(signal.open, signal.close)) / previous.close

    if gain_pct <= 0.20:
        return None
    if upper_shadow_pct <= 0.05:
        return None
    if not (ma5 > ma10 > ma20):
        return None
    if signal.open <= ma5:
        return None

    return WatchCandidate(symbol, signal.date, gain_pct, upper_shadow_pct, ma5, ma10, ma20, signal.open, signal.high, signal.close)


def validate_candidates(candidates: list[WatchCandidate]) -> None:
    """写入 watch_codes 前强制校验，防止不满足规则的股票进入监控列表。"""
    for candidate in candidates:
        if not (candidate.ma5 > candidate.ma10 > candidate.ma20):
            raise RuntimeError(f"{candidate.symbol} 均线不满足 MA5>MA10>MA20")
        if candidate.open <= candidate.ma5:
            raise RuntimeError(f"{candidate.symbol} 开盘价不满足 open>MA5")
        if candidate.gain_pct <= 0.20:
            raise RuntimeError(f"{candidate.symbol} 涨幅不满足 >20%")
        if candidate.upper_shadow_pct <= 0.05:
            raise RuntimeError(f"{candidate.symbol} 上影线不满足 >5%")


def is_completed_bar(bar: DailyBar, now_et: datetime) -> bool:
    """判断日线是否已经收盘；盘后 16:15 ET 以后允许使用当天 bar。"""
    if bar.date < now_et.date():
        return True
    after_close_buffer = now_et.hour > 16 or (now_et.hour == 16 and now_et.minute >= 15)
    return bar.date == now_et.date() and after_close_buffer


def average(values: list[float]) -> float:
    """计算简单平均值。"""
    return sum(values) / len(values)


def daily_bar_from_alpaca(symbol: str, bar, now_et: datetime) -> DailyBar:
    """把 alpaca-py Bar 对象转换成内部 DailyBar。"""
    bar_date = bar.timestamp.astimezone(now_et.tzinfo).date()
    return DailyBar(symbol, bar_date, float(bar.open), float(bar.high), float(bar.low), float(bar.close))


def request_end_datetime(now_et: datetime) -> datetime:
    """日线请求使用日期边界，避免 SIP recent 查询限制。"""
    if now_et.weekday() < 5 and (now_et.hour > 16 or (now_et.hour == 16 and now_et.minute >= 15)):
        end_date = now_et.date() + timedelta(days=1)
    else:
        end_date = now_et.date()
    return datetime.combine(end_date, time.min, tzinfo=now_et.tzinfo)


def write_watch_codes(path: Path, candidates: list[WatchCandidate]) -> None:
    """把候选股票写成监控程序可直接读取的 watch_codes.txt。"""
    lines = [
        "# Auto-generated by run_generate_watch_codes.py",
        "# Rules: gain>20%, upper_shadow>5%, MA5>MA10>MA20, open>MA5",
    ]
    if candidates:
        lines.append(f"# signal_date={candidates[0].signal_date}")
    lines.extend(f"US.{candidate.symbol}" for candidate in candidates)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_candidate_report(output_dir: Path, candidates: list[WatchCandidate]) -> None:
    """把候选股票的诊断数据写到 outputs，方便复盘为什么入选。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    day = candidates[0].signal_date if candidates else datetime.now().date()
    path = output_dir / f"watch_candidates_{day}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["symbol", "signal_date", "gain_pct", "upper_shadow_pct", "ma5", "ma10", "ma20", "open", "high", "close"],
        )
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate.__dict__)


def batched(values: list[str], size: int):
    """把股票列表切成 API 请求批次。"""
    for start in range(0, len(values), size):
        yield values[start : start + size]
