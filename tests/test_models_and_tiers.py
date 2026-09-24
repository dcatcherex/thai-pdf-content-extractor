"""Tests for the 2026-09-24 model/pricing/tier update. No network, no SDKs."""
from __future__ import annotations
import os, sys, tempfile, threading, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402
import common, providers, extractor, compare, estimate_cost  # noqa: E402


class TestGeminiThinking(unittest.TestCase):
    def test_levels(self):
        f = providers.gemini_thinking_config
        self.assertEqual(f("gemini-2.5-flash", None), {"thinking_budget": 0})
        self.assertEqual(f("gemini-2.5-flash-lite", None), {"thinking_budget": 0})
        self.assertEqual(f("gemini-3.5-flash-lite", None), {"thinking_level": "minimal"})
        self.assertEqual(f("gemini-3.5-flash", None), {"thinking_level": "minimal"})
        self.assertEqual(f("gemini-3.8-flash", None), {"thinking_level": "low"})
        self.assertEqual(f("gemini-3.1-pro-preview", None), {"thinking_level": "low"})
        self.assertEqual(f("gemini-3.8-flash", "high"), {"thinking_level": "high"})
        self.assertIsNone(f("gemini-2.5-pro", None))


class TestPricingTable(unittest.TestCase):
    def test_defaults_are_priced(self):
        for prov, model in providers.DEFAULT_MODELS.items():
            self.assertIn(model, providers.PRICING[prov])
            self.assertIn(prov, providers.BATCH_MULTIPLIER)


class TestImageTokens(unittest.TestCase):
    def test_claude_tiers(self):
        it = estimate_cost.image_tokens
        # 150 DPI A4-ish page: high-res tier reads it natively
        self.assertEqual(it("claude-sonnet-5", 1252, 1778), 45 * 64)
        # standard tier is capped at 1568 tokens
        self.assertLessEqual(it("claude-sonnet-4-6", 1252, 1778), 1568)
        # high-res tier is capped at 4784 tokens
        self.assertLessEqual(it("claude-sonnet-5", 3000, 4000), 4784)

    def test_gemini(self):
        self.assertEqual(estimate_cost.image_tokens("gemini-3.5-flash-lite", 1252, 1778), 1120)
        self.assertEqual(estimate_cost.image_tokens("gemini-2.5-flash", 1252, 1778), 2 * 3 * 258)


class TestCompareEntries(unittest.TestCase):
    def test_split_entry(self):
        self.assertEqual(compare.split_entry("gemini:gemini-2.5-flash"),
                         ("gemini", "gemini-2.5-flash"))
        self.assertEqual(compare.split_entry("anthropic"),
                         ("anthropic", providers.DEFAULT_MODELS["anthropic"]))

    def test_run_one_uses_entry_model_and_restores_env(self):
        seen = []

        def stub(img, prompt, mt):
            seen.append(os.environ.get("EXTRACTOR_MODEL"))
            return "ok"
        with mock.patch.dict(providers.BACKENDS, {"gemini": stub}), \
             mock.patch.dict(os.environ, {"EXTRACTOR_MODEL": "outer"}):
            compare.run_one("gemini:gemini-2.5-flash", "iVBORxx")
            self.assertEqual(os.environ["EXTRACTOR_MODEL"], "outer")
        self.assertEqual(seen, ["gemini-2.5-flash"])


class TestParallelWorkers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        doc = fitz.open()
        for i in range(6):
            doc.new_page().insert_text((72, 72), f"page {i}")
        pdf = self.tmp / "t.pdf"
        doc.save(pdf)
        self.doc = fitz.open(pdf)
        self.con = extractor.init_db(self.tmp / "state.db", 6, "h")
        self.fig = self.tmp / "figures"; self.fig.mkdir()

    def test_parallel_marks_done_and_isolates_failures(self):
        main_thread = threading.get_ident()
        db_threads = set()
        orig_record = extractor.record_result

        def spy(*a, **k):
            db_threads.add(threading.get_ident())
            return orig_record(*a, **k)

        calls = {"n": 0}
        lock = threading.Lock()

        def stub2(img, prompt, mt):
            with lock:
                calls["n"] += 1
                n = calls["n"]
            if n == 1:
                raise providers.EmptyOutput("empty output")
            return "# body"
        with mock.patch.dict(providers.BACKENDS, {"anthropic": stub2}), \
             mock.patch.object(extractor, "record_result", spy):
            extractor.run_parallel(self.con, self.doc, list(range(6)), 50,
                                   self.fig, "anthropic", workers=3)
        rows = dict(self.con.execute("SELECT page_no, status FROM pages"))
        self.assertEqual(sorted(rows.values()).count("done"), 5)
        self.assertEqual(sorted(rows.values()).count("failed"), 1)
        self.assertEqual(db_threads, {main_thread})  # DB writes on main thread only


