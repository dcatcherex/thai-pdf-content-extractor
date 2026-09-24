"""
Tests for the audit fixes (truncation/empty-output handling, chunks.jsonl
placeholder exclusion, compare.py prompt sourcing, image-size fallback,
multi-batch splitting/resume).

Run from the tool's folder root:
    python3 -m unittest discover -s tests

No network, no provider SDKs required: providers.BACKENDS is monkeypatched
with stub functions, and batch_extractor is exercised against a fake
Anthropic-batches client (plain objects/dicts, no `anthropic` import).
"""
from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Make the tool's modules importable regardless of cwd.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fitz  # PyMuPDF

import common
import providers
import extractor
import compare
import batch_extractor


def make_pdf(path: Path, n_pages=3, noise_page=None):
    """Build a tiny PDF with some text pages; optionally make one page a
    large random-noise image (to force the JPEG size fallback)."""
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page(width=200, height=200)
        page.insert_text((20, 100), f"page {i}")
        if noise_page is not None and i == noise_page:
            import random
            random.seed(0)
            w, h = 400, 400
            buf = bytes(random.getrandbits(8) for _ in range(w * h * 3))
            pix = fitz.Pixmap(fitz.csRGB, w, h, buf, False)
            page.insert_image(fitz.Rect(0, 0, 200, 200), pixmap=pix)
    doc.save(str(path))
    doc.close()


class CheckOutputTests(unittest.TestCase):
    def test_truncated_raises(self):
        with self.assertRaises(providers.TruncatedOutput):
            providers.check_output("some text", truncated=True)

    def test_empty_raises(self):
        with self.assertRaises(providers.EmptyOutput):
            providers.check_output("", truncated=False)
        with self.assertRaises(providers.EmptyOutput):
            providers.check_output("   \n\t ", truncated=False)
        with self.assertRaises(providers.EmptyOutput):
            providers.check_output(None, truncated=False)

    def test_success_passthrough(self):
        self.assertEqual(providers.check_output("hello", truncated=False), "hello")

    def test_media_type_sniff(self):
        self.assertEqual(providers.media_type_of("iVBORw0KGgo..."), "image/png")
        self.assertEqual(providers.media_type_of("/9j/4AAQSkZJRg..."), "image/jpeg")
        self.assertEqual(providers.media_type_of("something-else"), "image/png")


class SyncExtractorRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = Path(self.tmp.name)
        self.pdf_path = self.out_dir / "doc.pdf"
        make_pdf(self.pdf_path, n_pages=2)
        self.doc = fitz.open(self.pdf_path)
        self.fig_dir = self.out_dir / "figures"
        self.fig_dir.mkdir()
        self.con = extractor.init_db(self.out_dir / "state.db", len(self.doc), "hash")
        self._sleep_patch = mock.patch.object(extractor.time, "sleep", lambda s: None)
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    def test_truncated_backend_leaves_page_failed_and_pending(self):
        calls = {"n": 0}

        def stub(img_b64, prompt, max_tokens):
            calls["n"] += 1
            raise providers.TruncatedOutput("truncated at max_tokens")

        with mock.patch.dict(providers.BACKENDS, {"anthropic": stub}):
            ok = extractor.extract_page_with_retry(
                self.con, self.doc, 0, 72, self.fig_dir, "anthropic")

        self.assertFalse(ok)
        row = self.con.execute(
            "SELECT status, error FROM pages WHERE page_no=0").fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIn("truncated", row[1])
        self.assertIn(0, extractor.pending_pages(self.con))
        self.assertEqual(calls["n"], 1)  # not re-billed by in-run retries

    def test_successful_backend_marks_done(self):
        def stub(img_b64, prompt, max_tokens):
            return "# Page 0\nsome content"

        with mock.patch.dict(providers.BACKENDS, {"anthropic": stub}):
            ok = extractor.extract_page_with_retry(
                self.con, self.doc, 0, 72, self.fig_dir, "anthropic")

        self.assertTrue(ok)
        row = self.con.execute(
            "SELECT status, markdown FROM pages WHERE page_no=0").fetchone()
        self.assertEqual(row[0], "done")
        self.assertIn("some content", row[1])
        self.assertNotIn(0, extractor.pending_pages(self.con))


