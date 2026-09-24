# Changelog

## 2026-09-24 (f): `--printed` for the extractors too
Pre-change copy: `backup/ver8_before_printed_pages/`.
- `extractor.py` and `batch_extractor.py` accept `--printed 96,120-150` together with `--page-offset 9`, the same as compare.py.
- `--pages` accepts lists and ranges in every script (`2,105,180-182`). Before, the extractors only took a single `lo-hi` range.
  An out-of-range page now fails with a clear message.
- The progress lines show "printed 96 · pdf_index 105 (viewer 106)" when an offset is given.
- The batch retry hint now lists the exact failed pages (`--pages 3,17,42`) instead of a `lo-hi` range, so pages that already
  succeeded aren't re-billed. This closes an item from the original audit.
- The page-selection code now lives in one shared place (`common.parse_page_list`, `resolve_pages`, `page_label`), and compare.py uses it too.
- Added 3 tests (36 in total, all passing).

## 2026-09-24 (e): compare.py understands printed page numbers
- New `--printed 96,120-122` and `--page-offset 9`. Printed numbers are converted to `pdf_index` automatically
  (`printed + offset`), and `--printed` without an offset exits with a hint explaining how to work it out.
- `--pages` now accepts ranges (`180-182`) as well as lists. An out-of-range page now reports the valid `pdf_index` range.
- Report headers (HTML and MD) and the console show every page number a person might use:
  "printed 96 · pdf_index 105 (viewer 106)". `results.json` records `page_offset`, plus `printed_page` for each page.
- Added 5 tests (33 in total, all passing).

## 2026-09-24 (d): Page-offset docs fixed (off by one)
- The page-offset instructions in USER_MANUAL.md used the viewer's page number directly. Viewers count from 1, but
  `pdf_index` counts from 0, so the derived offset was one too high. The docs now say offset = (viewer page − 1) − printed page.
  For elder_manual.pdf: viewer 106 shows printed 96, so the offset is **9**, not 10. This was checked against the PDF's
  text layer. The `--page-offset` help text was fixed the same way. The code was already correct.
- Added how to target a printed page: `pdf_index = printed + offset` (printed 96 → `--pages 105`).

## 2026-09-24 (c): Gemini "API key not valid" fix

Pre-change copy: `backup/ver7_before_key_fix/`.
- **Keys in `.env` now override system environment variables** (`load_dotenv(..., override=True)` in common.py).
  Before this change, a key already stored in Windows' environment silently beat the one in `.env`.
- **The Gemini client now receives the key explicitly** (`GEMINI_API_KEY`, then `GOOGLE_API_KEY`). google-genai otherwise
  prefers `GOOGLE_API_KEY`, so a stray `GOOGLE_API_KEY` would be used instead of the key in `.env`.
- **New `check_keys.py`** shows which key each provider will use (masked to the last 4 characters) and where it came from.
  `--ping` tests each key with a free list-models call.
- Fixed `check_keys.py --ping` for Gemini. It failed with "client has been closed" because the client object was discarded
  before the model list finished loading. This was a bug in the checker only; extraction was not affected.

## 2026-09-24 (b): Model, pricing and cost-path update

Pre-change copy: `backup/ver6_before_model_update/`. Tests: `python -m unittest discover -s tests` → 28 passing.
Prices and model IDs come from the providers' pricing and docs pages as of 2026-09-24. The OpenAI prices come from the pricing table you pasted.

### New default models (verify on your own pages with compare.py first)
| Provider | Before | Now | Why |
|---|---|---|---|
| Anthropic | `claude-sonnet-4-6` ($3/$15) | `claude-sonnet-5` ($2/$10) | Cheaper per token, and reads the page at full detail (2,880 image tokens at 150 DPI vs ~1,550 downscaled). Similar total cost. |
| Gemini | `gemini-2.5-flash` ($0.30/$2.50) | `gemini-3.5-flash-lite` ($0.30/$2.50) | Same price, newer generation, fewer image tokens (1,120 vs 1,548). **Not yet tested on Thai tables.** |
| OpenAI | `gpt-4.1` ($2/$8) | `gpt-6-sol` ($2/$10) | Newest model at the same input price. **Not yet tested on Thai tables or Chat Completions.** |

Switch back per run with `--model`, e.g. `--model gemini-2.5-flash`.

### Changed
- **providers.py:** new `PRICING` table keyed by model ID. It covers Opus 5.5, Sonnet 5, Sonnet 4.6, Haiku 4.5
  (retiring no earlier than 2026-10-15), gpt-6-sol/luna, gpt-5.6-terra, gpt-5.4-mini, gpt-4.1(-mini), and Gemini 3.8-flash
  (promo price until 2026-12-31), 3.5/3.1-flash-lite and 2.5-flash(-lite). Added `BATCH_MULTIPLIER` (0.5 for all three providers).
- **Gemini thinking** is now set per model family. 2.5 Flash uses `thinking_budget=0`. Gemini 3 uses the lowest `thinking_level` each model
  accepts (`minimal`, or `low` for 3.7/3.8 Flash and Pro). Thinking tokens are billed as output, and they add little to transcription.
- **New `--effort low|medium|high`** (extractor, batch_extractor, compare). It maps to Anthropic `output_config.effort`, OpenAI
  `reasoning_effort` and Gemini 3 `thinking_level`. It is off by default for Anthropic and OpenAI, because a lower effort can also
  shorten the visible answer, so test it first.
