import contextlib
import csv
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jtl_fail_report as report


NESTED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testResults>
  <sample lb="position workflow" s="false"
          rm="Number of samples in transaction : 1, number of failing samples : 1">
    <httpSample lb="get_position" s="false" rc="500" rm="Server error">
      <java.net.URL>https://example.invalid/crm20/get_position</java.net.URL>
      <sample lb="get_position-0" s="false" rm="FAIL"/>
    </httpSample>
  </sample>
  <sample lb="prepare data" s="true" rc="200"/>
</testResults>
"""

PLAN_XML = """<jmeterTestPlan><hashTree>
  <TestPlan testname="test plan"/><hashTree>
    <ThreadGroup testname="disabled" enabled="false"/><hashTree>
      <TransactionController testname="hidden"/><hashTree/>
    </hashTree>
    <ThreadGroup testname="active"/><hashTree>
      <TransactionController testname="position workflow"/><hashTree>
        <TransactionController testname="disabled child" enabled="false"/><hashTree/>
      </hashTree>
      <TransactionController testname="second workflow"/><hashTree/>
      <TransactionController testname="second workflow"/><hashTree/>
    </hashTree>
  </hashTree>
</hashTree></jmeterTestPlan>"""


class ReportStatisticsTests(unittest.TestCase):
    def summary(self, rows):
        return report.summarize(report.extract_failures(rows), rows)

    def test_many_samples_are_not_one_transaction(self):
        rows = [{"label": "request", "success": "true"} for _ in range(100)]
        rows.extend([
            {"label": "get_history_position", "success": "false", "responseCode": "500"},
            {
                "label": "workflow",
                "success": "true",
                "responseMessage": "Number of samples in transaction : 2, number of failing samples : 0",
            },
        ])
        summary = self.summary(rows)
        self.assertEqual(summary["sample_total"], 102)
        self.assertEqual(summary["sample_failed"], 1)
        self.assertEqual(summary["sample_success"], 101)
        self.assertEqual(summary["transaction_total"], 1)
        self.assertEqual(summary["transaction_failed"], 0)
        self.assertEqual(report.rate_text(summary["sample_success"], summary["sample_total"]), "99.02%")

    def test_xml_subresults_are_not_counted_twice(self):
        rows = report.parse_xml_jtl(NESTED_XML)
        self.assertEqual([row["_sample_depth"] for row in rows], [0, 1, 2, 0])
        summary = self.summary(rows)
        self.assertEqual(summary["total_rows"], 4)
        self.assertEqual(summary["sample_total"], 2)
        self.assertEqual(summary["sample_failed"], 1)
        self.assertEqual(summary["sample_success"], 1)
        self.assertEqual(summary["transaction_total"], 1)
        self.assertEqual(summary["transaction_failed"], 1)
        self.assertEqual(summary["details"], 1)

    def test_nested_transactions_remain_separate_from_primary_samples(self):
        xml = """<testResults>
          <sample lb="outer" s="true" rm="Number of samples in transaction : 1">
            <sample lb="inner" s="true" rm="Number of samples in transaction : 1">
              <httpSample lb="request" s="true"/>
            </sample>
          </sample>
        </testResults>"""
        summary = self.summary(report.parse_xml_jtl(xml))
        self.assertEqual(summary["sample_total"], 1)
        self.assertEqual(summary["transaction_total"], 2)

    def test_http_sampler_with_transaction_in_name_keeps_failure_details(self):
        xml = '<testResults><httpSample lb="query\u4e8b\u52a1" s="false" rc="500"/></testResults>'
        summary = self.summary(report.parse_xml_jtl(xml))
        self.assertEqual(summary["sample_total"], 1)
        self.assertEqual(summary["transaction_total"], 0)
        self.assertEqual(summary["details"], 1)
        self.assertFalse(report.is_transaction_row({
            "label": "query\u4e8b\u52a1", "URL": "https://example.invalid/crm20/query",
        }))

    def test_ambiguous_csv_name_requires_jmx_confirmation(self):
        rows = [{"label": "\u4e8b\u52a1", "success": "false"}]
        summary = self.summary(rows)
        self.assertEqual(summary["sample_total"], 1)
        self.assertEqual(summary["transaction_total"], 0)
        self.assertEqual(summary["details"], 1)
        report.apply_transaction_plan(rows, {"enabled_labels": ["\u4e8b\u52a1"]})
        summary = self.summary(rows)
        self.assertEqual(summary["transaction_failed"], 1)
        self.assertEqual(summary["details"], 0)

    def test_multiple_transactions_both_parent_modes_and_repeated_names(self):
        xml = """<testResults>
          <sample lb="workflow" s="true" rm="Number of samples in transaction : 1">
            <httpSample lb="child" s="true"/>
          </sample>
          <httpSample lb="standalone request" s="true"/>
          <sample lb="workflow" s="false" rm="Number of samples in transaction : 1"/>
          <sample lb="other workflow" s="true" rm="number of samples in transaction : 1"/>
        </testResults>"""
        summary = self.summary(report.parse_xml_jtl(xml))
        self.assertEqual(summary["transaction_total"], 3)
        self.assertEqual(summary["transaction_failed"], 1)
        self.assertEqual(summary["transaction_success"], 2)
        self.assertEqual(summary["transaction_groups"], [
            {"label": "workflow", "total": 2, "failed": 1},
            {"label": "other workflow", "total": 1, "failed": 0},
        ])
        self.assertEqual(report.rate_text(2, 3), "66.67%")

    def test_jmx_respects_disabled_ancestors_and_keeps_duplicate_definitions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.jmx"
            path.write_text(PLAN_XML, encoding="utf-8")
            plan = report.read_transaction_plan(path)
        self.assertEqual(plan["controller_total"], 5)
        self.assertEqual(plan["enabled_labels"], ["position workflow", "second workflow", "second workflow"])

    def test_jmx_fallback_does_not_count_http_names_or_unexecuted_definitions(self):
        xml = """<testResults>
          <sample lb="workflow" s="true"/>
          <sample lb="workflow" s="false"/>
          <httpSample lb="workflow" s="true"/>
        </testResults>"""
        rows = report.parse_xml_jtl(xml)
        self.assertEqual(self.summary(rows)["transaction_total"], 0)
        report.apply_transaction_plan(rows, {"enabled_labels": ["workflow", "not executed"]})
        summary = self.summary(rows)
        self.assertEqual(summary["transaction_total"], 2)
        self.assertEqual(summary["transaction_failed"], 1)

    def test_xml_sample_name_alone_does_not_prove_it_is_a_transaction(self):
        rows = report.parse_xml_jtl('<testResults><sample lb="\u4e8b\u52a1" s="true"/></testResults>')
        self.assertEqual(self.summary(rows)["transaction_total"], 0)

    def test_no_transactions_still_counts_samples(self):
        summary = self.summary([{"label": "request", "success": "true"}])
        self.assertEqual(summary["sample_total"], 1)
        self.assertEqual(summary["transaction_total"], 0)
        self.assertTrue(summary["transaction_warning"])
        self.assertEqual(report.rate_text(summary["transaction_success"], summary["transaction_total"]), "N/A")

    def test_empty_input_has_no_division_by_zero(self):
        summary = self.summary([])
        self.assertEqual(summary["sample_total"], 0)
        self.assertEqual(report.rate_text(summary["sample_success"], summary["sample_total"]), "N/A")

    def test_csv_and_xml_file_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            xml_path = Path(directory) / "nested.jtl"
            xml_path.write_text(NESTED_XML, encoding="utf-8")
            self.assertEqual(self.summary(report.parse_jtl(xml_path))["sample_total"], 2)

            csv_path = Path(directory) / "flat.jtl"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["label", "success"])
                writer.writeheader()
                writer.writerows([
                    {"label": "repeated label", "success": "true"},
                    {"label": "repeated label", "success": "false"},
                ])
            summary = self.summary(report.parse_jtl(csv_path))
            self.assertEqual(summary["sample_total"], 2)
            self.assertEqual(summary["sample_failed"], 1)

    def test_html_and_teams_use_same_primary_statistics(self):
        rows = report.parse_xml_jtl(NESTED_XML)
        details = report.extract_failures(rows)
        html = report.generate_html(details, rows, "test.jtl")
        self.assertIn('<div class="num">1</div><div class="name">\u4e8b\u52a1\u603b\u6570</div>', html)
        self.assertIn("0.00%", html)
        self.assertIn("position workflow</td><td>1</td><td>1</td>", html)

        payload = report.build_teams_payload(
            details, rows, "test.jtl", "report.html", report_url="https://example.invalid/report.html",
        )
        body = payload["attachments"][0]["content"]["body"]
        facts = next(block["facts"] for block in body if block["type"] == "FactSet")
        values = {fact["title"]: fact["value"] for fact in facts}
        self.assertEqual(values["\u4e8b\u52a1\u603b\u6570"], "1")
        self.assertEqual(values["\u5931\u8d25\u4e8b\u52a1"], "1")
        self.assertEqual(values["\u4e8b\u52a1\u6210\u529f\u7387"], "0.00% (0/1)")
        self.assertEqual([fact["title"] for fact in facts][1:4], [
            "\u4e8b\u52a1\u603b\u6570", "\u5931\u8d25\u4e8b\u52a1", "\u4e8b\u52a1\u6210\u529f\u7387",
        ])
        self.assertTrue(any("position workflow" in block.get("text", "") for block in body))

    def test_zero_transactions_have_warning_and_na_rate_in_teams(self):
        payload = report.build_teams_payload([], [], "empty.jtl", "report.html")
        body = payload["attachments"][0]["content"]["body"]
        facts = next(block["facts"] for block in body if block["type"] == "FactSet")
        values = {fact["title"]: fact["value"] for fact in facts}
        self.assertEqual(values["\u4e8b\u52a1\u6210\u529f\u7387"], "N/A (0/0)")
        self.assertTrue(any(block.get("color") == "Attention" for block in body))

    def test_console_uses_same_statistics_without_sending_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            xml_path = Path(directory) / "nested.jtl"
            html_path = Path(directory) / "report.html"
            config_path = Path(directory) / "config.json"
            jmx_path = Path(directory) / "test.jmx"
            xml_path.write_text(NESTED_XML, encoding="utf-8")
            config_path.write_text("{}", encoding="utf-8")
            jmx_path.write_text(PLAN_XML, encoding="utf-8")
            args = ["jtl_fail_report.py", str(xml_path), "-o", str(html_path),
                    "--config", str(config_path), "--jmx", str(jmx_path), "--no-open"]
            output = io.StringIO()
            with mock.patch("sys.argv", args), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(report, "push_teams_report") as push_card, \
                    mock.patch.object(report, "push_teams_file_report") as push_file, \
                    contextlib.redirect_stdout(output):
                push_card.return_value = None
                push_file.return_value = None
                report.main()
            self.assertTrue(html_path.is_file())
            self.assertIn("\u4e8b\u52a1\u603b\u6570: 1", output.getvalue())
            self.assertIn("0.00% (0/1)", output.getvalue())
            self.assertIn(str(jmx_path), output.getvalue())
            self.assertIn("second workflow", output.getvalue())


if __name__ == "__main__":
    unittest.main()
