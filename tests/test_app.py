"""Web app + job pipeline tests. Stub backends; no network, no API keys."""
from __future__ import annotations
import os, sys, tempfile, time, unittest, zipfile, io
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402
import providers, jobs, offset_detect, quality  # noqa: E402

GOOD = "# หัวข้อ\n\nคู่มือผู้ดำเนินการดูแลผู้สูงอายุ ข้อความปกติที่ยาวพอสมควร\n"
BAD_TABLE = "| a | b |\n|---|---|\n| | |\n| - | |\n| | |\n" + GOOD


def stub(img, prompt, max_tokens, model=None):
    if prompt == offset_detect.NUMBER_PROMPT:
        return "NONE"
    return GOOD


def make_pdf(path, n=8):
    doc = fitz.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"page {i}")
    doc.save(path)
    return Path(path).read_bytes()


class JobPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p1 = mock.patch.object(jobs, "JOBS_DIR", self.tmp / "jobs")
        self.p2 = mock.patch.dict(providers.BACKENDS,
                                  {"anthropic": stub, "gemini": stub})
        self.p1.start(); self.p2.start()
        self.addCleanup(self.p1.stop); self.addCleanup(self.p2.stop)
        self.pdf = make_pdf(self.tmp / "t.pdf")

    def _wait(self, job_id, timeout=20):
        t0 = time.time()
        while jobs.is_running(job_id) and time.time() - t0 < timeout:
            time.sleep(0.05)

    def test_run_now_review_edit_redo_zip(self):
        job = jobs.create_job(self.pdf, "คู่มือ.pdf", title="T", publisher="P",
                              preset="standard", mode="now", pages=[2, 3, 4],
                              offset=1, dpi=40, estimate_thb=(1, 2))
        jobs.start(job["id"], workers=2)
        self._wait(job["id"])
        job = jobs.load_job(job["id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(jobs.progress(job), {"done": 3, "failed": 0, "pending": 0, "total": 3})
        self.assertEqual(jobs.flagged_pages(job), {})

        jobs.save_edit(job["id"], 3, BAD_TABLE)          # manual edit -> flagged
        job = jobs.load_job(job["id"])
        self.assertIn(3, jobs.flagged_pages(job))
        self.assertEqual(job["edited_pages"], [3])

        self.assertTrue(jobs.redo_page(job["id"], 3, "best"))   # redo clears it
        self.assertNotIn(3, jobs.flagged_pages(jobs.load_job(job["id"])))

        z = zipfile.ZipFile(io.BytesIO(jobs.output_zip(job)))
        self.assertIn("chunks.jsonl", z.namelist())
        chunks = z.read("chunks.jsonl").decode().strip().splitlines()
        self.assertEqual(len(chunks), 3)                  # only the selected pages
        self.assertIn('"printed_page": 2', chunks[1])      # pdf_index 3 - offset 1

    def test_failed_redo_keeps_text(self):
        job = jobs.create_job(self.pdf, "x.pdf", title="", publisher="",
                              preset="standard", mode="now", pages=[0],
                              offset=None, dpi=40, estimate_thb=None)
        jobs.start(job["id"], workers=1); self._wait(job["id"])

        def boom(*a, **k):
            raise providers.EmptyOutput("empty output")
        with mock.patch.dict(providers.BACKENDS, {"anthropic": boom}):
            self.assertFalse(jobs.redo_page(job["id"], 0, "best"))
        rows = jobs.page_rows(jobs.load_job(job["id"]))
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[0]["markdown"], GOOD)

    def test_stop_then_resume(self):
        def slow(img, prompt, mt, model=None):
            time.sleep(0.2); return GOOD
        with mock.patch.dict(providers.BACKENDS, {"anthropic": slow}):
            job = jobs.create_job(self.pdf, "x.pdf", title="", publisher="",
                                  preset="standard", mode="now", pages=list(range(8)),
                                  offset=None, dpi=40, estimate_thb=None)
            jobs.start(job["id"], workers=1); time.sleep(0.3); jobs.stop(job["id"])
            self._wait(job["id"])
            job = jobs.load_job(job["id"])
            self.assertEqual(job["status"], "stopped")
            self.assertGreater(jobs.progress(job)["pending"], 0)
            jobs.start(job["id"], workers=4); self._wait(job["id"])
        self.assertEqual(jobs.load_job(job["id"])["status"], "done")

    def test_estimate_and_batch_discount(self):
        doc = fitz.open(stream=self.pdf, filetype="pdf")
        s = dict(jobs.DEFAULT_SETTINGS)
        now = jobs.estimate_thb(doc, list(range(8)), 150, "standard", False, s)
        night = jobs.estimate_thb(doc, list(range(8)), 150, "standard", True, s)
        self.assertAlmostEqual(night[1], now[1] / 2, delta=0.02)

    def test_effective_status_after_restart(self):
        job = jobs.create_job(self.pdf, "x.pdf", title="", publisher="",
                              preset="standard", mode="now", pages=[0],
                              offset=None, dpi=40, estimate_thb=None)
        jobs.update_job(job["id"], status="running")     # thread never started
        self.assertEqual(jobs.effective_status(jobs.load_job(job["id"])), "stopped")


class OffsetDetect(unittest.TestCase):
    def test_majority_vote(self):
        doc = fitz.open()
        for i in range(40):
            doc.new_page()
        replies = iter(["96", "๑๐๒", "NONE", "หน้า 112", "118"])

        def reader(img, prompt, mt, model=None):
            return next(replies)
        pages = offset_detect.sample_pages(40, 5)        # [10, 12, 14, 16, 18]
        with mock.patch.dict(providers.BACKENDS, {"anthropic": reader}):
            r = offset_detect.detect_offset(doc, "anthropic")
        # readings: 10->96 (off -86), 12->102 (-90), 16->112 (-96), 18->118 (-100)
        self.assertEqual(len(r["readings"]), len(pages))
        self.assertEqual(offset_detect.parse_number("๑๐๒"), 102)
        self.assertFalse(r["confident"])                  # no majority -> ask a human

    def test_consistent(self):
        doc = fitz.open()
        for i in range(40):
            doc.new_page()

        def reader(img, prompt, mt, model=None):
            return reader.map.pop(0)
        pages = offset_detect.sample_pages(40, 5)
        reader.map = [str(p - 9) for p in pages]
        with mock.patch.dict(providers.BACKENDS, {"anthropic": reader}):
            r = offset_detect.detect_offset(doc, "anthropic")
        self.assertEqual(r["offset"], 9)
        self.assertTrue(r["confident"])


class AppScreens(unittest.TestCase):
    def test_job_detail_and_review_render(self):
        from streamlit.testing.v1 import AppTest
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(jobs, "JOBS_DIR", tmp / "jobs"), \
             mock.patch.dict(providers.BACKENDS, {"anthropic": stub}), \
             mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            pdf = make_pdf(tmp / "t.pdf")
            job = jobs.create_job(pdf, "t.pdf", title="เอกสารทดสอบ", publisher="",
                                  preset="standard", mode="now", pages=[0, 1],
                                  offset=None, dpi=40, estimate_thb=(1, 2))
            jobs.start(job["id"], workers=1)
            while jobs.is_running(job["id"]):
                time.sleep(0.05)
            jobs.save_edit(job["id"], 1, BAD_TABLE)
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
            at.session_state["goto"] = "งานทั้งหมด"
            at.run()
            self.assertEqual([e.value for e in at.exception], [])
            self.assertTrue(any("ควรตรวจ" in w.value for w in at.warning))
            save = [b for b in at.button if b.label == "บันทึกการแก้ไข"][0]
            at.text_area[0].set_value(GOOD)
            save.click().run()
            self.assertEqual([e.value for e in at.exception], [])
            self.assertEqual(jobs.flagged_pages(jobs.load_job(job["id"])), {})


class BatchJob(unittest.TestCase):
    def test_submit_then_check(self):
        from test_fixes import FakeClient, make_result
        tmp = Path(tempfile.mkdtemp())
        client = FakeClient()
        with mock.patch.object(jobs, "JOBS_DIR", tmp / "jobs"), \
             mock.patch.object(jobs, "_anthropic", lambda: client):
            pdf = make_pdf(tmp / "t.pdf")
            job = jobs.create_job(pdf, "t.pdf", title="", publisher="",
                                  preset="standard", mode="batch", pages=[1, 2, 3],
                                  offset=None, dpi=40, estimate_thb=None)
            jobs.submit_batch(job["id"])
            while jobs.is_running(job["id"]):
                time.sleep(0.05)
            self.assertEqual(jobs.load_job(job["id"])["status"], "submitted")
            bid = list(client.messages.batches.batches)[0]
            client.messages.batches.results_by_id[bid] = [
                make_result(3, GOOD), make_result(1, GOOD),
                make_result(2, "x", stop_reason="max_tokens")]
            msg = jobs.check_batch(job["id"])
            job = jobs.load_job(job["id"])
            self.assertEqual(job["status"], "incomplete")
            self.assertIn("2/3", msg)
            flags = jobs.flagged_pages(job)
            self.assertEqual(list(flags), [2])
            self.assertIn("ไม่จบหน้า", flags[2][0])


if __name__ == "__main__":
    unittest.main()
