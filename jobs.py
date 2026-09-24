"""
jobs.py — background extraction jobs for the web app (app.py).

Each job lives in jobs/<id>/:
    job.json      settings + status
    source.pdf    the uploaded PDF
    state.db      per-page progress/results (same schema as extractor.py)
    batch.json    batch ids (overnight mode only)
    output/       document.md, chunks.jsonl, document.json, figures/

Jobs run in background threads inside the Streamlit server process, so they
keep going if the staff member closes the browser. If the server itself
restarts, a job shows as stopped and "continue" resumes from state.db.
"""
from __future__ import annotations
import datetime, hashlib, io, json, re, sqlite3, threading, time, zipfile
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from types import SimpleNamespace

import fitz  # PyMuPDF

import common, providers, extractor, quality, estimate_cost

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "jobs"
SETTINGS_PATH = ROOT / "app_settings.json"

# Quality presets shown to staff. Edit models here; verify with compare.py.
PRESETS = {
    "economy":  {"label": "ประหยัด (gemini-3.8-flash)",          "provider": "gemini",
                 "model": "gemini-3.8-flash", "batch_ok": True,
                 "desc": "ถูกที่สุด เหมาะกับเอกสารข้อความล้วน ตารางน้อย"},
    "standard": {"label": "มาตรฐาน (แนะนำ) (sonnet-5)",  "provider": "anthropic",
                 "model": "claude-sonnet-5", "batch_ok": True,
                 "desc": "สมดุลระหว่างคุณภาพและราคา อ่านตารางภาษาไทยได้ดี"},
    "best":     {"label": "ละเอียดสูงสุด (opus-5-5)",    "provider": "anthropic",
                 "model": "claude-opus-5-5", "batch_ok": True,
                 "desc": "แพงกว่า ใช้กับเอกสารตารางซับซ้อน หรือหน้าที่อ่านผิด"},
}
KEY_VARS = {"anthropic": ["ANTHROPIC_API_KEY"], "openai": ["OPENAI_API_KEY"],
            "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"]}

DEFAULT_SETTINGS = {
    "thb_per_usd": 33.41,        # set to the current exchange rate
    "budget_limit_thb": 1000.0, # a job estimated above this can't be started
    "workers": 4,               # parallel API calls per job
    "dpi": 150,
}

_RUNNING: dict[str, threading.Event] = {}      # job_id -> stop event
_LOCK = threading.Lock()


# ---------------- settings / keys ----------------

def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return s


def save_settings(s: dict):
    SETTINGS_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def has_key(provider: str) -> bool:
    import os
    return any(os.environ.get(k) for k in KEY_VARS[provider])


# ---------------- job storage ----------------

def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def load_job(job_id: str) -> dict:
    return json.loads((job_dir(job_id) / "job.json").read_text(encoding="utf-8"))


def save_job(job: dict):
    p = job_dir(job["id"]) / "job.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def update_job(job_id: str, **fields) -> dict:
    with _LOCK:
        job = load_job(job_id)
        job.update(fields)
        save_job(job)
        return job


def list_jobs() -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    out = []
    for d in JOBS_DIR.iterdir():
        try:
            out.append(load_job(d.name))
        except Exception:
            continue
    return sorted(out, key=lambda j: j["created"], reverse=True)


def create_job(pdf_bytes: bytes, filename: str, *, title: str, publisher: str,
               preset: str, mode: str, pages: list[int], offset: int | None,
               dpi: int, estimate_thb: tuple | None) -> dict:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", Path(filename).stem)[:30].strip("-") or "doc"
    job_id = f"{stamp}-{slug}"
    d = job_dir(job_id)
    (d / "output" / "figures").mkdir(parents=True, exist_ok=True)
    (d / "source.pdf").write_bytes(pdf_bytes)
    p = PRESETS[preset]
    job = {
        "id": job_id, "filename": filename, "title": title, "publisher": publisher,
        "preset": preset, "provider": p["provider"], "model": p["model"],
        "mode": mode, "pages": pages, "page_offset": offset, "dpi": dpi,
        "status": "queued", "error": None, "estimate_thb": estimate_thb,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "edited_pages": [],
    }
    save_job(job)
    return job


