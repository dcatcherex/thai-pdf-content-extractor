# Architecture & Implementation — Thai PDF Extractor

For developers (human or AI) who will modify, extend, or port this system. If you only want to *run* it, read `USER_MANUAL.md`.

---

## 1. The core problem and why the design follows from it

The source documents are Thai PDFs whose **text layer is corrupted**: font/cmap mappings scramble Thai combining characters (vowels, tone marks). Text extraction returns `คู่่ มืือ` where the page reads `คู่มือ`. This is not a normalization bug you can fix downstream — the character sequence is genuinely wrong.

**Verified on the reference document (364-page Thai government manual):**
- `pdfplumber` text extraction → scrambled, unusable.
- `PyMuPDF` render to PNG at 150 DPI → **visually pristine**, fully legible Thai.

**Therefore: rendering + vision transcription is the PRIMARY path, not a fallback.** Every other design decision descends from this:

| Consequence | Design response |
|---|---|
| One API call per page | Cost and duration scale with page count → need cost estimation and a cheap batch path |
| API calls fail transiently | Need per-page retry with backoff |
| A 364-page run is long | Need durable checkpointing and resume |
| Vision quality varies by model | Need a provider comparison harness |
| Vision output is free text | Need prompt discipline + structured assembly |

Do **not** "optimize" this by reintroducing text-layer extraction as the primary path. It was tested and it fails on this document class.

---

## 2. Module map

```
common.py           Shared core. Prompt, .env loading (.env overrides system
                    env), rendering, figure extraction, output assembly,
                    page-offset math + page selection (parse_page_list,
                    resolve_pages, page_label), shared CLI args.
                    ← Both extractors depend on this. Single source of truth
                      for output format.

providers.py        Vision backends (anthropic/openai/gemini) behind one
                    interface + the PRICING table.
                    ← The ONLY provider-specific code in the system.

extractor.py        Sync mode. Per-page loop, SQLite checkpointing, retry.
                    Multi-provider. Live progress.

batch_extractor.py  Batch mode. Anthropic Message Batches API (50% cost).
                    Async submit (split across multiple batches if needed) →
                    poll → retrieve. batch.json checkpointing after each batch.

compare.py          Quality harness. Same pages through N providers,
                    emits HTML/MD/JSON report.

estimate_cost.py    Pre-flight cost estimate. Per-model image-token rules.

check_keys.py       Shows which API key each provider will use (masked) and,
                    with --ping, tests each one with a free list-models call.
```

**Dependency direction:** `extractor` / `batch_extractor` / `compare` / `estimate_cost` → `common` + `providers`. Nothing depends on the extractors. Keep it that way.

---

## 3. Key contracts

### 3.1 Vision backend interface (`providers.py`)

Every backend is a function with this exact signature:

```python
def call_X(img_b64: str, prompt: str, max_tokens: int) -> str:
    """Takes a base64 image (PNG, or JPEG if common.render_page_b64() had to
    fall back — see §3.2) + prompt. Returns transcribed Markdown.
    Raises on failure — the CALLER owns retry."""
```

Registered in `BACKENDS: dict[str, Callable]`, retrieved via `get_backend(provider)`.

**To add a provider:** write one function matching that signature, add it to `BACKENDS`, add a default model to `DEFAULT_MODELS`, and add prices to `PRICING`. Nothing else in the system changes. This is the intended extension point.

**Backends must not retry internally.** Retry lives in the callers (`extractor.vision_with_retry`, `compare.run_one`) so policy is consistent and observable.

**Run-wide knobs are environment variables** so the backend signature never changes:
`EXTRACTOR_MODEL` (`--model`), `EXTRACTOR_EFFORT` (`--effort`), `EXTRACTOR_SERVICE_TIER`
(`--service-tier flex`). The CLIs set them; backends read them.

**API keys:** `common.load_env()` loads `.env` with `override=True`, so `.env` beats a stale key in
the system environment. `call_gemini` passes the key explicitly (`GEMINI_API_KEY`, then
`GOOGLE_API_KEY`), because google-genai would otherwise prefer `GOOGLE_API_KEY`.

