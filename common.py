"""
Shared helpers used by both the synchronous extractor.py and the batch-mode
batch_extractor.py: .env loading, the transcription prompt, figure extraction,
output assembly, and page-range parsing. Keeping these here avoids duplication
and guarantees both modes produce identical outputs.
"""
from __future__ import annotations
import json, os
from pathlib import Path

import fitz  # PyMuPDF


def load_env():
    """Load API keys from a .env file next to the scripts, if present.
    Keys in .env OVERRIDE same-named variables already set in the system
    environment, so a stale key saved in Windows settings can't silently win.
    Falls back to real environment variables if python-dotenv isn't
    installed or no .env exists."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    here = Path(__file__).resolve().parent
    for candidate in (here / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=True)
            return


PROMPT = """You are transcribing one page of a Thai government manual into clean, AI-ready Markdown.

Rules:
- Transcribe ALL Thai text faithfully and correctly. The page image is the source of truth.
- Preserve heading hierarchy with Markdown headings (#, ##, ###).
- Render every table as a proper GitHub-flavored Markdown table. Do not flatten tables into prose.
- For each illustration, photo, diagram, chart, or infographic, insert a line:
  [FIGURE n: <concise factual description in Thai of what it shows and any text/labels inside it>]
  where n starts at 1 and increments per figure on this page.
- EXCLUDE all running page furniture: the printed page number, and any repeating
  header or footer such as the book title, chapter title, or publisher/department
  line that appears at the top or bottom of the page. Do NOT transcribe these.
  Transcribe only the actual body content of the page.
- Do not add commentary or anything not in the page body.
- Output ONLY the Markdown transcription of the body."""


# Maximum base64-encoded image size we'll send a provider. PNG is used first
# (lossless, best for dense Thai text/tables); if it's over this limit
# (typically a very image-heavy page at high DPI), we fall back to JPEG at
# decreasing quality until it fits, since providers reject or truncate
# oversized images.
# Claude API allows 10 MB per base64 image (5 MB on Bedrock/Vertex); Gemini
# allows ~20 MB per inline request. 9 MB keeps lossless PNG for almost every
# page while staying under all three direct APIs.
MAX_IMAGE_B64_BYTES = 9_000_000


def render_page_b64(doc, page_no, dpi):
    """Render a page to base64. PNG by default; falls back to JPEG (quality
    90, then 80, then 70) if the PNG base64 would exceed MAX_IMAGE_B64_BYTES.
    Returns the smallest attempt if even JPEG q70 is still over the limit."""
    import base64
    pix = doc[page_no].get_pixmap(dpi=dpi)
    png_bytes = pix.tobytes("png")
    b64 = base64.b64encode(png_bytes).decode()
    if len(b64) <= MAX_IMAGE_B64_BYTES:
        return b64

    best = b64
    for quality in (90, 80, 70):
        jpg_bytes = pix.tobytes("jpeg", jpg_quality=quality)
        cand = base64.b64encode(jpg_bytes).decode()
        best = cand
        if len(cand) <= MAX_IMAGE_B64_BYTES:
            return cand
    return best


def render_page_png_b64(doc, page_no, dpi):
    """Backward-compatible alias for render_page_b64."""
    return render_page_b64(doc, page_no, dpi)


def save_figures(doc, page_no, fig_dir: Path):
    """Extract embedded images on a page to disk; return saved paths."""
    saved = []
    page = doc[page_no]
    for idx, img in enumerate(page.get_images(full=True)):
        xref = img[0]
        try:
            base = doc.extract_image(xref)
            out = fig_dir / f"page{page_no:04d}_fig{idx+1}.{base['ext']}"
            out.write_bytes(base["image"])
            saved.append(out)
        except Exception:
            continue
    return saved


def parse_page_list(spec):
    """'96, 100-102' -> [96, 100, 101, 102] (sorted, de-duplicated)."""
    nums = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            nums.update(range(min(lo, hi), max(lo, hi) + 1))
        else:
            nums.add(int(part))
    return sorted(nums)


def _check_range(nums, n_pages):
    bad = [n for n in nums if n < 0 or n >= n_pages]
    if bad:
        raise SystemExit(f"pages out of range (doc has {n_pages} pages, "
                         f"pdf_index 0-{n_pages - 1}): {bad}")
    return nums


def parse_pages_arg(pages_str, n_pages):
    """pdf_index list/ranges ('0-99', '2,105,180-182') or '' (= all)."""
    if not pages_str:
        return list(range(n_pages))
    return _check_range(parse_page_list(pages_str), n_pages)


def resolve_pages(pages_str, printed_str, offset, n_pages):
    """Pages to process as pdf_index values. `printed_str` (printed page
    numbers, converted with pdf_index = printed + offset) wins over
    `pages_str` (pdf_index directly); both empty = every page."""
    if printed_str:
        if offset is None:
            raise SystemExit("--printed needs --page-offset (pdf_index - printed "
                             "page; viewer page 106 showing '96' -> 105-96 = 9)")
        return _check_range([p + offset for p in parse_page_list(printed_str)],
                            n_pages)
    return parse_pages_arg(pages_str, n_pages)


def page_label(pno, offset):
    """'printed 96 · pdf_index 105 (viewer 106)' — every number a person
    might use to find the page."""
    pp = printed_page(pno, offset)
    base = f"pdf_index {pno} (viewer {pno + 1})"
    return f"printed {pp} · {base}" if pp is not None else base


def printed_page(pdf_index, offset):
    """Map a 0-based PDF index to the number printed on the page.

    offset = pdf_index - printed_page. Example: if PDF index 24 shows printed
    page 14, offset is 10. Front matter (index < offset) has no printed number
    and returns None.
    """
    if offset is None:
        return None
    n = pdf_index - offset
    return n if n >= 1 else None


def build_doc_meta(args, model):
    """Collect document-level metadata attached to every chunk."""
    import datetime
    return {
        "source_file": Path(args.pdf).name,
        "title": getattr(args, "title", "") or None,
        "publisher": getattr(args, "publisher", "") or None,
        "extracted_at": datetime.date.today().isoformat(),
        "model": model,
        "page_offset": getattr(args, "page_offset", None),
    }


def add_meta_args(ap):
    """Shared CLI args for metadata, used by all three modes."""
    ap.add_argument("--title", default="",
                    help="document title, attached to every chunk")
    ap.add_argument("--publisher", default="",
                    help="publisher/department, attached to every chunk")
    ap.add_argument("--page-offset", dest="page_offset", type=int, default=None,
                    help="pdf_index - printed_page, where pdf_index = viewer page "
                         "number - 1. E.g. viewer page 106 shows printed '96': "
                         "105 - 96 = 9. Omit if the doc has no printed numbers.")
    ap.add_argument("--printed", default="",
                    help="select pages by PRINTED page number instead of "
                         "--pages, e.g. '96' or '96,120-150'. Needs --page-offset.")


def assemble_from_pages(pages: dict, out_dir: Path, n_pages: int, doc_meta=None):
    """pages: {page_no: {"markdown": str, "n_figures": int}}.
    Writes document.md, chunks.jsonl, document.json with per-page provenance
    (pdf_index, printed_page) and document-level metadata on every chunk.

    Pages with no record still get a `*[page N: not extracted]*` placeholder
    in document.md and an entry in document.json (with "extracted": false and
    "markdown": null), but are EXCLUDED from chunks.jsonl — a placeholder is
    not real content and shouldn't be embedded/retrieved as if it were."""
    doc_meta = doc_meta or {}
    offset = doc_meta.get("page_offset")
    title = doc_meta.get("title")
    src = doc_meta.get("source_file")

    md_parts, chunks, structured = [], [], []
    if title or src:
        md_parts.append(f"<!-- title: {title or ''} | source: {src or ''} | "
                        f"extracted: {doc_meta.get('extracted_at','')} -->\n")

    not_extracted = 0
    for page_no in range(n_pages):
        rec = pages.get(page_no)
        extracted = rec is not None
        md = (rec or {}).get("markdown") or f"*[page {page_no}: not extracted]*"
        n_figs = (rec or {}).get("n_figures", 0) or 0
        pp = printed_page(page_no, offset)
        pp_label = f"printed {pp}" if pp is not None else "no printed number"

        md_parts.append(
            f"\n\n<!-- pdf_index {page_no} | {pp_label} -->\n\n{md}")

        if extracted:
            chunk = {
                "pdf_index": page_no,
                "printed_page": pp,
                "text": md,
                "n_figures": n_figs,
            }
            chunk.update({k: v for k, v in doc_meta.items()
                          if k in ("source_file", "title", "publisher",
                                   "extracted_at", "model")})
            chunks.append(chunk)
        else:
            not_extracted += 1

        structured.append({"pdf_index": page_no, "printed_page": pp,
                           "markdown": md if extracted else None,
                           "n_figures": n_figs, "extracted": extracted})

    (out_dir / "document.md").write_text("".join(md_parts), encoding="utf-8")
    with (out_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    (out_dir / "document.json").write_text(
        json.dumps({"document": doc_meta, "n_pages": n_pages,
                    "pages": structured}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"Wrote document.md, chunks.jsonl, document.json to {out_dir}")
    if not_extracted:
        print(f"{not_extracted} pages not extracted — excluded from chunks.jsonl")
