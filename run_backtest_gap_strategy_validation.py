from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from backtest.runners.run_backtest_gap_strategy_validation import main


if __name__ == "__main__":
    main()