**Truncation and empty output are failures, not partial successes.** Every backend routes its
result through `providers.check_output(text, truncated)`, which raises `TruncatedOutput` if the
model was cut off at `max_tokens` (Anthropic: `stop_reason == "max_tokens"`; OpenAI:
`finish_reason == "length"`; Gemini: candidate `finish_reason` ending in `MAX_TOKENS`) and
`EmptyOutput` if the text is blank. A truncated transcription silently drops the end of a page —
treating it as a success would ship an incomplete chunk with no signal that anything is missing.
These two errors are **not retried within a run** (an immediate retry would regenerate and re-bill
up to `MAX_TOKENS` of output for the same result); the page is marked `failed` and picked up on the
next rerun. Gemini is called with `temperature=0` and the lowest thinking setting each family
accepts (`providers.gemini_thinking_config`: `thinking_budget=0` on 2.5 Flash; `thinking_level`
`minimal`, or `low` on 3.7/3.8 Flash and Pro), because thinking tokens count against
`max_output_tokens`. Anthropic and OpenAI keep default sampling settings, since some newer models
reject non-default values.

**Media type is sniffed, not hardcoded.** `providers.media_type_of(img_b64)` looks at the base64
prefix (`iVBOR` → PNG, `/9j/` → JPEG) so a backend sends the right `media_type`/`mime_type`
regardless of whether `common.render_page_b64()` used PNG or fell back to JPEG.

### 3.2 Page record (the unit of work)

```python
{page_no: {"markdown": str, "n_figures": int}}
```

**Rendering** goes through `common.render_page_b64(doc, page_no, dpi)` (an alias
`render_page_png_b64` is kept for backward compatibility). It renders PNG first; if the base64
would exceed `common.MAX_IMAGE_B64_BYTES` (9,000,000 — under the Claude API's 10 MB per-image limit;
the largest page of the reference doc is 4.5 MB even at 200 DPI, so PNG is almost always kept), it re-encodes as JPEG at quality 90, then 80, then 70, returning the smallest attempt
if even q70 is still over the limit. This exists because providers reject or truncate oversized
images; a lossy JPEG that arrives intact beats a PNG that doesn't.

Both modes produce this dict and hand it to `common.assemble_from_pages()`. That function is the **single writer** of all output files — which is why sync and batch modes produce byte-identical output formats. If you add a third mode, produce this dict; do not write output files yourself.

### 3.3 Output schema

`chunks.jsonl` — one object per page:

```json
{
  "pdf_index": 24,          // 0-based position in file; use to fetch page image
  "printed_page": 14,       // number printed on the page; null for front matter
  "text": "...",            // body Markdown, page furniture stripped
  "n_figures": 1,
  "source_file": "doc.pdf", // ─┐
  "title": "...",           //  │ document-level metadata,
  "publisher": "...",       //  │ attached to EVERY chunk
  "extracted_at": "2026-07-12", // │ (for RAG provenance)
  "model": "claude-sonnet-5"    // ─┘
}
```

Pages that were never extracted (never attempted, or attempted and still failing at the
end of a run) get a `*[page N: not extracted]*` placeholder in `document.md` and a `document.json`
entry with `"extracted": false, "markdown": null` — but they are **excluded from `chunks.jsonl`**,
since a placeholder string is not real content and shouldn't be embedded/retrieved as if it were.
`assemble_from_pages()` prints a count of how many pages were excluded this way.

`document.json` — `{"document": {...meta}, "n_pages": int, "pages": [{"pdf_index", "printed_page", "markdown", "n_figures", "extracted"}, ...]}`
`document.md` — pages joined with `<!-- pdf_index N | printed M -->` markers.

---

## 4. Data flow

### Sync mode (`extractor.py`)

```
init SQLite (pages table: page_no, status, attempts, markdown, provider, error)
   ↓
pending_pages() ∩ resolve_pages(--pages / --printed)  ──►  for each page:
     render @dpi (common.render_page_b64)                 [main thread]
     vision_with_retry(): call_vision, backoff, ≤4 tries  [worker thread if --workers > 1]
     record_result(): save figures, UPDATE pages ...      [main thread]
   ↓
assemble() → build page dict → common.assemble_from_pages()
```

With `--workers N` (`run_parallel`), only the API calls run in threads; rendering, figure
extraction and every SQLite write stay on the main thread, and at most 2×N rendered pages are in
memory.

**Resume mechanism:** the SQLite `pages` table. `status != 'done'` defines the work queue. Rerunning the same command re-queries and continues. Partial artifacts are written even mid-run so progress is usable.

**Cross-provider splitting** works because provider is a per-run flag but state is per-page in a shared DB. Ranges assigned to different providers merge into one output set.

### Batch mode (`batch_extractor.py`)

