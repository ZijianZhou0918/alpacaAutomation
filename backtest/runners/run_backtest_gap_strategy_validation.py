"""Write the saved-evidence validation artifacts for the frozen gap strategy."""

from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from backtest.gap_strategy_validation_report import write_validation_artifacts


def main() -> None:
    json_path, markdown_path, notebook_path = write_validation_artifacts()
    print(f"Validation JSON: {json_path}")
    print(f"Validation report: {markdown_path}")
    print(f"Executed notebook: {notebook_path}")


if __name__ == "__main__":
    main()
