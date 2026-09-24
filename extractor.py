"""
Thai PDF -> AI-ready content extractor.

Designed for documents that are heavy on Thai script, tables, and illustrations
where the PDF text layer is corrupted (scrambled vowels/tone marks). Uses
page-image rendering + a vision model (Anthropic, OpenAI or Gemini) as the
PRIMARY extraction path.

Key properties:
  - Page-level checkpointing in SQLite  -> resume after any crash/Ctrl-C
  - Per-page retry with exponential backoff -> survives transient API errors
  - Figures are BOTH described (in text) AND saved (as PNG) and linked
  - Emits three artifacts: Markdown, JSONL chunks, structured JSON

Usage:
    python extractor.py input.pdf --out ./out --dpi 150
    python extractor.py input.pdf --provider gemini --service-tier flex --workers 8
    # interrupt any time; rerun the same command to resume.

Requires: pip install pymupdf python-dotenv + the SDK of the provider you use.
API keys come from .env (see USER_MANUAL.md).
"""
from __future__ import annotations
import argparse, os, sqlite3, sys, time, hashlib
from pathlib import Path

import fitz  # PyMuPDF
import providers
import common

common.load_env()

# Output-token ceiling. You only pay for tokens actually generated, so a high
# ceiling costs nothing extra on a normal page — it just prevents truncating
# dense Thai tables on the rare page that needs more room.
MAX_TOKENS = 16384
MAX_ATTEMPTS = 4
BASE_BACKOFF = 2.0  # seconds

PROMPT = common.PROMPT


# ---------- state store ----------