```
if out/batch.json exists:  load it, upgrade old single-batch_id format if needed
                           check pages/dpi/model/pdf match → else exit non-zero
else:                      fresh state
   ↓
render remaining pages sequentially, grouping into batches that stay under
MAX_BATCH_BYTES of base64 image data (and MAX_REQUESTS_PER_BATCH count) ──►
    submit ONE batch per group, custom_id = "page_NNNN"
    persist batch_ids + submitted_pages → out/batch.json   ← AFTER EACH GROUP,
                                                               before anything
                                                               else can fail
   ↓ (unless --no-wait)
poll every batch until processing_status == "ended"
   ↓
stream results from every batch  ──►  custom_id → page_no  (ORDER IS NOT
                                       GUARANTEED, within or across batches)
                     succeeded, stop_reason == "max_tokens" → truncated list
                     succeeded, blank text                  → empty list
                     succeeded, otherwise                   → page record
                     errored/expired                        → their own lists
                     (all four lists → failed_pages.json)
   ↓
common.assemble_from_pages()
```

**Why multiple batches:** the Anthropic Batches API caps a single batch's request body at 256 MB.
A 364-page document at 200 DPI runs ~232 MB of base64 image data alone, so one batch isn't always
enough. `batch_extractor.py` renders pages sequentially and starts a new batch (`MAX_BATCH_BYTES =
180_000_000`, with headroom for per-request JSON overhead) whenever the running total would exceed
it, also capping at `MAX_REQUESTS_PER_BATCH = 10_000` requests. It never holds more than one
group's rendered pages in memory at a time.

**`batch.json` format:**
```json
{
  "batch_ids": ["msgbatch_1", "msgbatch_2"],
  "submitted_pages": [[0, 1, 2, "... batch 0's pages"], [3, 4, "... batch 1's pages"]],
  "pages": [0, 1, 2, 3, 4],
  "model": "...", "dpi": 150, "n_pages": 364,
  "pdf_name": "doc.pdf", "pdf_size": 25868399,
  "submitted_at": "2026-07-12 10:00:00"
}
```
An older `batch.json` with a single `"batch_id"` is transparently upgraded on load (`batch_ids =
[batch_id]`, `submitted_pages = [pages]`) — old resume flows still work.

**Critical invariants:**
1. **`batch.json` is persisted after EACH batch submission**, not just at the end. It is the only
   thing standing between a mid-submission crash and a duplicate (double-billed) submission of a
   group already sent — the next run only submits pages not yet in `submitted_pages`.
2. **Results must be matched by `custom_id`, never by array position.** The Batches API explicitly
   does not guarantee order, within a batch or across batches.
3. **A resume checks that `pages`/`dpi`/`model`/the PDF's filename+size match** what's stored, and
   refuses (non-zero exit, clear message) rather than silently reusing a mismatched `batch.json`.
4. Submission is the only step requiring the local machine to stay up. After `--no-wait` prints the
   batch ids, the job is server-side.

---

## 5. Page numbering: `pdf_index` vs `printed_page`

Two different numbers, both needed, frequently conflated.

- **`pdf_index`** — 0-based position in the file. What code needs to fetch a page.
- **`printed_page`** — the number printed on the paper. What a *human* needs to find the passage in the physical book.

They differ because front matter (cover, foreword, TOC) precedes printed page 1.

**Current implementation — fixed offset:**

```python
def printed_page(pdf_index, offset):
    n = pdf_index - offset
    return n if n >= 1 else None   # front matter → None
```

User supplies `--page-offset`, derived once: `offset = pdf_index − printed_page`, where
`pdf_index = viewer page − 1` (viewers count from 1). Example (reference doc): viewer page 106 shows
"96" → `105 − 96 = 9`.

**Selecting pages by printed number:** every script accepts `--printed 96,120-150` with
`--page-offset`; `common.resolve_pages()` converts with `pdf_index = printed + offset`, validates
the range, and `--printed` overrides `--pages`. `common.page_label()` renders
"printed 96 · pdf_index 105 (viewer 106)" for progress lines and compare reports.

**Known limitation:** assumes a single constant offset for the whole document. Books that **restart numbering** mid-way (new part resets to 1, roman-numeral front matter) break this. Two ways to extend:
- Accept a piecewise map: `--page-offset "0-23:none,24-200:10,201-364:50"`.
- Have the vision model read the printed number off each page and return it as structured output (handles arbitrary schemes; costs a bit of prompt complexity and can misread).

If you implement either, keep `printed_page` nullable — front matter genuinely has no number.

---

## 6. Prompt design (`common.PROMPT`)

The prompt does four jobs. Changing it changes output for every mode, so change it deliberately.

