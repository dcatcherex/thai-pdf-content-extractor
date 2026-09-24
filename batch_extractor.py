"""
Batch-mode Thai PDF extractor (Anthropic Message Batches API).

Submits ALL pages as one or more asynchronous batches at 50% of standard API
cost. Best for large one-off documents where you don't need live, page-by-page
output and can wait (usually <1h, up to 24h).

Trade-offs vs. extractor.py (sync mode):
  + 50% cheaper (batch pricing)
  - Anthropic only (OpenAI/Gemini have separate batch APIs)
  - No live progress; results arrive when each batch ends
  Resume model: submitted batch ids are saved to <out>/batch.json. Re-running
  reconnects to those batches and retrieves results — no re-submission, no
  double charge.

Flow:
  1) render pages sequentially, grouping them into batches that stay under
     the Batches API's 256 MB request-body cap (we split at MAX_BATCH_BYTES
     of base64 image data, with a safety margin, and cap at 10,000 requests
     per batch); each request is tagged custom_id=page_NNNN
  2) persist batch.json AFTER EACH batch is submitted — so a crash mid-way
     never resubmits a group already submitted
  3) poll every batch until all have ended
  4) stream results from every batch, match by custom_id (order is NOT
     guaranteed within or across batches)
  5) save embedded figures, assemble Markdown / JSONL / JSON
  Pages that errored/expired/were truncated/came back empty are recorded so
  you can resubmit just those.

Usage:
    python batch_extractor.py input.pdf --out ./out_batch --dpi 150
    python batch_extractor.py input.pdf --out ./out_batch --no-wait   # submit & exit
    python batch_extractor.py input.pdf --out ./out_batch             # rerun = resume

Requires: pip install pymupdf anthropic python-dotenv
API key: ANTHROPIC_API_KEY (loaded from .env next to this script if present).
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

import fitz  # PyMuPDF
import common
import providers
from providers import DEFAULT_MODELS

common.load_env()

# Output-token ceiling. You only pay for tokens actually generated, so a high
# ceiling costs nothing extra on a normal page — it just prevents truncating
# dense Thai tables on the rare page that needs more room.
MAX_TOKENS = 16384
POLL_SECONDS = 30

# The Anthropic Batches API caps a single batch's request body at 256 MB.
# We split well under that (measured: ~232 MB of base64 image data for our
# reference 364-page document at 200 DPI) to leave headroom for per-request
# JSON overhead (custom_id, params wrapper, etc).
MAX_BATCH_BYTES = 180_000_000
MAX_REQUESTS_PER_BATCH = 10_000


def cid(page_no: int) -> str:
    return f"page_{page_no:04d}"


def cid_to_page(custom_id: str) -> int:
    return int(custom_id.split("_")[1])


EFFORT = None  # set from --effort; None = model default


def _build_request(pno, img_b64, model):
    """Plain-dict request. The Batches API accepts {"custom_id", "params"}
    directly, so we don't need the anthropic.types request-building classes
    (simpler, and easy to construct/stub without the SDK installed)."""
    params_extra = {"output_config": {"effort": EFFORT}} if EFFORT else {}
    return {
        "custom_id": cid(pno),
        "params": {
            **params_extra,
            "model": model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": providers.media_type_of(img_b64), "data": img_b64}},
                {"type": "text", "text": common.PROMPT},
            ]}],
        },
    }


def submit_batch(client, doc, pages, dpi, model, state, state_path):
    """Render pages sequentially and submit them as one or more batches,
    splitting whenever the running base64 byte total would exceed
    MAX_BATCH_BYTES (or the request count would exceed MAX_REQUESTS_PER_BATCH).
    Persists `state` to state_path after EACH batch submission, so a crash
    mid-way never resubmits a group already submitted. Mutates and returns
    `state`. Only pages not already covered by state["submitted_pages"] are
    (re)submitted, so this also serves the crash-resume path."""
    already = {p for group in state.get("submitted_pages", []) for p in group}
    remaining = [p for p in pages if p not in already]
    if not remaining:
        return state

    batch_ids = state.setdefault("batch_ids", [])
    submitted_pages = state.setdefault("submitted_pages", [])

    def flush(group_requests, group_pages):
        if not group_requests:
            return
        batch = client.messages.batches.create(requests=group_requests)
        batch_ids.append(batch.id)
        submitted_pages.append(group_pages)
        state_path.write_text(json.dumps(state, indent=2))
        print(f"\n  submitted batch {batch.id} ({len(group_pages)} pages, "
              f"saved to {state_path})")

    group_requests, group_pages, group_bytes = [], [], 0
    for i, pno in enumerate(remaining, 1):
        img_b64 = common.render_page_b64(doc, pno, dpi)
        req_bytes = len(img_b64)
        would_exceed = (group_requests and
                        (group_bytes + req_bytes > MAX_BATCH_BYTES or
                         len(group_requests) >= MAX_REQUESTS_PER_BATCH))
        if would_exceed:
            flush(group_requests, group_pages)
            group_requests, group_pages, group_bytes = [], [], 0

        group_requests.append(_build_request(pno, img_b64, model))
        group_pages.append(pno)
        group_bytes += req_bytes
        print(f"\r  rendered {i}/{len(remaining)} pages", end="", flush=True)

    flush(group_requests, group_pages)
    return state


def poll_until_ended(client, batch_ids):
    """Poll every batch until all reach processing_status == 'ended'."""
    ended = {}
    pending = list(batch_ids)
    while pending:
        still_pending = []
        for batch_id in pending:
            b = client.messages.batches.retrieve(batch_id)
            if b.processing_status == "ended":
                ended[batch_id] = b
                c = b.request_counts
                print(f"  batch {batch_id}: ended "
                      f"(succeeded={c.succeeded} errored={c.errored})")
            else:
                still_pending.append(batch_id)
                c = b.request_counts
                print(f"  batch {batch_id}: processing... "
                      f"succeeded={c.succeeded} errored={c.errored} "
                      f"processing={c.processing}", flush=True)
        pending = still_pending
        if pending:
            time.sleep(POLL_SECONDS)
    return ended


def collect_results(client, batch_ids, doc, fig_dir):
    """Return (pages_dict, errored_list, expired_list, truncated_list,
    empty_list) merged across all batches."""
    pages, errored, expired, truncated, empty = {}, [], [], [], []
    for batch_id in batch_ids:
        for result in client.messages.batches.results(batch_id):
            pno = cid_to_page(result.custom_id)
            rtype = result.result.type
            if rtype == "succeeded":
                message = result.result.message
                if getattr(message, "stop_reason", None) == "max_tokens":
                    truncated.append(pno)
                    continue
                md = "".join(b.text for b in message.content if b.type == "text")
                if not md or not md.strip():
                    empty.append(pno)
                    continue
                saved = common.save_figures(doc, pno, fig_dir)
                if saved:
                    md += "\n\n" + "\n".join(
                        f"<!-- figure_file: {p.name} -->" for p in saved)
                pages[pno] = {"markdown": md, "n_figures": md.count("[FIGURE")}
            elif rtype == "expired":
                expired.append(pno)
            else:  # errored / canceled
                errored.append(pno)
    return pages, errored, expired, truncated, empty


def _load_state(state_path):
    """Load batch.json, upgrading the old single-batch_id format to the
    current multi-batch format."""
    state = json.loads(state_path.read_text())
    if "batch_id" in state and "batch_ids" not in state:
        state["batch_ids"] = [state["batch_id"]]
        state["submitted_pages"] = [state["pages"]]
    state.setdefault("batch_ids", [])
    state.setdefault("submitted_pages", [])
    return state


def _check_resume_matches(state, pages, dpi, model, pdf_path):
    """Refuse to silently reuse a batch.json submitted with different
    parameters. Prints a clear explanation and returns False on mismatch."""
    mismatches = []
    if state.get("pages") != pages:
        mismatches.append(f"pages: saved={state.get('pages')} vs requested={pages}")
    if state.get("dpi") != dpi:
        mismatches.append(f"dpi: saved={state.get('dpi')} vs requested={dpi}")
    if state.get("model") != model:
        mismatches.append(f"model: saved={state.get('model')} vs requested={model}")
    if state.get("pdf_name") is not None and state.get("pdf_name") != pdf_path.name:
        mismatches.append(
            f"pdf filename: saved={state.get('pdf_name')} vs requested={pdf_path.name}")
    pdf_size = pdf_path.stat().st_size
    if state.get("pdf_size") is not None and state.get("pdf_size") != pdf_size:
        mismatches.append(
            f"pdf size: saved={state.get('pdf_size')} vs requested={pdf_size}")
    if mismatches:
        print("ERROR: existing batch.json doesn't match this run's parameters:",
              file=sys.stderr)
        for m in mismatches:
            print(f"  - {m}", file=sys.stderr)
        print("Use a different --out for a different run, or delete batch.json "
              "if you really mean to change parameters (this forfeits the "
              "resume/no-double-charge guarantee for any already-submitted "
              "batches).", file=sys.stderr)
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default="./out_batch")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--pages", default="",
                    help="restrict to a range e.g. '0-199' (inclusive, 0-based)")
    ap.add_argument("--model", default=DEFAULT_MODELS["anthropic"],
                    help="Anthropic model (default: %(default)s)")
    ap.add_argument("--effort", default="", choices=["", "low", "medium", "high"],
                    help="output_config.effort (default: model default). "
                         "Lower = fewer output/thinking tokens; test with "
                         "compare.py first.")
    ap.add_argument("--no-wait", action="store_true",
                    help="submit the batch(es) and exit; rerun later to retrieve")
    common.add_meta_args(ap)
    args = ap.parse_args()
    global EFFORT
    EFFORT = args.effort or None

    from anthropic import Anthropic
    client = Anthropic()  # reads ANTHROPIC_API_KEY (from .env via load_env)

    pdf_path = Path(args.pdf)
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figures"; fig_dir.mkdir(exist_ok=True)
    state_path = out_dir / "batch.json"

    doc = fitz.open(pdf_path)
    n_pages = len(doc)
    pages = common.parse_pages_arg(args.pages, n_pages)
    pdf_size = pdf_path.stat().st_size

    # ---- resume: reuse existing batch_ids if present ----
    if state_path.exists():
        state = _load_state(state_path)
        if not _check_resume_matches(state, pages, args.dpi, args.model, pdf_path):
            sys.exit(1)
        already = {p for group in state["submitted_pages"] for p in group}
        remaining = [p for p in pages if p not in already]
        if remaining:
            print(f"Resuming: {len(already)}/{len(pages)} pages already "
                  f"submitted in {len(state['batch_ids'])} batch(es); "
                  f"submitting the remaining {len(remaining)}.")
            state = submit_batch(client, doc, pages, args.dpi, args.model,
                                  state, state_path)
        else:
            print(f"Resuming {len(state['batch_ids'])} existing batch(es) "
                  f"(submitted {state.get('submitted_at','?')}).")
    else:
        print(f"Submitting {len(pages)} pages via {args.model} "
              f"(50% batch pricing)...")
        state = {
            "batch_ids": [], "submitted_pages": [], "pages": pages,
            "model": args.model, "dpi": args.dpi, "n_pages": n_pages,
            "pdf_name": pdf_path.name, "pdf_size": pdf_size,
            "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        state = submit_batch(client, doc, pages, args.dpi, args.model,
                              state, state_path)
        print(f"Submitted {len(state['batch_ids'])} batch(es): "
              f"{state['batch_ids']}\nSaved to {state_path}")

    batch_ids = state["batch_ids"]

    if args.no_wait:
        print("--no-wait set; exiting. Rerun the same command to retrieve.")
        return

    print("Polling for completion (most batches finish within 1 hour)...")
    poll_until_ended(client, batch_ids)

    print("All batches ended. Retrieving results...")
    page_recs, errored, expired, truncated, empty = collect_results(
        client, batch_ids, doc, fig_dir)
    doc_meta = common.build_doc_meta(args, args.model)
    common.assemble_from_pages(page_recs, out_dir, n_pages, doc_meta)

    done = len(page_recs)
    print(f"\nDone: {done}/{len(pages)} pages succeeded.")
    if errored or expired or truncated or empty:
        problem = sorted(set(errored + expired + truncated + empty))
        (out_dir / "failed_pages.json").write_text(json.dumps({
            "errored": errored, "expired": expired,
            "truncated": truncated, "empty": empty}, indent=2))
        print(f"{len(problem)} pages need resubmission "
              f"(errored={len(errored)}, expired={len(expired)}, "
              f"truncated={len(truncated)}, empty={len(empty)}); "
              f"see failed_pages.json.")
        lo, hi = min(problem), max(problem)
        print(f"Resubmit them in a fresh --out, e.g.:\n"
              f"  python batch_extractor.py {args.pdf} "
              f"--out {args.out}_retry --pages {lo}-{hi}")
    else:
        print("All pages succeeded.")


if __name__ == "__main__":
    main()
