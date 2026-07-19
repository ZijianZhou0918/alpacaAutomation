from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from intraday_top20.backtest.config import BacktestConfig, load_config
from intraday_top20.backtest.engine import IntradayTopGainersBacktester
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.data.cache import ResultStore
from intraday_top20.data.loader import MarketDataLoader
from intraday_top20.data.sample_data import generate_example_data

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "intraday_top20" / "config" / "default_config.yaml"


def bootstrap() -> tuple[BacktestConfig, BacktestResult | None]:
    if "intraday_config" not in st.session_state:
        st.session_state.intraday_config = load_config(DEFAULT_CONFIG)
    config: BacktestConfig = st.session_state.intraday_config
    data_dir = Path(config.data.data_dir)
    if config.data.example_mode and not list(data_dir.glob("synthetic_full_market_*.csv.gz")):
        generate_example_data(data_dir, seed=config.random_seed)
    if "intraday_result" not in st.session_state:
        loader = MarketDataLoader(config)
        run_id = config.config_hash(loader.fingerprint())
        store = ResultStore(config.output.output_root)
        st.session_state.intraday_result = store.load(run_id) if store.has(run_id) else None
    return config, st.session_state.intraday_result


def run_or_load(config: BacktestConfig, *, force: bool = False) -> BacktestResult:
    loader = MarketDataLoader(config)
    store = ResultStore(config.output.output_root)
    run_id = config.config_hash(loader.fingerprint())
    if not force and store.has(run_id):
        result = store.load(run_id)
        st.session_state.intraday_result = result
        st.session_state.intraday_config = config
        return result
    progress_bar = st.progress(0.0, text="准备加载数据")
    status = st.empty()

    def update(stage: str, current: int, total: int, message: str) -> None:
        progress_bar.progress(min(1.0, current / max(total, 1)), text=message)
        status.info(f"{stage}: {message}")

    result = IntradayTopGainersBacktester(config, loader).run(update)
    store.save(result)
    progress_bar.progress(1.0, text="回测完成并已缓存")
    status.success(f"完成：{result.run_id}")
    st.session_state.intraday_result = result
    st.session_state.intraday_config = config
    return result


def require_result(result: BacktestResult | None) -> BacktestResult | None:
    if result is None:
        st.info("尚无回测结果。请先到“参数与运行”页面运行回测，或加载已缓存结果。")
    return result


def render_data_banner(result: BacktestResult | None) -> None:
    if result is None:
        st.warning("当前未加载结果。默认数据是明确标记的合成样例。")
        return
    if result.is_example:
        st.error("合成示例数据：以下指标仅验证代码和网页功能，绝不是历史实盘表现，不能用于投资决策。")
    elif not result.validation.get("credible_for_strategy_conclusion", False):
        st.warning("真实数据回测已完成，但数据可靠性门禁未通过；请先处理证券主表、退市、拆股或缺失数据问题。")
    else:
        st.success("数据可靠性门禁已通过；仍需结合样本外和模拟盘验证。")


def metric_card(label: str, value: Any, kind: str = "number") -> None:
    if value is None:
        rendered = "N/A"
    elif kind == "money":
        rendered = f"${float(value):,.2f}"
    elif kind == "percent":
        rendered = f"{float(value):.2%}"
    elif kind == "integer":
        rendered = f"{int(value):,}"
    else:
        rendered = f"{float(value):.2f}" if isinstance(value, float) else str(value)
    st.metric(label, rendered)


def plot_config() -> dict[str, Any]:
    return {
        "displaylogo": False,
        "scrollZoom": True,
        "toImageButtonOptions": {"format": "png", "filename": "intraday_top20_chart", "scale": 2},
    }


def csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8-sig")


def excel_bytes(frame: pd.DataFrame) -> bytes:
    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="trades")
    return stream.getvalue()


def test_summary() -> dict[str, Any]:
    path = ROOT / "intraday_top20" / "outputs" / "validation" / "test_summary.json"
    if not path.exists():
        return {"passed": False, "status": "尚未在本机完成自动化测试"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"passed": False, "status": "测试摘要无法读取"}