1. **Faithful Thai transcription** — "the page image is the source of truth."
2. **Tables as real Markdown tables** — explicitly forbids flattening to prose. *This is the highest-variance requirement across models.*
3. **Figures described inline** — `[FIGURE n: <Thai description>]`. Description carries the *meaning* into the text channel; the image file preserves the visual.
4. **Strip page furniture** — running header/footer (page number, book title, publisher line) is excluded from the body.

**Why furniture-stripping matters:** on a 364-page manual the footer repeats 364 times. Left in, it dilutes embeddings and surfaces as retrieval noise. It's safe to strip *because* `printed_page` comes from the offset, not from reading the footer — no information is lost.

---

## 7. Failure handling

| Layer | Mechanism |
|---|---|
| Transient API error (sync) | `vision_with_retry`: up to 4 attempts, exponential backoff (2s → 4s → 8s; base 30s with `--service-tier flex`) |
| Transient API error (compare) | `run_one`: retries on 503/429/overload/timeout keyword match |
| Truncated output (`max_tokens`) | `providers.TruncatedOutput`, raised by every backend via `check_output()`. Sync: not retried within the run (would re-bill); page stays failed and is retried on rerun. Batch: routed to a `truncated` list, never into `pages`. Compare: reported as an error (`TruncatedOutput: ...`), not retried. |
| Empty output | `providers.EmptyOutput`, same handling as truncation in each mode. |
| Permanent page failure (sync) | Row marked `status='failed'` with error text; **does not block other pages**; rerun retries it |
| Permanent page failure (batch) | Written to `failed_pages.json` — `errored`, `expired`, `truncated`, and `empty` lists — plus one printed resubmit command listing exactly those pages (`--pages 3,17,42`) |
| Process crash / power loss | Sync: SQLite checkpoint. Batch: `batch.json` (batch_ids + submitted_pages, persisted after every batch submission). Both: rerun same command. |
| OpenAI param drift | `call_openai` tries `max_completion_tokens`, falls back to `max_tokens` (newer models renamed it) |

**Design principle:** a single page's failure must never abort the run. Failures are recorded and isolated, and the batch API's own semantics agree — one failed request doesn't affect others in the batch.

---

## 8. Cost model (`estimate_cost.py`)

```
image_tokens = per-model rule (estimate_cost.image_tokens):
  Claude 4.7+ (Sonnet 5, Opus 5.x):  ceil(w/28)·ceil(h/28), after downscale to ≤2576 px / ≤4784 tok
  Claude Sonnet 4.6, Haiku 4.5:      same patches, ≤1568 px / ≤1568 tok
  Gemini 3.x:                        ~1120 per image (default media resolution)
  Gemini 2.5:                        258 per 768×768 tile
  OpenAI:                            (w·h)/750 — rough proxy only
input_per_page = image_tokens + prompt_tokens
total_cost = (total_input/1e6 × price_in) + (est_output × tokenizer_factor/1e6 × price_out)
```

Measured on the reference doc at 150 DPI (1252×1778 px): Sonnet 5 **2,880** image tokens,
Sonnet 4.6 **~1,550** (downscaled), Gemini 3 **1,120**, Gemini 2.5 **1,548**. Claude 4.7+ models
use a newer tokenizer (~30% more tokens for the same text), so their output estimate is ×1.3.

**Output tokens are estimated, not measured** (default range 750–1100/page; Thai is token-heavy). Input dominates, so totals are reasonably stable, but treat figures as approximate.

**The `max_tokens` request ceiling (`MAX_TOKENS`, defined per-script) is 16,384**, well above the
estimated range above. Providers only bill for tokens actually generated, so a generous ceiling
costs nothing extra on a typical page — it exists purely to stop a rare, unusually dense Thai table
from hitting the ceiling and silently truncating (see §7).

**Cheap paths (all ~50% off, `providers.BATCH_MULTIPLIER`):** Anthropic Message Batches
(`batch_extractor.py`); OpenAI and Gemini **flex tier** (`extractor.py --service-tier flex`, billed at
their batch prices, synchronous API but slow and sheddable — use `--workers`). OpenAI/Gemini batch
APIs would cost the same as flex; they're not implemented because flex reuses the sync path.

**Prompt caching is deliberately not used.** Each request = one unique page image + a ~290-token
instruction. There is no repeated prefix above any provider's minimum cacheable length, so caching
would save nothing (and cache writes cost 1.25–2× on Anthropic). It becomes worthwhile only if the
prompt grows (e.g. few-shot table examples of several thousand tokens): put that shared text first
as the cached prefix, then the page image. Caching and batch discounts stack.