class AssembleFromPagesTests(unittest.TestCase):
    def test_missing_pages_excluded_from_chunks_but_kept_as_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            pages = {0: {"markdown": "# hello", "n_figures": 0}}
            common.assemble_from_pages(pages, out_dir, n_pages=2, doc_meta={})

            chunks = [json.loads(l) for l in
                      (out_dir / "chunks.jsonl").read_text().splitlines()]
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0]["pdf_index"], 0)

            doc_json = json.loads((out_dir / "document.json").read_text())
            self.assertEqual(len(doc_json["pages"]), 2)
            p0, p1 = doc_json["pages"]
            self.assertTrue(p0["extracted"])
            self.assertFalse(p1["extracted"])
            self.assertIsNone(p1["markdown"])

            md = (out_dir / "document.md").read_text()
            self.assertIn("*[page 1: not extracted]*", md)


class ComparePromptTests(unittest.TestCase):
    def test_compare_has_no_local_prompt_and_uses_common(self):
        self.assertFalse(hasattr(compare, "PROMPT"))

    def test_run_one_uses_common_prompt(self):
        seen = {}

        def stub(img_b64, prompt, max_tokens):
            seen["prompt"] = prompt
            return "text"

        with mock.patch.dict(providers.BACKENDS, {"anthropic": stub}):
            text, secs, err = compare.run_one("anthropic", "b64data")
        self.assertIsNone(err)
        self.assertEqual(seen["prompt"], common.PROMPT)

    def test_run_one_reports_truncated_as_error_not_retry(self):
        calls = {"n": 0}

        def stub(img_b64, prompt, max_tokens):
            calls["n"] += 1
            raise providers.TruncatedOutput("truncated at max_tokens")

        with mock.patch.dict(providers.BACKENDS, {"anthropic": stub}):
            text, secs, err = compare.run_one("anthropic", "b64data", retries=2, backoff=0)
        self.assertIsNone(text)
        self.assertIn("TruncatedOutput", err)
        self.assertEqual(calls["n"], 1)  # not retried


