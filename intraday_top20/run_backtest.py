from __future__ import annotations

import argparse
from pathlib import Path

from intraday_top20.backtest.config import load_config
from intraday_top20.backtest.engine import IntradayTopGainersBacktester
from intraday_top20.backtest.robustness import run_robustness
from intraday_top20.data.cache import ResultStore
from intraday_top20.data.loader import MarketDataLoader
from intraday_top20.data.sample_data import generate_example_data

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config" / "default_config.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the intraday dynamic top-gainers backtest")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--robustness", action="store_true")
    parser.add_argument("--generate-example", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.generate_example:
        print(generate_example_data(config.data.data_dir, seed=config.random_seed))
    loader = MarketDataLoader(config)
    store = ResultStore(config.output.output_root)
    run_id = config.config_hash(loader.fingerprint())
    if store.has(run_id) and not args.force:
        result = store.load(run_id)
        print(f"loaded cached result {run_id}")
    else:
        result = IntradayTopGainersBacktester(config, loader).run(
            lambda stage, current, total, message: print(f"[{stage}] {current}/{total} {message}")
        )
        store.save(result)
        print(f"saved result {result.run_id}")
    print(result.metrics)
    print(result.validation)
    if args.robustness:
        frame = run_robustness(
            config, progress=lambda current, total, name: print(f"[robustness] {current}/{total} {name}")
        )
        target = Path(config.output.output_root) / result.run_id / "robustness.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(target, index=False)
        print(f"saved {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