# ---------------- helpers ----------------

def _pdf_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _open(job):
    d = job_dir(job["id"])
    doc = fitz.open(d / "source.pdf")
    con = extractor.init_db(d / "state.db", len(doc), _pdf_hash(d / "source.pdf"))
    return doc, con


def _read_con(job):
    return sqlite3.connect(job_dir(job["id"]) / "state.db", timeout=30)


def doc_meta(job) -> dict:
    return {
        "source_file": job["filename"], "title": job["title"] or None,
        "publisher": job["publisher"] or None,
        "extracted_at": datetime.date.today().isoformat(),
        "model": job["model"], "page_offset": job["page_offset"],
    }


def assemble(job):
    doc, con = _open(job)
    try:
        extractor.assemble(con, job_dir(job["id"]) / "output", len(doc), doc_meta(job))
    finally:
        con.close()


def estimate_thb(doc, pages, dpi, preset, batch, settings) -> tuple[float, float] | None:
    p = PRESETS[preset]
    price = providers.PRICING.get(p["provider"], {}).get(p["model"])
    if not price or not pages:
        return None
    pin, pout = price
    sample = pages[:: max(1, len(pages) // 6)] or pages[:1]
    toks = []
    for pno in sample:
        r = doc[pno].rect
        w, h = int(r.width * dpi / 72), int(r.height * dpi / 72)
        toks.append(estimate_cost.image_tokens(p["model"], w, h))
    tin = (sum(toks) / len(toks) + len(common.PROMPT) / 3.5) * len(pages) / 1e6
    f = estimate_cost.output_factor(p["model"])
    disc = providers.BATCH_MULTIPLIER.get(p["provider"], 1.0) if batch else 1.0
    rate = settings["thb_per_usd"]
    lo = (tin * pin + 750 * f * len(pages) / 1e6 * pout) * disc * rate
    hi = (tin * pin + 1100 * f * len(pages) / 1e6 * pout) * disc * rate
    return round(lo, 2), round(hi, 2)


# ---------------- progress ----------------

def progress(job) -> dict:
    """Counts over this job's selected pages."""
    wanted = set(job["pages"])
    counts = {"done": 0, "failed": 0, "pending": 0, "total": len(wanted)}
    if not (job_dir(job["id"]) / "state.db").exists():
        counts["pending"] = len(wanted)
        return counts
    con = _read_con(job)
    try:
        for pno, status in con.execute("SELECT page_no, status FROM pages"):
            if pno in wanted:
                key = status if status in ("done", "failed") else "pending"
                counts[key] += 1
    finally:
        con.close()
    return counts


def is_running(job_id: str) -> bool:
    return job_id in _RUNNING


def effective_status(job) -> str:
    """A job saved as running/submitting whose thread is gone (server restart)
    is really 'stopped' — it can be continued."""
    if job["status"] in ("running", "submitting") and not is_running(job["id"]):
        return "stopped"
    return job["status"]


def page_rows(job) -> dict[int, dict]:
    con = _read_con(job)
    try:
        rows = con.execute("SELECT page_no, status, markdown, error, provider "
                           "FROM pages").fetchall()
    finally:
        con.close()
    wanted = set(job["pages"])
    return {p: {"status": s, "markdown": m, "error": e, "provider": pr}
            for p, s, m, e, pr in rows if p in wanted}


def flagged_pages(job) -> dict[int, list[str]]:
    return {p: f for p, r in sorted(page_rows(job).items())
            if (f := quality.page_flags(r["markdown"], r["status"], r["error"]))}


def page_png(job, pno: int, dpi: int = 110) -> bytes:
    doc = fitz.open(job_dir(job["id"]) / "source.pdf")
    return doc[pno].get_pixmap(dpi=dpi).tobytes("png")


# ---------------- run now (sync, background thread) ----------------

def start(job_id: str, workers: int | None = None):
    """Start or resume a 'now' job in the background."""
    if is_running(job_id):
        return
    stop = threading.Event()
    _RUNNING[job_id] = stop
    workers = workers or load_settings()["workers"]
    threading.Thread(target=_run, args=(job_id, stop, workers), daemon=True).start()


def stop(job_id: str):
    ev = _RUNNING.get(job_id)
    if ev:
        ev.set()


def _run(job_id: str, stop_ev: threading.Event, workers: int):
    job = update_job(job_id, status="running", error=None)
    try:
        doc, con = _open(job)
        fig_dir = job_dir(job_id) / "output" / "figures"
        wanted = set(job["pages"])
        todo = [p for p in extractor.pending_pages(con) if p in wanted]
        it, inflight = iter(todo), {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            def submit_next():
                if stop_ev.is_set():
                    return False
                pno = next(it, None)
                if pno is None:
                    return False
                img = common.render_page_b64(doc, pno, job["dpi"])
                inflight[ex.submit(extractor.vision_with_retry, img,
                                   job["provider"], pno, job["model"])] = pno
                return True
            for _ in range(max(1, workers) * 2):
                if not submit_next():
                    break
            while inflight:
                finished, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in finished:
                    pno = inflight.pop(fut)
                    md, attempts, error = fut.result()
                    extractor.record_result(con, doc, pno, fig_dir,
                                            job["provider"], md, attempts, error)
                    submit_next()
        extractor.assemble(con, job_dir(job_id) / "output", len(doc), doc_meta(job))
        con.close()
        c = progress(load_job(job_id))
        status = ("stopped" if stop_ev.is_set() and c["pending"]
                  else "done" if c["done"] == c["total"] else "incomplete")
        update_job(job_id, status=status)
    except Exception as e:
        update_job(job_id, status="failed", error=str(e)[:500])
    finally:
        _RUNNING.pop(job_id, None)


# ---------------- overnight (Anthropic batch, -50%) ----------------

def _anthropic():
    from anthropic import Anthropic
    return Anthropic()


def _gemini():
    return providers.gemini_client()


def _openai():
    from openai import OpenAI
    return OpenAI()


def _file_batch(provider):
    """(module, client, state file) for the JSONL-file batch providers."""
    if provider == "gemini":
        import gemini_batch
        return gemini_batch, _gemini(), "gemini_batch.json"
    import openai_batch
    return openai_batch, _openai(), "openai_batch.json"


def submit_batch(job_id: str):
    if is_running(job_id):
        return
    _RUNNING[job_id] = threading.Event()

    def work():
        import batch_extractor, gemini_batch
        job = update_job(job_id, status="submitting", error=None)
        try:
            doc = fitz.open(job_dir(job_id) / "source.pdf")
            if job["provider"] in ("gemini", "openai"):
                mod, client, _ = _file_batch(job["provider"])
                mod.submit(client, doc, job["pages"], job["dpi"],
                           job["model"], job_dir(job_id), "source.pdf",
                           (job_dir(job_id) / "source.pdf").stat().st_size)
                update_job(job_id, status="submitted")
                return
            state_path = job_dir(job_id) / "batch.json"
            state = (batch_extractor._load_state(state_path) if state_path.exists()
                     else {"batch_ids": [], "submitted_pages": [],
                           "pages": job["pages"], "model": job["model"],
                           "dpi": job["dpi"], "n_pages": len(doc),
                           "pdf_name": "source.pdf",
                           "pdf_size": (job_dir(job_id) / "source.pdf").stat().st_size,
                           "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            batch_extractor.submit_batch(_anthropic(), doc, job["pages"], job["dpi"],
                                         job["model"], state, state_path)
            update_job(job_id, status="submitted")
        except Exception as e:
            update_job(job_id, status="failed", error=str(e)[:500])
        finally:
            _RUNNING.pop(job_id, None)
    threading.Thread(target=work, daemon=True).start()


def check_batch(job_id: str) -> str:
    """Poll once. If every batch has ended, download results into state.db and
    assemble. Returns a short Thai status message."""
    import batch_extractor, gemini_batch
    job = load_job(job_id)
    fig_dir = job_dir(job_id) / "output" / "figures"
    if job["provider"] == "gemini":
        client = _gemini()
        state = gemini_batch.load_state(job_dir(job_id) / "gemini_batch.json")
        s = gemini_batch.job_state(client, state)
        if s not in gemini_batch.DONE_STATES:
            return ("รอคิวประมวลผล" if s == "JOB_STATE_PENDING"
                    else "ยังประมวลผลอยู่") + " ลองตรวจสอบอีกครั้งภายหลัง"
        doc, con = _open(job)
        pages, errored, expired, truncated, empty = gemini_batch.collect(
            client, state, doc, fig_dir, job_dir(job_id))
        provider_tag = "gemini-batch"
    elif job["provider"] == "openai":
        import openai_batch
        client = _openai()
        state = openai_batch.load_state(job_dir(job_id) / "openai_batch.json")
        st = openai_batch.statuses(client, state)
        waiting = [s for s in st.values() if s not in openai_batch.DONE]
        if waiting:
            return (f"ยังประมวลผลอยู่ ({len(st) - len(waiting)}/{len(st)} ชุดเสร็จแล้ว) "
                    "ลองตรวจสอบอีกครั้งภายหลัง")
        doc, con = _open(job)
        pages, errored, expired, truncated, empty = openai_batch.collect(
            client, state, doc, fig_dir, job_dir(job_id))
        provider_tag = "openai-batch"
    else:
        state = batch_extractor._load_state(job_dir(job_id) / "batch.json")
        client = _anthropic()
        pending = 0
        for bid in state["batch_ids"]:
            b = client.messages.batches.retrieve(bid)
            if b.processing_status != "ended":
                pending += b.request_counts.processing
        if pending:
            return f"ยังประมวลผลอยู่ อีกประมาณ {pending} หน้า"
        doc, con = _open(job)
        pages, errored, expired, truncated, empty = batch_extractor.collect_results(
            client, state["batch_ids"], doc, fig_dir)
        provider_tag = "anthropic-batch"
    now = time.time()
    for pno, rec in pages.items():
        con.execute("""UPDATE pages SET status='done', attempts=1, markdown=?,
                       n_figures=?, provider=?, error=NULL, updated_at=?
                       WHERE page_no=?""",
                    (rec["markdown"], rec["n_figures"], provider_tag, now, pno))
    for label, lst in (("errored", errored), ("expired", expired),
                       ("TruncatedOutput", truncated), ("EmptyOutput", empty)):
        for pno in lst:
            con.execute("""UPDATE pages SET status='failed', error=?, updated_at=?
                           WHERE page_no=? AND status!='done'""", (label, now, pno))
    con.commit()
    extractor.assemble(con, job_dir(job_id) / "output", len(doc), doc_meta(job))
    con.close()
    c = progress(load_job(job_id))
    update_job(job_id, status="done" if c["done"] == c["total"] else "incomplete")
    return f"เสร็จแล้ว {c['done']}/{c['total']} หน้า"


# ---------------- review actions ----------------

def save_edit(job_id: str, pno: int, text: str):
    job = load_job(job_id)
    con = _read_con(job)
    con.execute("""UPDATE pages SET status='done', markdown=?, n_figures=?,
                   error=NULL, updated_at=? WHERE page_no=?""",
                (text, text.count("[FIGURE"), time.time(), pno))
    con.commit()
    con.close()
    edited = sorted(set(job.get("edited_pages", [])) | {pno})
    job = update_job(job_id, edited_pages=edited)
    assemble(job)


def redo_page(job_id: str, pno: int, preset: str) -> bool:
    """Re-read one page with another preset's model. Returns ok."""
    job = load_job(job_id)
    p = PRESETS[preset]
    doc, con = _open(job)
    try:
        img = common.render_page_b64(doc, pno, job["dpi"])
        md, attempts, error = extractor.vision_with_retry(img, p["provider"], pno,
                                                          p["model"])
        if md is None:
            return False          # keep the existing text rather than wiping it
        ok = extractor.record_result(con, doc, pno,
                                     job_dir(job_id) / "output" / "figures",
                                     p["provider"], md, attempts, error)
        extractor.assemble(con, job_dir(job_id) / "output", len(doc), doc_meta(job))
        return ok
    finally:
        con.close()


def output_zip(job) -> bytes | None:
    out = job_dir(job["id"]) / "output"
    if not (out / "document.md").exists():
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in out.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(out))
    return buf.getvalue()
