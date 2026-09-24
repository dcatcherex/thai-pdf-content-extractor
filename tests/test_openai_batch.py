"""OpenAI Batch API path — fake openai client, no network."""
from __future__ import annotations
import json, os, sys, tempfile, time, types, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402
import openai_batch, providers, jobs, batch_extractor  # noqa: E402

GOOD = "# หน้า\nข้อความยาวพอสมควรสำหรับทดสอบระบบถอดความ"


class FakeOpenAI:
    def __init__(self):
        self.uploads, self.batches_made = {}, {}
        self.status = {}
        self.outputs, self.errors = {}, {}
        self.files = SimpleNamespace(create=self._upload, content=self._content)
        self.batches = SimpleNamespace(create=self._create, retrieve=self._retrieve)

    def _upload(self, file, purpose):
        fid = f"file-in{len(self.uploads)}"
        self.uploads[fid] = [json.loads(l) for l in file.read().decode().splitlines()]
        return SimpleNamespace(id=fid)

    def _create(self, input_file_id, endpoint, completion_window, metadata=None):
        bid = f"batch_{len(self.batches_made)}"
        self.batches_made[bid] = input_file_id
        self.status[bid] = "in_progress"
        return SimpleNamespace(id=bid)

    def _retrieve(self, bid):
        return SimpleNamespace(status=self.status[bid],
                               output_file_id=f"out-{bid}" if bid in self.outputs else None,
                               error_file_id=f"err-{bid}" if bid in self.errors else None)

    def _content(self, fid):
        kind, bid = fid.split("-", 1)
        lines = (self.outputs if kind == "out" else self.errors)[bid]
        return SimpleNamespace(text="\n".join(json.dumps(l) for l in lines))

    def pages_of(self, bid):
        return [openai_batch.cid_to_page(l["custom_id"])
                for l in self.uploads[self.batches_made[bid]]]


def ok(pno, text=GOOD, finish="stop"):
    return {"id": "x", "custom_id": openai_batch.cid(pno), "error": None,
            "response": {"status_code": 200, "body": {"choices": [
                {"message": {"content": text}, "finish_reason": finish}]}}}


def err(pno, code="server_error"):
    return {"custom_id": openai_batch.cid(pno), "response": None,
            "error": {"code": code, "message": "x"}}


def make_pdf(path, n=6):
    doc = fitz.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"page {i}")
    doc.save(path)
    return path


class Core(unittest.TestCase):
    def test_build_line(self):
        line = openai_batch.build_line(3, "/9j/abc", "gpt-6-sol")
        self.assertEqual(line["custom_id"], "page_0003")
        self.assertEqual(line["url"], "/v1/chat/completions")
        body = line["body"]
        self.assertEqual(body["max_completion_tokens"], openai_batch.MAX_TOKENS)
        self.assertTrue(body["messages"][0]["content"][1]["image_url"]["url"]
                        .startswith("data:image/jpeg;base64,"))
        self.assertNotIn("reasoning_effort", body)
        with mock.patch.dict(os.environ, {"EXTRACTOR_EFFORT": "low"}):
            self.assertEqual(openai_batch.build_line(3, "x", "m")["body"]["reasoning_effort"], "low")

    def test_parse_lines(self):
        text = "\n".join(json.dumps(x) for x in [
            ok(5), ok(4, finish="length"), ok(2, text=" "), err(3),
            err(6, "batch_expired"),
            {"custom_id": "page_0007", "response": {"status_code": 400, "body": {}}}])
        r = openai_batch.parse_lines(text)
        self.assertEqual(r[5], ("ok", GOOD))
        self.assertEqual(r[4][0], "truncated")
        self.assertEqual(r[2][0], "empty")
        self.assertEqual(r[3][0], "errored")
        self.assertEqual(r[6][0], "expired")
        self.assertEqual(r[7][0], "errored")

    def test_split_persist_and_no_resubmit(self):
        tmp = Path(tempfile.mkdtemp())
        doc = fitz.open(make_pdf(tmp / "t.pdf"))
        c = FakeOpenAI()
        with mock.patch.object(openai_batch, "MAX_FILE_BYTES", 1):   # 1 page per file
            s = openai_batch.submit(c, doc, [0, 1, 2], 40, "gpt-6-sol", tmp, "t.pdf", 1)
            self.assertEqual(len(s["batches"]), 3)
            self.assertEqual([c.pages_of(b["id"]) for b in s["batches"]], [[0], [1], [2]])
            saved = json.loads((tmp / "openai_batch.json").read_text())
            self.assertEqual(len(saved["batches"]), 3)
            openai_batch.submit(c, doc, [0, 1, 2], 40, "gpt-6-sol", tmp, "t.pdf", 1)
        self.assertEqual(len(c.batches_made), 3)           # nothing re-created
        self.assertEqual(list(tmp.glob("openai_requests_*.jsonl")), [])

    def test_resume_after_crash_submits_only_missing(self):
        tmp = Path(tempfile.mkdtemp())
        doc = fitz.open(make_pdf(tmp / "t.pdf"))
        c = FakeOpenAI()
        (tmp / "openai_batch.json").write_text(json.dumps({
            "provider": "openai", "batches": [{"id": "old", "input_file": "f", "pages": [0, 1]}],
            "pages": [0, 1, 2, 3], "model": "gpt-6-sol", "dpi": 40,
            "pdf_name": "t.pdf", "pdf_size": 1, "submitted_at": "x"}))
        s = openai_batch.submit(c, doc, [0, 1, 2, 3], 40, "gpt-6-sol", tmp, "t.pdf", 1)
        self.assertEqual(len(s["batches"]), 2)
        self.assertEqual(c.pages_of(s["batches"][1]["id"]), [2, 3])

    def test_collect_expired_and_failed(self):
        tmp = Path(tempfile.mkdtemp())
        doc = fitz.open(make_pdf(tmp / "t.pdf"))
        (tmp / "figures").mkdir()
        c = FakeOpenAI()
        c.status = {"b0": "expired", "b1": "failed"}
        c.outputs = {"b0": [ok(1)]}                          # partial output
        c.errors = {"b0": [err(2, "batch_expired")]}
        state = {"batches": [{"id": "b0", "pages": [1, 2, 3]},
                             {"id": "b1", "pages": [4]}]}
        pages, errored, expired, truncated, empty = openai_batch.collect(
            c, state, doc, tmp / "figures", tmp)
        self.assertEqual(list(pages), [1])
        self.assertEqual(expired, [2, 3])                   # 3 had no line at all
        self.assertEqual(errored, [4])


