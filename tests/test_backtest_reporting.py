from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.reporting import (
    InteractiveReportDocument,
    ReportBadge,
    ReportSection,
    json_for_html,
    render_interactive_report,
    write_interactive_report,
)


class InteractiveBacktestReportTests(unittest.TestCase):
    def make_document(self) -> InteractiveReportDocument:
        return InteractiveReportDocument(
            title="Reusable <strategy>",
            eyebrow="RESEARCH / 2026",
            lede="A renderer-neutral report.",
            badges=(ReportBadge("Source", "SQLite / read-only"),),
            data_gate_title="DATA GATE",
            data_gate_body="No future data.",
            sections=(
                ReportSection(
                    section_id="outcome",
                    index_label="01 / OUTCOME",
                    nav_label="Outcome",
                    title="Summary",
                    note="Reader-facing note.",
                    content_html="<div id='custom-section'>Adapter content</div>",
                ),
            ),
            datasets={
                "equity": [{"timestamp": "2026-01-02", "equity": 100_100, "cash": 95_000}],
                "details": {},
            },
        )

    def test_renderer_is_independent_and_embeds_assets_and_data(self):
        content = render_interactive_report(self.make_document())

        self.assertIn('data-report-version="2"', content)
        self.assertIn("Reusable &lt;strategy&gt;", content)
        self.assertNotIn("<title>Reusable <strategy>", content)
        self.assertIn("<div id='custom-section'>Adapter content</div>", content)
        self.assertIn("--font-mono", content)
        self.assertIn("bindSymbolFilters", content)
        self.assertIn("TIMEFRAME / DAY DRILLDOWN", content)
        self.assertIn("loadMinuteDetail", content)
        self.assertIn("plotly_click", content)
        self.assertIn("resetPlotlyChart", content)
        self.assertIn("window.Plotly.newPlot(chart, traces", content)
        self.assertIn("return renderMinuteChart(day, payload, windowData)", content)
        self.assertIn("分钟图表渲染失败", content)
        self.assertNotIn("window.Plotly.react(chart, traces", content)
        self.assertIn('name: `${activeSymbol} 1Min Close`', content)
        self.assertIn("1 分钟收盘价折线", content)
        self.assertIn("个真实分钟点，按时间连续连接", content)
        self.assertIn('mode: "lines"', content)
        self.assertIn("connectgaps: true", content)
        self.assertIn('shape: "linear"', content)
        self.assertIn("String(left.timestamp).localeCompare(String(right.timestamp))", content)
        self.assertIn('"equity":[{"timestamp":"2026-01-02"', content)
        self.assertNotIn("__REPORT_", content)

    def test_renderer_writes_a_standalone_html_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "report.html"

            written = write_interactive_report(self.make_document(), path)

            self.assertEqual(written, path)
            self.assertTrue(path.exists())
            self.assertIn("Backtest research", path.read_text(encoding="utf-8"))

    def test_json_for_html_neutralizes_script_end_sequences(self):
        encoded = json_for_html({"value": "</script><script>alert(1)</script>"})

        self.assertNotIn("</script>", encoded)
        self.assertEqual(json.loads(encoded), {"value": "</script><script>alert(1)</script>"})


if __name__ == "__main__":
    unittest.main()