- **New `--service-tier flex`** (extractor.py, OpenAI and Gemini only). It is billed at batch prices (~50% off) and uses the normal sync path.
  Requests are slow and can be refused with 429/503. The retry backoff goes up to 30 s, and the OpenAI timeout is 15 min. Anthropic is
  refused with a pointer to batch_extractor.py.
- **New `--workers N`** (extractor.py): API calls run in parallel threads. Rendering, figure extraction and all SQLite writes
  stay on the main thread. At most 2×N rendered pages are held in memory at once.
- **New `--model`** on extractor.py. In compare.py, `--providers` now accepts `provider:model` entries, so several models of one
  provider can be compared side by side.
- **estimate_cost.py rewritten:** image tokens are now calculated per model (Claude 28-px patches with high-res and standard caps, Gemini 3 fixed
  1,120, Gemini 2.5 tiles, OpenAI a rough proxy). Output is ×1.3 for the Claude 4.7+ tokenizer. It shows standard and batch/flex
  prices for every provider.
- **`MAX_IMAGE_B64_BYTES` raised from 4.5 MB to 9 MB.** The Claude API allows 10 MB per image, so pages stay lossless PNG instead of
  falling back to JPEG, which can blur Thai tone marks.
- extractor.py was refactored into `vision_with_retry` (no shared state), `record_result` (writes to the DB) and
  `extract_page_with_retry` (same behavior as before).
- USER_MANUAL.md and ARCHITECTURE.md were updated: new flags, cost sample, flex workflow, and why prompt caching isn't used.

### Decided against
- **Prompt caching:** each request is a unique page image plus a ~290-token instruction. There is no repeated prefix above the
  minimum cacheable length, so caching would save nothing. It would help only if the prompt grew with few-shot table examples.

## 2026-09-24 — Audit fixes (silent-failure bugs + size limits)

Pre-change copy of every file: `backup/ver5_before_audit_fixes/`.
Verify: `python -m unittest discover -s tests` → 20 tests, all passing (no API keys or network needed).

### Fixed — output that was silently wrong
- **Truncated pages no longer count as success.** All three backends now detect a cut-off response
  (Anthropic `stop_reason=max_tokens`, OpenAI `finish_reason=length`, Gemini `MAX_TOKENS`) and raise
  `TruncatedOutput`. *(providers.py)*
- **Empty responses no longer count as success** (e.g. a Gemini safety block). They raise `EmptyOutput`,
  so the page stays pending and is retried on the next rerun. Before, such pages were lost for good. *(providers.py)*
- Neither error is retried within the same run. That avoids paying for the same long output 4 times. *(extractor.py)*
- **Output ceiling raised from 4096 to 16384 tokens** in all modes. You only pay for tokens actually
  generated, so this costs nothing extra on normal pages. *(extractor.py, batch_extractor.py, compare.py)*
- **Gemini 2.5 Flash: thinking disabled** (`thinking_budget=0`). Thinking tokens were eating the output
  budget. Gemini also now uses `temperature=0`. *(providers.py)*
- **Placeholder pages are kept out of `chunks.jsonl`.** `*[page N: not extracted]*` stays in `document.md`,
  but is no longer embedded into RAG. `document.json` now has `"extracted": true/false` per page. *(common.py)*
- **`compare.py` now uses the production prompt** (`common.PROMPT`). Its own copy was missing the
  header/footer-stripping rule, so comparisons weren't testing real behavior. *(compare.py)*

### Fixed — size limits
- **Oversized page images fall back to JPEG.** If a PNG exceeds 4.5 MB base64, it is re-encoded as JPEG at
  quality 90, then 80, then 70. This keeps pages under the ~5 MB per-image limit (the largest page at 200 DPI measured 4.54 MB).
  Backends detect PNG vs JPEG from the data itself, so the backend function signature is unchanged. *(common.py, providers.py)*
- **Large batch jobs are split into multiple batches** (≤180 MB of image data and ≤10,000 requests each),
  because the Batches API has a 256 MB limit and this manual measured ~232 MB at 200 DPI. *(batch_extractor.py)*
- `batch.json` is saved after **each** batch is submitted, so a crash partway through never resubmits (and re-bills)
  a group that was already sent. New format: `batch_ids` / `submitted_pages` lists, plus the PDF name and size.
  Old single-`batch_id` files still resume.
- **Resuming with different settings is refused.** If `--pages`, `--dpi`, `--model` or the PDF differ from
  what's saved in `batch.json`, the script exits with an explanation instead of silently reusing the old batch.
- `failed_pages.json` now lists `errored`, `expired`, `truncated` and `empty` pages.

### Cleanup
- Removed the duplicate `save_figures` from extractor.py (it now uses `common.save_figures`).
- Batch requests are now built as plain dicts, so they no longer depend on `anthropic.types` classes.
- New `tests/test_fixes.py` covers resume, retry, truncation, placeholders, JPEG fallback,
  batch splitting, out-of-order results, and page-offset math.
- ARCHITECTURE.md and USER_MANUAL.md updated to match.

### Not changed yet (from the audit, still open)
- Retry treats auth/bad-model errors as transient; no fail-fast after repeated identical errors.
- Chunks record the last run's model rather than each page's actual model. (A `--model` flag now exists; see the 2026-09-24 (b) entry.)
- Sync mode only warns when the PDF hash changes. Batch retries still go to a new folder. (The retry hint is fixed; see the (f) entry.)
- Figure files aren't linked to `[FIGURE n]`. Vector charts aren't saved. (`--pages` lists are done; see the (f) entry.)
- `import fitz` is deprecated (use `import pymupdf`). (A git repo now exists.)
