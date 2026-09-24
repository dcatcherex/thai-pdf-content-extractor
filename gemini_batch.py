"""
gemini_batch.py — Gemini Batch API path (50% off, target turnaround < 24 h).

Flow (JSONL input file, recommended by Google for large jobs):
  1) render pages one at a time into a JSONL file, one line per page:
       {"key": "page_0105", "request": {GenerateContentRequest}}
  2) upload it with the Files API (limit 2 GB — no splitting needed here)
  3) batches.create(model, src=<file name>)  → persist gemini_batch.json AT ONCE
     (creation is NOT idempotent: calling it twice creates and bills two jobs)
  4) poll batches.get() until JOB_STATE_SUCCEEDED / FAILED / CANCELLED / EXPIRED
  5) download the result JSONL, match lines to pages BY KEY, never by position

Results are kept by Google for 6 weeks; a local copy is saved as
gemini_results.jsonl so re-assembly never needs the network.
Used by batch_extractor.py (--provider gemini) and the web app (jobs.py).
"""
from __future__ import annotations
import json, time
from pathlib import Path

import common
import providers

MAX_TOKENS = 16384
POLL_SECONDS = 30
DONE_STATES = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED",
               "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}


def cid(pno: int) -> str:
    return f"page_{pno:04d}"


def cid_to_page(key: str) -> int:
    return int(key.split("_")[1])


def build_line(pno: int, img_b64: str, model: str) -> dict:
    return {"key": cid(pno), "request": {
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": providers.media_type_of(img_b64),
                             "data": img_b64}},
            {"text": common.PROMPT},
        ]}],
        "generation_config": providers.gemini_generation_config(model, MAX_TOKENS),
    }}


def write_jsonl(doc, pages, dpi, model, path: Path) -> int:
    """Stream pages into the JSONL (one rendered page in memory at a time)."""
    with open(path, "w", encoding="utf-8") as f:
        for i, pno in enumerate(pages, 1):
            img = common.render_page_b64(doc, pno, dpi)
            f.write(json.dumps(build_line(pno, img, model)) + "\n")
            print(f"\r  rendered {i}/{len(pages)} pages", end="", flush=True)
    print()
    return path.stat().st_size


def load_state(state_path: Path) -> dict | None:
    return json.loads(state_path.read_text()) if state_path.exists() else None


def submit(client, doc, pages, dpi, model, work_dir: Path, pdf_name: str,
           pdf_size: int) -> dict:
    """Build + upload the JSONL and create ONE batch job. If gemini_batch.json
    already holds a job, return it untouched — never create a second one."""
    state_path = work_dir / "gemini_batch.json"
    state = load_state(state_path)
    if state and state.get("batch_name"):
        return state
    jsonl = work_dir / "gemini_requests.jsonl"
    size = write_jsonl(doc, pages, dpi, model, jsonl)
    print(f"  uploading {size / 1e6:.0f} MB request file...")
    up = client.files.upload(file=str(jsonl), config={
        "display_name": jsonl.name, "mime_type": "jsonl"})
    job = client.batches.create(model=model, src=up.name,
                                config={"display_name": f"thai-pdf-{pdf_name}"[:120]})
    state = {"provider": "gemini", "batch_name": job.name, "input_file": up.name,
             "pages": pages, "model": model, "dpi": dpi,
             "pdf_name": pdf_name, "pdf_size": pdf_size,
             "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    state_path.write_text(json.dumps(state, indent=2))      # persist immediately
    try:
        jsonl.unlink()           # local copy no longer needed (can be large)
    except OSError:
        pass
    return state


def job_state(client, state) -> str:
    return client.batches.get(name=state["batch_name"]).state.name


def wait(client, state) -> str:
    while True:
        s = job_state(client, state)
        if s in DONE_STATES:
            return s
        print(f"  {state['batch_name']}: {s}", flush=True)
        time.sleep(POLL_SECONDS)


def _text_and_finish(resp: dict) -> tuple[str, str]:
    cands = resp.get("candidates") or []
    if not cands:
        return "", ""
    c = cands[0]
    parts = (c.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return text, str(c.get("finishReason") or c.get("finish_reason") or "")


def parse_results(text: str, pages) -> tuple[dict, list, list, list, list]:
    """Result JSONL → (pages_md, errored, expired, truncated, empty).
    Lines are matched by "key"; requested pages with no line count as errored."""
    got, errored, truncated, empty = {}, [], [], []
    seen = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        key = obj.get("key")
        if not key:
            continue
        pno = cid_to_page(key)
        seen.add(pno)
        if obj.get("error") or not obj.get("response"):
            errored.append(pno)
            continue
        md, finish = _text_and_finish(obj["response"])
        if finish.upper().endswith("MAX_TOKENS"):
            truncated.append(pno)
        elif not md.strip():
            empty.append(pno)
        else:
            got[pno] = md
    errored += [p for p in pages if p not in seen]
    return got, sorted(errored), [], sorted(truncated), sorted(empty)


def collect(client, state, doc, fig_dir: Path, work_dir: Path):
    """Call once the job is done. Same return shape as
    batch_extractor.collect_results: (pages, errored, expired, truncated, empty)."""
    raw = work_dir / "gemini_results.jsonl"
    job = client.batches.get(name=state["batch_name"])
    s = job.state.name
    if s != "JOB_STATE_SUCCEEDED":
        pages = list(state["pages"])
        return ({}, [] if s == "JOB_STATE_EXPIRED" else pages,
                pages if s == "JOB_STATE_EXPIRED" else [], [], [])
    if not raw.exists():
        data = client.files.download(file=job.dest.file_name)
        raw.write_bytes(data if isinstance(data, bytes) else bytes(data))
    got, errored, expired, truncated, empty = parse_results(
        raw.read_text(encoding="utf-8"), state["pages"])
    pages = {}
    for pno, md in got.items():
        saved = common.save_figures(doc, pno, fig_dir)
        if saved:
            md += "\n\n" + "\n".join(f"<!-- figure_file: {p.name} -->" for p in saved)
        pages[pno] = {"markdown": md, "n_figures": md.count("[FIGURE")}
    return pages, errored, expired, truncated, empty
