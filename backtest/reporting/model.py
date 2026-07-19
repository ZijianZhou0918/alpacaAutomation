from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class ReportBadge:
    """A compact provenance or configuration fact shown in the report header."""

    label: str
    value: str


@dataclass(frozen=True)
class ReportSection:
    """One independently ordered report section.

    ``content_html`` is trusted HTML produced by the local backtest adapter. The
    renderer escapes all reader-facing metadata around it.
    """

    section_id: str
    index_label: str
    nav_label: str
    title: str
    note: str
    content_html: str
    section_class: str = ""


@dataclass(frozen=True)
class InteractiveReportDocument:
    """Renderer-neutral document consumed by the standalone HTML renderer.

    Backtest engines adapt their own result objects into this model. This keeps
    strategy and execution code out of the UI layer while allowing other
    backtest implementations to reuse the same report shell.
    """

    title: str
    eyebrow: str
    lede: str
    badges: tuple[ReportBadge, ...]
    data_gate_title: str
    data_gate_body: str
    sections: tuple[ReportSection, ...]
    datasets: Mapping[str, object] = field(default_factory=dict)
    language: str = "zh-CN"
    plotly_url: str = "https://cdn.plot.ly/plotly-2.35.2.min.js"
    report_version: str = "2"