class TestFlexFlag(unittest.TestCase):
    def test_flex_rejected_for_anthropic(self):
        with mock.patch.object(sys, "argv", ["extractor.py", "x.pdf",
                                             "--service-tier", "flex"]):
            with self.assertRaises(SystemExit):
                extractor.main()


class TestComparePrinted(unittest.TestCase):
    def _args(self, **kw):
        from types import SimpleNamespace
        d = dict(pages="", printed="", page_offset=None, start=0, count=3)
        d.update(kw)
        return SimpleNamespace(**d)

    def test_printed_converts_with_offset(self):
        a = self._args(printed="96,120-121", page_offset=9)
        self.assertEqual(compare.parse_pages(a, 364), [105, 129, 130])

    def test_printed_requires_offset(self):
        with self.assertRaises(SystemExit):
            compare.parse_pages(self._args(printed="96"), 364)

    def test_pages_ranges_and_bounds(self):
        self.assertEqual(compare.parse_pages(self._args(pages="2,105,180-182"), 364),
                         [2, 105, 180, 181, 182])
        with self.assertRaises(SystemExit):
            compare.parse_pages(self._args(pages="364"), 364)

    def test_label(self):
        self.assertEqual(compare.page_label(105, 9),
                         "printed 96 · pdf_index 105 (viewer 106)")
        self.assertEqual(compare.page_label(3, 9), "pdf_index 3 (viewer 4)")

    def test_report_end_to_end(self):
        tmp = Path(tempfile.mkdtemp())
        doc = fitz.open()
        for i in range(12):
            doc.new_page().insert_text((72, 72), f"p{i}")
        pdf = tmp / "t.pdf"; doc.save(pdf)
        stub = lambda img, prompt, mt: "# body"
        argv = ["compare.py", str(pdf), "--printed", "1", "--page-offset", "9",
                "--providers", "anthropic", "--dpi", "40", "--out", str(tmp / "c")]
        with mock.patch.dict(providers.BACKENDS, {"anthropic": stub}), \
             mock.patch.object(sys, "argv", argv):
            compare.main()
        run = next((tmp / "c").iterdir())
        self.assertIn("printed 1 · pdf_index 10", (run / "report.html").read_text("utf-8"))
        self.assertTrue((run / "page_0010.png").exists())


class TestExtractorsPrinted(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        doc = fitz.open()
        for i in range(12):
            doc.new_page().insert_text((72, 72), f"p{i}")
        self.pdf = self.tmp / "t.pdf"; doc.save(self.pdf)

    def test_resolve_pages(self):
        self.assertEqual(common.resolve_pages("", "1-2", 9, 12), [10, 11])
        self.assertEqual(common.resolve_pages("2,5-6", "", None, 12), [2, 5, 6])
        self.assertEqual(common.resolve_pages("", "", None, 3), [0, 1, 2])
        with self.assertRaises(SystemExit):
            common.resolve_pages("", "3", 9, 12)      # printed 3 -> 12, out of range
        with self.assertRaises(SystemExit):
            common.resolve_pages("", "1", None, 12)   # offset missing

    def test_sync_extractor_only_runs_printed_pages(self):
        seen = []

        def stub(img, prompt, mt):
            seen.append(1)
            return "# body"
        out = self.tmp / "out"
        argv = ["extractor.py", str(self.pdf), "--out", str(out), "--dpi", "40",
                "--printed", "1-2", "--page-offset", "9"]
        with mock.patch.dict(providers.BACKENDS, {"anthropic": stub}), \
             mock.patch.object(sys, "argv", argv):
            extractor.main()
        con = extractor.init_db(out / "state.db", 12, "x")
        done = [r[0] for r in con.execute(
            "SELECT page_no FROM pages WHERE status='done' ORDER BY page_no")]
        self.assertEqual(done, [10, 11])
        self.assertEqual(len(seen), 2)

    def test_batch_extractor_submits_printed_pages(self):
        import types, json
        import batch_extractor
        from test_fixes import FakeClient
        client = FakeClient()
        fake = types.ModuleType("anthropic")
        fake.Anthropic = lambda *a, **k: client
        out = self.tmp / "ob"
        argv = ["batch_extractor.py", str(self.pdf), "--out", str(out), "--dpi", "40",
                "--printed", "1", "--page-offset", "9", "--no-wait"]
        with mock.patch.dict(sys.modules, {"anthropic": fake}), \
             mock.patch.object(sys, "argv", argv):
            batch_extractor.main()
        state = json.loads((out / "batch.json").read_text())
        self.assertEqual(state["pages"], [10])
        self.assertEqual(state["submitted_pages"], [[10]])


if __name__ == "__main__":
    unittest.main()