**Thinking/effort.** Thinking tokens are billed as output and count against `max_tokens`.
Gemini defaults to its lowest thinking level (`providers.gemini_thinking_config`); Anthropic and
OpenAI keep their defaults unless `--effort` is passed, because Anthropic's effort also shapes the
visible answer and needs testing (`compare.py --effort low`) before relying on it.

`PRICING` in `providers.py` is a hardcoded snapshot. **It will go stale.** Verify against provider pricing pages before large runs.

---

## 9. Known limitations & good next steps

**Limitations (be honest about these):**
- Page-offset assumes constant numbering (see §5).
- Output-token cost is an estimate.
- `PRICING` and `DEFAULT_MODELS` are snapshots that drift as providers ship models.
- Batch mode is Anthropic-only (OpenAI/Gemini get the same discount via `--service-tier flex`).
- Table transcription quality varies significantly by model — **the main quality risk.** Weaker models emit a table frame with empty cells. Always validate with `compare.py` on table-heavy pages before a full run.
- Chunking is one-chunk-per-page. Fine as a baseline; semantic/section chunking would likely retrieve better.

**Sensible extensions, roughly in value order:**
1. **Semantic chunking** — split by heading rather than page boundary; keep `pdf_index`/`printed_page` as a range on each chunk.
2. ~~OpenAI + Gemini batch APIs~~ — covered by the flex tier (same price).
3. **Piecewise page offsets** (§5).
4. ~~Parallel sync workers~~ — done: `--workers N` (`extractor.run_parallel`; API calls in threads, rendering and SQLite writes on the main thread).
5. **Table-quality validator** — auto-detect empty-cell tables in output and flag/reprocess those pages on a stronger model. This directly targets the top quality risk.
6. **Package as a `.skill`** — wrap with a `SKILL.md` for conversational invocation. Core logic needs no changes.

---

## 10. Testing notes

There's a `unittest` suite in `tests/` (`test_fixes.py`, `test_models_and_tiers.py`; 36 tests). Run it from the tool's folder root:

```bash
python3 -m unittest discover -s tests
```

It needs no API keys, no provider SDKs, and no network — `providers.BACKENDS` is monkeypatched
with stub functions, and `batch_extractor` is exercised against a small fake Anthropic-batches
client (plain objects, no `anthropic` import required). It covers:

- **`check_output`:** raises `TruncatedOutput` / `EmptyOutput` as appropriate.
- **Retry (sync):** a stub raising `TruncatedOutput` leaves the page `status='failed'` and pending;
  a stub returning text marks it `done`.
- **Assembly:** `assemble_from_pages` skips missing pages in `chunks.jsonl` but keeps the
  `document.md` placeholder and `document.json`'s `extracted: false`.
- **`compare.py`:** has no local `PROMPT` (uses `common.PROMPT`); `run_one` reports
  truncated/empty as an error rather than retrying.
- **Image fallback:** `render_page_b64` falls back to JPEG over the size limit;
  `media_type_of` sniffs PNG vs. JPEG correctly.
- **Batch splitting/resume:** submission splits into multiple batches under a small
  `MAX_BATCH_BYTES`; `batch.json` persists every `batch_id`; a rerun with identical args doesn't
  call `create()` again; a rerun with different `--dpi` exits non-zero; out-of-order results map to
  the right pages; a `stop_reason == "max_tokens"` result goes to `truncated`, not `pages`; an old
  single-`batch_id` `batch.json` still resumes.
- **Offset math:** `printed_page(24, 10) == 14`; `printed_page(5, 10) is None`.
- **Models/tiers (`test_models_and_tiers.py`):** Gemini thinking levels per family; every default
  model is priced; per-model image-token rules; `compare` `provider:model` entries restore the env;
  `--workers` keeps DB writes on the main thread and isolates a failing page; flex is refused for
  Anthropic.
- **Page selection:** `resolve_pages` (printed → pdf_index, ranges, bounds, missing offset);
  `compare --printed` end to end; `extractor --printed` processes only those pages;
  `batch_extractor --printed` submits only those pages.

Stub `providers.BACKENDS` to test everything without API keys or network — that's the seam the design exists to give you. Remaining gaps worth adding: resume correctness for sync mode
(interrupt mid-run, rerun, assert only pending pages are reprocessed) and full failure isolation
(one errored page must not prevent the others from assembling) — both straightforward extensions
of the fixtures already in `tests/test_fixes.py`.
