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


if __name__ == "__main__":
    unittest.main()