def init_db(db_path: Path, n_pages: int, pdf_hash: str):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS pages(
        page_no INTEGER PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        markdown TEXT,
        n_figures INTEGER DEFAULT 0,
        provider TEXT,
        error TEXT,
        updated_at REAL
    )""")
    cur = con.execute("SELECT v FROM meta WHERE k='pdf_hash'").fetchone()
    if cur and cur[0] != pdf_hash:
        print("WARNING: pdf hash changed since last run; state may be stale.", file=sys.stderr)
    con.execute("INSERT OR REPLACE INTO meta VALUES('pdf_hash', ?)", (pdf_hash,))
    con.execute("INSERT OR REPLACE INTO meta VALUES('n_pages', ?)", (str(n_pages),))
    for i in range(n_pages):
        con.execute("INSERT OR IGNORE INTO pages(page_no) VALUES(?)", (i,))
    con.commit()
    return con


def pending_pages(con):
    rows = con.execute(
        "SELECT page_no FROM pages WHERE status != 'done' ORDER BY page_no").fetchall()
    return [r[0] for r in rows]


# ---------- vision call ----------

def call_vision(img_b64: str, provider: str) -> str:
    """Single vision call via the selected provider. Raises on failure."""
    backend = providers.get_backend(provider)
    return backend(img_b64, PROMPT, MAX_TOKENS)


def vision_with_retry(img_b64, provider, page_no):
    """Call the provider with retry. Touches no shared state, so it is safe to
    run in worker threads. Returns (markdown | None, attempts, error | None)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return call_vision(img_b64, provider), attempt, None
        except (providers.TruncatedOutput, providers.EmptyOutput) as e:
            # Not transient: an immediate retry would just regenerate (and
            # re-bill) up to MAX_TOKENS of output again. Give up for this run;
            # the page stays pending and is retried on the next rerun (e.g.
            # after raising MAX_TOKENS or switching model).
            print(f"  page {page_no}: {type(e).__name__} ({e}); not retrying "
                  f"this run", file=sys.stderr)
            return None, attempt, f"{type(e).__name__}: {e}"
        except Exception as e:
            if attempt >= MAX_ATTEMPTS:
                print(f"  page {page_no}: gave up after {attempt} attempts: {e}",
                      file=sys.stderr)
                return None, attempt, str(e)[:500]
            wait = BASE_BACKOFF * (2 ** (attempt - 1))
            print(f"  page {page_no}: attempt {attempt} failed ({e}); "
                  f"retrying in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)


def record_result(con, doc, page_no, fig_dir, provider, md, attempts, error):
    """Write one page's outcome to state.db (main thread only). Returns ok."""
    if md is None:
        con.execute("""UPDATE pages SET status='failed', attempts=?, error=?,
                       updated_at=? WHERE page_no=?""",
                    (attempts, error, time.time(), page_no))
        con.commit()
        return False
    n_figs = md.count("[FIGURE")
    saved = common.save_figures(doc, page_no, fig_dir)
    if saved:
        md += "\n\n" + "\n".join(
            f"<!-- figure_file: {p.name} -->" for p in saved)
    con.execute("""UPDATE pages SET status='done', attempts=?, markdown=?,
                   n_figures=?, provider=?, error=NULL, updated_at=?
                   WHERE page_no=?""",
                (attempts, md, n_figs, provider, time.time(), page_no))
    con.commit()
    return True


def extract_page_with_retry(con, doc, page_no, dpi, fig_dir, provider):
    img_b64 = common.render_page_b64(doc, page_no, dpi)
    md, attempts, error = vision_with_retry(img_b64, provider, page_no)
    return record_result(con, doc, page_no, fig_dir, provider, md, attempts, error)


def run_parallel(con, doc, todo, dpi, fig_dir, provider, workers, offset=None):
    """Run pages through `workers` threads. Rendering, figure extraction and
    all SQLite writes stay on the main thread; only the API calls run in
    parallel. At most 2*workers rendered pages are held in memory."""
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    it = iter(todo)
    inflight, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        def submit_next():
            pno = next(it, None)
            if pno is None:
                return False
            img_b64 = common.render_page_b64(doc, pno, dpi)
            inflight[ex.submit(vision_with_retry, img_b64, provider, pno)] = pno
            return True

        for _ in range(workers * 2):
            if not submit_next():
                break
        while inflight:
            finished, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            for fut in finished:
                pno = inflight.pop(fut)
                md, attempts, error = fut.result()
                ok = record_result(con, doc, pno, fig_dir, provider,
                                   md, attempts, error)
                done += 1
                print(f"[{done}/{len(todo)}] {common.page_label(pno, offset)}: "
                      f"{'ok' if ok else 'FAILED'}")
                submit_next()


# ---------- assembly ----------

def assemble(con, out_dir: Path, n_pages: int, doc_meta=None):
    rows = con.execute(
        "SELECT page_no, markdown, n_figures FROM pages ORDER BY page_no").fetchall()
    pages = {pno: {"markdown": md, "n_figures": nf or 0}
             for pno, md, nf in rows if md}
    common.assemble_from_pages(pages, out_dir, n_pages, doc_meta)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default="./out")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--provider", default="anthropic",
                    choices=["anthropic", "openai", "gemini"],
                    help="vision provider for this run")
    ap.add_argument("--pages", default="",
                    help="restrict to pdf_index pages (0-based = viewer page - 1), "
                         "e.g. '0-99' or '2,105,180-182'. Lets you route ranges "
                         "to different prepaid providers across runs.")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N pending pages this run (0 = all)")
    ap.add_argument("--model", default="",
                    help="model ID for this run (default: providers.DEFAULT_MODELS)")
    ap.add_argument("--effort", default="", choices=["", "low", "medium", "high"],
                    help="Anthropic effort / OpenAI reasoning_effort / Gemini 3 "
                         "thinking_level. Default: provider default (Gemini: lowest).")
    ap.add_argument("--service-tier", dest="service_tier", default="standard",
                    choices=["standard", "flex"],
                    help="'flex' = OpenAI/Gemini flex tier at ~50%% off; slower "
                         "(minutes per request), use with --workers")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel API calls (default 1). Mind your rate limits.")
    common.add_meta_args(ap)
    args = ap.parse_args()

    global BASE_BACKOFF
    if args.model:
        os.environ["EXTRACTOR_MODEL"] = args.model
    if args.effort:
        os.environ["EXTRACTOR_EFFORT"] = args.effort
    if args.service_tier == "flex":
        if args.provider == "anthropic":
            ap.error("--service-tier flex is OpenAI/Gemini only; for Anthropic "
                     "use batch_extractor.py (50% off)")
        os.environ["EXTRACTOR_SERVICE_TIER"] = "flex"
        BASE_BACKOFF = 30.0   # flex capacity errors (429/503) need longer waits

    pdf_path = Path(args.pdf)
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figures"; fig_dir.mkdir(exist_ok=True)
    pdf_hash = hashlib.md5(pdf_path.read_bytes()).hexdigest()

    doc = fitz.open(pdf_path)
    n_pages = len(doc)
    con = init_db(out_dir / "state.db", n_pages, pdf_hash)

    todo = pending_pages(con)
    if args.pages or args.printed:
        wanted = set(common.resolve_pages(args.pages, args.printed,
                                          args.page_offset, n_pages))
        todo = [p for p in todo if p in wanted]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{n_pages} pages total; {len(todo)} to process this run "
          f"via {args.provider} ({providers._model_for(args.provider)}).")

    if args.workers > 1:
        run_parallel(con, doc, todo, args.dpi, fig_dir, args.provider,
                     args.workers, args.page_offset)
    else:
        done = 0
        for page_no in todo:
            ok = extract_page_with_retry(
                con, doc, page_no, args.dpi, fig_dir, args.provider)
            done += 1
            status = "ok" if ok else "FAILED"
            print(f"[{done}/{len(todo)}] "
                  f"{common.page_label(page_no, args.page_offset)}: {status}")

    doc_meta = common.build_doc_meta(args, providers._model_for(args.provider))
    remaining = len(pending_pages(con))
    if remaining == 0:
        assemble(con, out_dir, n_pages, doc_meta)
        print("COMPLETE.")
    else:
        print(f"{remaining} pages still pending/failed. Rerun to resume.")
        # still emit partial artifacts so progress is usable
        assemble(con, out_dir, n_pages, doc_meta)


if __name__ == "__main__":
    main()
