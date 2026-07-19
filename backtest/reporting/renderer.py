from __future__ import annotations

import html
import json
from pathlib import Path

from .model import InteractiveReportDocument


_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATE_PATH = _PACKAGE_DIR / "templates" / "interactive_report.html"
_STYLE_PATH = _PACKAGE_DIR / "assets" / "interactive_report.css"
_SCRIPT_PATH = _PACKAGE_DIR / "assets" / "interactive_report.js"


def json_for_html(value: object) -> str:
    """Serialize JSON safely for an inline script element."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render_interactive_report(document: InteractiveReportDocument) -> str:
    """Render a standalone interactive backtest report.

    The output embeds all report datasets, CSS and application JavaScript. The
    only remote runtime dependency is the configurable Plotly script URL.
    """

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    styles = _STYLE_PATH.read_text(encoding="utf-8")
    script = _SCRIPT_PATH.read_text(encoding="utf-8")

    badges_html = "".join(
        "<span class='chip'>"
        f"{html.escape(badge.label)} <strong>{html.escape(badge.value)}</strong>"
        "</span>"
        for badge in document.badges
    )
    nav_html = "".join(
        f"<a href='#{html.escape(section.section_id)}'>{html.escape(section.nav_label)}</a>"
        for section in document.sections
    )
    sections_html = "".join(_render_section(section) for section in document.sections)
    replacements = {
        "__LANGUAGE__": html.escape(document.language),
        "__REPORT_VERSION__": html.escape(document.report_version),
        "__TITLE__": html.escape(document.title),
        "__EYEBROW__": html.escape(document.eyebrow),
        "__LEDE__": html.escape(document.lede),
        "__BADGES__": badges_html,
        "__DATA_GATE_TITLE__": html.escape(document.data_gate_title),
        "__DATA_GATE_BODY__": html.escape(document.data_gate_body),
        "__REPORT_NAV__": nav_html,
        "__SECTIONS__": sections_html,
        "__PLOTLY_URL__": html.escape(document.plotly_url, quote=True),
        "__REPORT_DATA__": json_for_html(dict(document.datasets)),
        "__REPORT_STYLES__": styles,
        "__REPORT_SCRIPT__": script,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def write_interactive_report(document: InteractiveReportDocument, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_interactive_report(document), encoding="utf-8")
    return path


def _render_section(section: object) -> str:
    section_id = html.escape(str(getattr(section, "section_id")))
    section_class = html.escape(str(getattr(section, "section_class", "")))
    return (
        f"<section id='{section_id}' class='report-section {section_class}' data-report-section>"
        "<div class='section-head'>"
        "<div>"
        f"<span class='section-no'>{html.escape(str(getattr(section, 'index_label')))}</span>"
        f"<h2>{html.escape(str(getattr(section, 'title')))}</h2>"
        "</div>"
        f"<p class='note'>{html.escape(str(getattr(section, 'note')))}</p>"
        "</div>"
        f"{getattr(section, 'content_html')}"
        "</section>"
    )
