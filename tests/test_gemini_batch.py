"""Gemini Batch API path — fake google-genai client, no network."""
from __future__ import annotations
import json, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402
import gemini_batch, providers, jobs, batch_extractor  # noqa: E402


class FakeGenai:
    def __init__(self):
        self.state = "JOB_STATE_PENDING"
        self.uploaded = None
        self.creates = 0
        self.result_lines = []
        self.files = SimpleNamespace(upload=self._upload, download=self._download)
        self.batches = SimpleNamespace(create=self._create, get=self._get)

    def _upload(self, file, config):
        self.uploaded = [json.loads(l) for l in Path(file).read_text().splitlines()]
        return SimpleNamespace(name="files/input1")

    def _create(self, model, src, config):
        self.creates += 1
        self.model, self.src = model, src
        return SimpleNamespace(name="batches/42")

    def _get(self, name):
        return SimpleNamespace(state=SimpleNamespace(name=self.state),
                               dest=SimpleNamespace(file_name="files/out1"))

    def _download(self, file):
        return "\n".join(json.dumps(l) for l in self.result_lines).encode()


def ok_line(pno, text="# หน้า\nข้อความยาวพอสมควรสำหรับทดสอบระบบ", finish="STOP"):
    return {"key": gemini_batch.cid(pno), "response": {"candidates": [{
        "content": {"parts": [{"text": "thinking...", "thought": True},
                              {"text": text}]}, "finishReason": finish}]}}


def make_pdf(path, n=6):
    doc = fitz.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"page {i}")
    doc.save(path)
    return path


class GeminiBatchCore(unittest.TestCase):
    def test_build_line_shape(self):
        line = gemini_batch.build_line(7, "iVBORabc", "gemini-3.8-flash")
        self.assertEqual(line["key"], "page_0007")
        parts = line["request"]["contents"][0]["parts"]
        self.assertEqual(parts[0]["inline_data"]["mime_type"], "image/png")
        cfg = line["request"]["generation_config"]
        self.assertEqual(cfg["thinking_config"], {"thinking_level": "low"})
        self.assertEqual(cfg["max_output_tokens"], gemini_batch.MAX_TOKENS)

    def test_parse_results(self):
        text = "\n".join(json.dumps(x) for x in [
            ok_line(5), {"key": "page_0003", "error": {"code": 500}},
            ok_line(4, finish="MAX_TOKENS"), ok_line(2, text="  "), ok_line(1)])
        got, errored, expired, truncated, empty = gemini_batch.parse_results(
            text, [1, 2, 3, 4, 5, 6])
        self.assertEqual(sorted(got), [1, 5])
        self.assertNotIn("thinking...", got[5])          # thought parts dropped
        self.assertEqual(errored, [3, 6])                 # 6 had no result line
        self.assertEqual(truncated, [4])
        self.assertEqual(empty, [2])

    def test_submit_once(self):
        tmp = Path(tempfile.mkdtemp())
        doc = fitz.open(make_pdf(tmp / "t.pdf"))
        c = FakeGenai()
        s1 = gemini_batch.submit(c, doc, [0, 2], 40, "gemini-3.8-flash", tmp, "t.pdf", 1)
        s2 = gemini_batch.submit(c, doc, [0, 2], 40, "gemini-3.8-flash", tmp, "t.pdf", 1)
        self.assertEqual(c.creates, 1)                    # never a second job
        self.assertEqual(s1, s2)
        self.assertEqual([l["key"] for l in c.uploaded], ["page_0000", "page_0002"])
        self.assertEqual(c.src, "files/input1")
        self.assertTrue((tmp / "gemini_batch.json").exists())
        self.assertFalse((tmp / "gemini_requests.jsonl").exists())


class GeminiBatchCLI(unittest.TestCase):
    def test_no_wait_then_retrieve(self):
        tmp = Path(tempfile.mkdtemp())
        pdf = make_pdf(tmp / "t.pdf")
        out = tmp / "out"
        c = FakeGenai()
        base = ["batch_extractor.py", str(pdf), "--provider", "gemini",
                "--out", str(out), "--dpi", "40", "--pages", "1-3"]
        with mock.patch.object(providers, "gemini_client", lambda: c):
            with mock.patch.object(sys, "argv", base + ["--no-wait"]):
                batch_extractor.main()
            self.assertEqual(c.creates, 1)
            self.assertEqual(c.model, providers.DEFAULT_MODELS["gemini"])
            c.state = "JOB_STATE_SUCCEEDED"
            c.result_lines = [ok_line(3), ok_line(1), {"key": "page_0002", "error": {}}]
            with mock.patch.object(sys, "argv", base), \
                 mock.patch.object(gemini_batch, "POLL_SECONDS", 0):
                batch_extractor.main()
        self.assertEqual(c.creates, 1)                    # resume, no resubmit
        chunks = (out / "chunks.jsonl").read_text().strip().splitlines()
        self.assertEqual([json.loads(x)["pdf_index"] for x in chunks], [1, 3])
        failed = json.loads((out / "failed_pages.json").read_text())
        self.assertEqual(failed["errored"], [2])

    def test_mismatch_refused(self):
        tmp = Path(tempfile.mkdtemp())
        pdf = make_pdf(tmp / "t.pdf")
        c = FakeGenai()
        base = ["batch_extractor.py", str(pdf), "--provider", "gemini",
                "--out", str(tmp / "o"), "--no-wait"]
        with mock.patch.object(providers, "gemini_client", lambda: c):
            with mock.patch.object(sys, "argv", base + ["--dpi", "40"]):
                batch_extractor.main()
            with mock.patch.object(sys, "argv", base + ["--dpi", "200"]):
                with self.assertRaises(SystemExit):
                    batch_extractor.main()
        self.assertEqual(c.creates, 1)


class GeminiBatchWebJob(unittest.TestCase):
    def test_economy_overnight(self):
        tmp = Path(tempfile.mkdtemp())
        c = FakeGenai()
        pdf = make_pdf(tmp / "t.pdf").read_bytes()
        with mock.patch.object(jobs, "JOBS_DIR", tmp / "jobs"), \
             mock.patch.object(jobs, "_gemini", lambda: c):
            job = jobs.create_job(pdf, "t.pdf", title="", publisher="",
                                  preset="economy", mode="batch", pages=[0, 1],
                                  offset=None, dpi=40, estimate_thb=None)
            jobs.submit_batch(job["id"])
            while jobs.is_running(job["id"]):
                time.sleep(0.05)
            self.assertEqual(jobs.load_job(job["id"])["status"], "submitted")
            self.assertIn("ภายหลัง", jobs.check_batch(job["id"]))   # still pending
            c.state = "JOB_STATE_SUCCEEDED"
            c.result_lines = [ok_line(1), ok_line(0)]
            self.assertIn("2/2", jobs.check_batch(job["id"]))
            job = jobs.load_job(job["id"])
            self.assertEqual(job["status"], "done")
            rows = jobs.page_rows(job)
            self.assertEqual({r["provider"] for r in rows.values()}, {"gemini-batch"})


if __name__ == "__main__":
    unittest.main()
