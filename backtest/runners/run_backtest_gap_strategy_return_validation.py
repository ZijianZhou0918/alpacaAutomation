"""Write saved-evidence artifacts for the total-return gap study."""

from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from backtest.gap_strategy_return_report import (
    write_return_validation_artifacts,
)


def main() -> None:
    json_path, markdown_path, notebook_path = (
        write_return_validation_artifacts()
    )
    print(f"Return validation JSON: {json_path}")
    print(f"Return validation report: {markdown_path}")
    print(f"Executed notebook: {notebook_path}")


if __name__ == "__main__":
    main()
