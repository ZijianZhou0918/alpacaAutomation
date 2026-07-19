"""Reusable interactive report rendering for historical backtests."""

from .model import InteractiveReportDocument, ReportBadge, ReportSection
from .renderer import json_for_html, render_interactive_report, write_interactive_report

__all__ = [
    "InteractiveReportDocument",
    "ReportBadge",
    "ReportSection",
    "json_for_html",
    "render_interactive_report",
    "write_interactive_report",
]
