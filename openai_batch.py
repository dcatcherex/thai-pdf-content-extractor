"""
openai_batch.py — OpenAI Batch API path (50% off, 24 h completion window).

Flow:
  1) render pages into JSONL request files, one line per page:
       {"custom_id": "page_0105", "method": "POST",
        "url": "/v1/chat/completions", "body": {...same as call_openai...}}
     Each input file is capped at 200 MB / 50,000 requests, so pages are
     split into several files (MAX_FILE_BYTES leaves headroom).
  2) per file: files.create(purpose="batch") → batches.create(...)
     → persist openai_batch.json AFTER EACH batch (a crash never re-creates
     a batch that already exists, so nothing is billed twice)
  3) poll batches.retrieve until completed / failed / expired / cancelled
  4) download output_file_id (successes) and error_file_id (failures),
     match lines to pages by custom_id — output order is NOT guaranteed.
     An expired batch still returns its finished pages; the rest are
     reported as expired.
Used by batch_extractor.py (--provider openai) and jobs.py.
"""
from __future__ import annotations
import json, os, time
from pathlib import Path

import common
import providers

MAX_TOKENS = 16384
POLL_SECONDS = 30
MAX_FILE_BYTES = 180_000_000      # OpenAI limit: 200 MB per input file
MAX_REQUESTS = 50_000             # OpenAI limit per batch
ENDPOINT = "/v1/chat/completions"
DONE = {"completed", "failed", "expired", "cancelled"}


def cid(pno: int) -> str:
    return f"page_{pno:04d}"


def cid_to_page(custom_id: str) -> int:
    return int(custom_id.split("_")[1])


def build_line(pno: int, img_b64: str, model: str) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": common.PROMPT},
            {"type": "image_url", "image_url": {
                "url": f"data:{providers.media_type_of(img_b64)};base64,{img_b64}"}},
        ]}],
        "max_completion_tokens": MAX_TOKENS,
    }
    effort = providers._effort()
    if effort:
        body["reasoning_effort"] = effort
    return {"custom_id": cid(pno), "method": "POST", "url": ENDPOINT, "body": body}


def load_state(state_path: Path) -> dict | None:
    return json.loads(state_path.read_text()) if state_path.exists() else None


def _save(state_path: Path, state: dict):
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_path)


def submit(client, doc, pages, dpi, model, work_dir: Path, pdf_name: str,
           pdf_size: int) -> dict:
    """Split pages into ≤180 MB files and create one batch per file. Pages
    already in openai_batch.json are skipped, so rerunning after a crash only
    submits what is missing."""
    state_path = work_dir / "openai_batch.json"
    state = load_state(state_path) or {
        "provider": "openai", "batches": [], "pages": pages, "model": model,
        "dpi": dpi, "pdf_name": pdf_name, "pdf_size": pdf_size,
        "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    done = {p for b in state["batches"] for p in b["pages"]}
    remaining = [p for p in pages if p not in done]
    if not remaining:
        return state

    def flush(path: Path, group: list[int]):
        if not group:
            return
        with open(path, "rb") as fh:
            up = client.files.create(file=fh, purpose="batch")
        b = client.batches.create(input_file_id=up.id, endpoint=ENDPOINT,
                                  completion_window="24h",
                                  metadata={"description": f"thai-pdf {pdf_name}"[:500]})
        state["batches"].append({"id": b.id, "input_file": up.id, "pages": group})
        _save(state_path, state)                    # persist after EACH batch
        print(f"\n  submitted batch {b.id} ({len(group)} pages)")
        try:
            os.remove(path)
        except OSError:
            pass

    n = len(state["batches"])
    path = work_dir / f"openai_requests_{n}.jsonl"
    f, group, size = open(path, "w", encoding="utf-8"), [], 0
    for i, pno in enumerate(remaining, 1):
        line = json.dumps(build_line(pno, common.render_page_b64(doc, pno, dpi), model)) + "\n"
        nbytes = len(line.encode("utf-8"))
        if group and (size + nbytes > MAX_FILE_BYTES or len(group) >= MAX_REQUESTS):
            f.close(); flush(path, group)
            n += 1
            path = work_dir / f"openai_requests_{n}.jsonl"
            f, group, size = open(path, "w", encoding="utf-8"), [], 0
        f.write(line); group.append(pno); size += nbytes
        print(f"\r  rendered {i}/{len(remaining)} pages", end="", flush=True)
    f.close(); flush(path, group)
    return state


def statuses(client, state) -> dict[str, str]:
    return {b["id"]: client.batches.retrieve(b["id"]).status for b in state["batches"]}


def wait(client, state) -> dict[str, str]:
    while True:
        st = statuses(client, state)
        if all(s in DONE for s in st.values()):
            return st
        print("  " + ", ".join(f"{k}: {v}" for k, v in st.items()), flush=True)
        time.sleep(POLL_SECONDS)


def parse_lines(text: str) -> dict[int, tuple[str, str]]:
    """Output/error JSONL → {page: (kind, markdown)}, kind in
    ok / truncated / empty / errored / expired."""
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if not obj.get("custom_id"):
            continue
        pno = cid_to_page(obj["custom_id"])
        err, resp = obj.get("error"), obj.get("response") or {}
        if err:
            code = (err.get("code") if isinstance(err, dict) else "") or ""
            out[pno] = ("expired" if code == "batch_expired" else "errored", "")
            continue
        if resp.get("status_code") != 200:
            out[pno] = ("errored", "")
            continue
        choice = ((resp.get("body") or {}).get("choices") or [{}])[0]
        md = ((choice.get("message") or {}).get("content")) or ""
        if choice.get("finish_reason") == "length":
            out[pno] = ("truncated", "")
        elif not md.strip():
            out[pno] = ("empty", "")
        else:
            out[pno] = ("ok", md)
    return out


def _text(client, file_id) -> str:
    if not file_id:
        return ""
    c = client.files.content(file_id)
    return c.text if hasattr(c, "text") else c.read().decode("utf-8")


def collect(client, state, doc, fig_dir: Path, work_dir: Path):
    """(pages, errored, expired, truncated, empty), same shape as the other
    batch paths. Raw output is cached as openai_results_<batch>.jsonl."""
    results = {}
    for b in state["batches"]:
        cache = work_dir / f"openai_results_{b['id']}.jsonl"
        batch = client.batches.retrieve(b["id"])
        if cache.exists():
            text = cache.read_text(encoding="utf-8")
        else:
            text = _text(client, getattr(batch, "output_file_id", None)) + "\n" + \
                   _text(client, getattr(batch, "error_file_id", None))
            if batch.status in DONE:
                cache.write_text(text, encoding="utf-8")
        parsed = parse_lines(text)
        missing_kind = "expired" if batch.status == "expired" else "errored"
        for p in b["pages"]:
            results[p] = parsed.get(p, (missing_kind, ""))
    pages, errored, expired, truncated, empty = {}, [], [], [], []
    buckets = {"errored": errored, "expired": expired,
               "truncated": truncated, "empty": empty}
    for pno, (kind, md) in sorted(results.items()):
        if kind == "ok":
            saved = common.save_figures(doc, pno, fig_dir)
            if saved:
                md += "\n\n" + "\n".join(f"<!-- figure_file: {p.name} -->" for p in saved)
            pages[pno] = {"markdown": md, "n_figures": md.count("[FIGURE")}
        else:
            buckets[kind].append(pno)
    return pages, errored, expired, truncated, empty