class CLI(unittest.TestCase):
    def test_no_wait_then_retrieve(self):
        tmp = Path(tempfile.mkdtemp())
        pdf = make_pdf(tmp / "t.pdf")
        out = tmp / "out"
        c = FakeOpenAI()
        fake = types.ModuleType("openai")
        fake.OpenAI = lambda *a, **k: c
        base = ["batch_extractor.py", str(pdf), "--provider", "openai",
                "--out", str(out), "--dpi", "40", "--pages", "1-3"]
        with mock.patch.dict(sys.modules, {"openai": fake}):
            with mock.patch.object(sys, "argv", base + ["--no-wait"]):
                batch_extractor.main()
            (bid,) = c.batches_made
            c.status[bid] = "completed"
            c.outputs[bid] = [ok(3), ok(1)]
            c.errors[bid] = [err(2)]
            with mock.patch.object(sys, "argv", base), \
                 mock.patch.object(openai_batch, "POLL_SECONDS", 0):
                batch_extractor.main()
        self.assertEqual(len(c.batches_made), 1)
        chunks = (out / "chunks.jsonl").read_text().strip().splitlines()
        self.assertEqual([json.loads(x)["pdf_index"] for x in chunks], [1, 3])
        self.assertEqual(json.loads((out / "failed_pages.json").read_text())["errored"], [2])


class WebJob(unittest.TestCase):
    def test_openai_preset_overnight(self):
        tmp = Path(tempfile.mkdtemp())
        c = FakeOpenAI()
        preset = {"label": "GPT", "provider": "openai", "model": "gpt-6-sol",
                  "batch_ok": True, "desc": ""}
        with mock.patch.object(jobs, "JOBS_DIR", tmp / "jobs"), \
             mock.patch.dict(jobs.PRESETS, {"gpt": preset}), \
             mock.patch.object(jobs, "_openai", lambda: c):
            job = jobs.create_job(make_pdf(tmp / "t.pdf").read_bytes(), "t.pdf",
                                  title="", publisher="", preset="gpt", mode="batch",
                                  pages=[0, 1], offset=None, dpi=40, estimate_thb=None)
            jobs.submit_batch(job["id"])
            while jobs.is_running(job["id"]):
                time.sleep(0.05)
            self.assertEqual(jobs.load_job(job["id"])["status"], "submitted")
            self.assertIn("ภายหลัง", jobs.check_batch(job["id"]))
            (bid,) = c.batches_made
            c.status[bid] = "completed"
            c.outputs[bid] = [ok(1), ok(0)]
            self.assertIn("2/2", jobs.check_batch(job["id"]))
            self.assertEqual(jobs.load_job(job["id"])["status"], "done")


if __name__ == "__main__":
    unittest.main()