class RenderImageFallbackTests(unittest.TestCase):
    def test_falls_back_to_jpeg_when_over_limit(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "doc.pdf"
            make_pdf(pdf_path, n_pages=1, noise_page=0)
            doc = fitz.open(pdf_path)
            with mock.patch.object(common, "MAX_IMAGE_B64_BYTES", 500):
                b64 = common.render_page_b64(doc, 0, dpi=150)
            self.assertEqual(providers.media_type_of(b64), "image/jpeg")

    def test_stays_png_under_limit(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "doc.pdf"
            make_pdf(pdf_path, n_pages=1)
            doc = fitz.open(pdf_path)
            b64 = common.render_page_b64(doc, 0, dpi=72)
            self.assertEqual(providers.media_type_of(b64), "image/png")

    def test_render_page_png_b64_is_alias(self):
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "doc.pdf"
            make_pdf(pdf_path, n_pages=1)
            doc = fitz.open(pdf_path)
            self.assertEqual(
                common.render_page_png_b64(doc, 0, 72),
                common.render_page_b64(doc, 0, 72))


class FakeBatch:
    def __init__(self, id_, requests):
        self.id = id_
        self.requests = requests
        self._results = []

    @property
    def processing_status(self):
        return "ended"

    @property
    def request_counts(self):
        n = len(self.requests)
        return SimpleNamespace(succeeded=n, errored=0, processing=0)


class FakeBatchesAPI:
    def __init__(self):
        self.batches = {}
        self.create_calls = 0
        self.results_by_id = {}

    def create(self, requests):
        self.create_calls += 1
        bid = f"batch_{len(self.batches)}"
        b = FakeBatch(bid, requests)
        self.batches[bid] = b
        return b

    def retrieve(self, batch_id):
        return self.batches[batch_id]

    def results(self, batch_id):
        return iter(self.results_by_id.get(batch_id, []))


class FakeClient:
    def __init__(self):
        self.messages = SimpleNamespace(batches=FakeBatchesAPI())


def make_result(pno, text="ok", stop_reason="end_turn", rtype="succeeded"):
    if rtype == "succeeded":
        message = SimpleNamespace(
            stop_reason=stop_reason,
            content=[SimpleNamespace(type="text", text=text)] if text is not None else [],
        )
        result = SimpleNamespace(type="succeeded", message=message)
    else:
        result = SimpleNamespace(type=rtype)
    return SimpleNamespace(custom_id=batch_extractor.cid(pno), result=result)


class BatchExtractorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = Path(self.tmp.name)
        self.pdf_path = self.out_dir / "doc.pdf"
        make_pdf(self.pdf_path, n_pages=6)
        self.doc = fitz.open(self.pdf_path)
        self.fig_dir = self.out_dir / "figures"
        self.fig_dir.mkdir()

    def test_submit_splits_into_multiple_batches(self):
        client = FakeClient()
        pages = list(range(6))
        state = {"batch_ids": [], "submitted_pages": [], "pages": pages,
                  "model": "m", "dpi": 72, "n_pages": 6,
                  "pdf_name": "doc.pdf", "pdf_size": self.pdf_path.stat().st_size,
                  "submitted_at": "now"}
        state_path = self.out_dir / "batch.json"

        with mock.patch.object(batch_extractor, "MAX_BATCH_BYTES", 1000):
            state = batch_extractor.submit_batch(
                client, self.doc, pages, 72, "m", state, state_path)

        self.assertGreater(len(state["batch_ids"]), 1)
        self.assertEqual(sorted(p for g in state["submitted_pages"] for p in g),
                          pages)
        saved = json.loads(state_path.read_text())
        self.assertEqual(saved["batch_ids"], state["batch_ids"])

    def test_rerun_same_args_does_not_resubmit(self):
        client = FakeClient()
        pages = list(range(6))
        state = {"batch_ids": [], "submitted_pages": [], "pages": pages,
                  "model": "m", "dpi": 72, "n_pages": 6,
                  "pdf_name": "doc.pdf", "pdf_size": self.pdf_path.stat().st_size,
                  "submitted_at": "now"}
        state_path = self.out_dir / "batch.json"
        with mock.patch.object(batch_extractor, "MAX_BATCH_BYTES", 1000):
            state = batch_extractor.submit_batch(
                client, self.doc, pages, 72, "m", state, state_path)
        calls_after_first = client.messages.batches.create_calls
        self.assertGreater(calls_after_first, 0)

        with mock.patch.object(batch_extractor, "MAX_BATCH_BYTES", 1000):
            state2 = batch_extractor.submit_batch(
                client, self.doc, pages, 72, "m", state, state_path)
        self.assertEqual(client.messages.batches.create_calls, calls_after_first)
        self.assertEqual(state2["batch_ids"], state["batch_ids"])

    def test_resume_matches_rejects_different_dpi(self):
        state = {"pages": [0, 1, 2], "dpi": 72, "model": "m",
                  "pdf_name": "doc.pdf", "pdf_size": self.pdf_path.stat().st_size}
        ok = batch_extractor._check_resume_matches(
            state, [0, 1, 2], dpi=150, model="m", pdf_path=self.pdf_path)
        self.assertFalse(ok)

    def test_resume_matches_accepts_identical_params(self):
        state = {"pages": [0, 1, 2], "dpi": 72, "model": "m",
                  "pdf_name": "doc.pdf", "pdf_size": self.pdf_path.stat().st_size}
        ok = batch_extractor._check_resume_matches(
            state, [0, 1, 2], dpi=72, model="m", pdf_path=self.pdf_path)
        self.assertTrue(ok)

    def test_old_single_batch_id_format_resumes(self):
        old_state = {"batch_id": "msgbatch_old", "model": "m", "dpi": 72,
                     "pages": [0, 1, 2], "n_pages": 6,
                     "submitted_at": "then"}
        state_path = self.out_dir / "batch.json"
        state_path.write_text(json.dumps(old_state))
        loaded = batch_extractor._load_state(state_path)
        self.assertEqual(loaded["batch_ids"], ["msgbatch_old"])
        self.assertEqual(loaded["submitted_pages"], [[0, 1, 2]])

    def test_collect_results_out_of_order_maps_correctly_and_splits_truncated_empty(self):
        client = FakeClient()
        client.messages.batches.batches["b0"] = FakeBatch("b0", [])
        client.messages.batches.results_by_id["b0"] = [
            make_result(2, text="page two content"),
            make_result(0, text="page zero content"),
            make_result(1, text=None, stop_reason="max_tokens"),
            make_result(3, text=""),
            make_result(4, rtype="expired"),
            make_result(5, rtype="errored"),
        ]

        pages, errored, expired, truncated, empty = batch_extractor.collect_results(
            client, ["b0"], self.doc, self.fig_dir)

        self.assertEqual(pages[2]["markdown"], "page two content")
        self.assertEqual(pages[0]["markdown"], "page zero content")
        self.assertNotIn(1, pages)
        self.assertIn(1, truncated)
        self.assertNotIn(3, pages)
        self.assertIn(3, empty)
        self.assertEqual(expired, [4])
        self.assertEqual(errored, [5])


class PrintedPageTests(unittest.TestCase):
    def test_offset_math(self):
        self.assertEqual(common.printed_page(24, 10), 14)
        self.assertIsNone(common.printed_page(5, 10))


if __name__ == "__main__":
    unittest.main()
