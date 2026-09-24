# User Manual — Thai PDF Extractor

A tool that turns Thai-heavy PDFs (with tables and illustrations) into clean, AI-ready content. Written for people who will *run* the tool. If you're going to *modify* it, read `ARCHITECTURE.md` instead.

---

## 1. What this tool solves

Many Thai PDFs have a **corrupted text layer**. Copying text out gives you scrambled vowels and tone marks:

```
What's in the PDF text layer:   คู่่ มืือ      ← garbage, unusable
What's actually on the page:    คู่มือ        ← correct
```

You cannot fix this by extracting text harder. But the **rendered page image is perfectly clean**. So this tool renders each page to an image and has a vision AI model read it — which sidesteps the broken text layer entirely.

**You get:** correct Thai text, tables as real Markdown tables, illustrations both described in words *and* saved as image files, all with page-level provenance so you can cite sources.

---

## 2. Install

```bash
pip install -r requirements.txt        # everything (all three providers)

# or only what you need:
pip install pymupdf python-dotenv      # always required
pip install anthropic                  # for Claude
pip install openai                     # for GPT
pip install google-genai               # for Gemini (keep it up to date: newer
                                       # thinking/flex settings need a recent version)
```

### API keys

Copy `.env.example` to `.env` in the same folder as the scripts and fill it in:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

All scripts load this automatically. You only need keys for providers you use. Keys in `.env`
**override** any same-named key saved in your system (Windows) environment.

**Check your keys** before a run:

```bash
python check_keys.py          # which key each provider will use (last 4 chars) and where it came from
python check_keys.py --ping   # also asks each provider whether the key works (free call)
```

**Important:** `.env` is already listed in `.gitignore`, so it is never committed. Keep it that way.

Where to get keys:
| Provider | Get key from |
|----------|--------------|
| Anthropic | console.anthropic.com |
| OpenAI | platform.openai.com |
| Gemini | aistudio.google.com |

---

## 3. The scripts

| Script | What it does | When to use |
|--------|--------------|-------------|
| `estimate_cost.py` | Prints what a run will cost | **Always run first** |
| `compare.py` | Runs the same pages through multiple providers side by side | Before choosing a provider |
| `extractor.py` | Extracts pages one at a time, live | Testing, small jobs, splitting cost across providers |
| `batch_extractor.py` | Submits all pages as one async job at **50% off** | Large documents where you can wait |
| `check_keys.py` | Shows/tests which API key each provider uses | Setup, or when you get an "API key" error |

---

## 4. Recommended workflow

### Step 1 — Find your page offset

PDFs usually have a cover, foreword, and table of contents before printed page 1. So the PDF's page *index* runs ahead of the number *printed* on the page.

Open the PDF in any viewer and look at one page. **Viewers count pages from 1, but this tool
counts from 0** (`pdf_index`), so subtract 1 from the viewer's number:

```
Viewer shows:        page 106 of 364   →  pdf_index = 106 − 1 = 105
Page footer prints:  96

offset = pdf_index − printed = 105 − 96 = 9
```

You'll pass `--page-offset 9`. This lets the tool record both numbers, so a citation can say "หน้า 96" (what a human looks up) while the code still knows it's `pdf_index` 105.

**`--pages` also uses `pdf_index`.** To extract or compare the page printed as 96:
`pdf_index = printed + offset = 96 + 9 = 105` → `--pages 105`.
Or let the tool convert for you: every script accepts `--printed 96 --page-offset 9`
(ranges and lists work too: `--printed 96,120-150`).

If your document has no printed page numbers, skip this flag.

### Step 2 — Estimate the cost

```bash
python estimate_cost.py doc.pdf
```

Sample output for the 364-page manual at 150 DPI (prices checked 2026-09-24):

```
ANTHROPIC
  model                    img tok/pg           standard         BATCH (-50%)
  claude-opus-5-5               2,880      $11.71–$15.02          $5.85–$7.51
  claude-sonnet-5               2,880        $5.85–$7.51          $2.93–$3.76
  claude-sonnet-4-6             1,551        $6.10–$8.01          $3.05–$4.01
  ...
GEMINI
  gemini-3.5-flash-lite         1,120        $0.84–$1.15          $0.42–$0.58
  gemini-2.5-flash              1,548        $0.88–$1.20          $0.44–$0.60
  ...
```

Each provider counts image tokens differently, so the script computes them per model.
Note the "img tok/pg" column: Sonnet 4.6 downscales the page to ~1,550 tokens, while Sonnet 5
reads it at full 150 DPI detail (~2,880) for about the same total price.

Options: `--dpi 200` (more detail for Claude 4.7+ models, ~65% more image tokens; no effect on
Gemini 3, which uses a fixed ~1,120 tokens/image, or on Sonnet 4.6/Haiku, which downscale anyway),
`--pages 0-199` (cost of one range).

### Step 3 — Compare provider quality

**Do not skip this.** Cheapest is not cheapest if the output is unusable.

```bash
python compare.py doc.pdf --pages 2,182,200 --dpi 200

# or by the page number printed on the paper (converted with the offset):
python compare.py doc.pdf --printed 96,120-122 --page-offset 9

# compare several models of one provider side by side (provider:model):
python compare.py doc.pdf --pages 2,182 \
  --providers anthropic,gemini:gemini-3.5-flash-lite,gemini:gemini-2.5-flash
```

Pick pages that have **dense tables** — that's where providers differ most. Open the generated `report.html` in a browser and look at the side-by-side output.

What to look for:
- Are the Thai characters correct?
- **Are the table cells filled in, or just empty dashes?** (Weaker models often draw the table frame but fail to transcribe the contents.)
- Was the repeating page footer correctly left out?

### Step 4 — Run the extraction

**Option A — Batch mode (cheapest, Anthropic only):**

```bash
python batch_extractor.py doc.pdf --out ./out_batch \
  --title "คู่มือผู้ดำเนินการฯ" \
  --publisher "กรมสนับสนุนบริการสุขภาพ" \
  --page-offset 9
```

**Option B — Sync mode (live, any provider, can split cost):**

```bash
python extractor.py doc.pdf --out ./out --provider anthropic \
  --title "คู่มือผู้ดำเนินการฯ" --page-offset 9
```

**Option C — Flex tier (OpenAI/Gemini at ~50% off, no batch plumbing):**

```bash
python extractor.py doc.pdf --out ./out --provider gemini \
  --service-tier flex --workers 8 --title "คู่มือผู้ดำเนินการฯ" --page-offset 9
```

Flex requests can take minutes each (Gemini targets 1–15 min) and may be refused with 429/503
when capacity is short (you aren't charged for those; the page is retried). Use `--workers` so
pages run in parallel, and rerun the same command to pick up any failed pages.

---

## 5. Batch mode — fire and forget

Batch mode submits everything as one asynchronous job to Anthropic at **half price**. You don't have to sit and watch it.

```bash
# Tonight — submit and walk away:
python batch_extractor.py doc.pdf --out ./out_batch --no-wait

# Wait for "Submitted 1 batch(es): ['msgbatch_...']" to print, THEN you can shut down.

# In the morning — same command, without --no-wait:
python batch_extractor.py doc.pdf --out ./out_batch
```

**How it survives your computer being off:** the job runs on Anthropic's servers. The batch id(s) are saved to `out_batch/batch.json`, so rerunning reconnects to those same jobs and downloads results. It never resubmits, so you're never charged twice.

**Large documents may submit as more than one batch.** The Batches API caps a single batch at
256 MB of request data, and a big document at high DPI can exceed that on its own. When it does,
`batch_extractor.py` automatically splits the pages across several batches, saving `batch.json`
after each one submits — so even a crash partway through a multi-batch submission never resubmits
a batch that already went out. You don't need to do anything differently; `--no-wait` still exits
once everything has been submitted, and the follow-up run still polls and retrieves all of them.

**Timing:** most batches finish in under an hour, but the limit is 24 hours. Results stay downloadable for 29 days. So "in the morning" is safe. A batch that doesn't finish within 24 hours expires — those pages get logged for resubmission.

**Before a fire-and-forget run**, do a tiny test so you don't discover an auth typo in the morning:

```bash
python batch_extractor.py doc.pdf --out ./test --pages 0-1 --no-wait
```

Confirm it prints `Submitted 1 batch(es)` and creates `test/batch.json`.

---

## 6. Splitting cost across providers (sync mode)

If you have prepaid credit with several providers, assign page ranges to each. They all write to the **same output files** and merge automatically.

```bash
python extractor.py doc.pdf --out ./out --provider anthropic --pages 0-120
python extractor.py doc.pdf --out ./out --provider openai    --pages 121-242
python extractor.py doc.pdf --out ./out --provider gemini    --pages 243-363
```

A sensible strategy: put the **table-heavy sections on your strongest model** and the plain-prose sections on the cheapest one.

---

## 7. If it stops or crashes

**Both modes resume. Just run the same command again.**

| Mode | How resume works |
|------|------------------|
| Sync | A SQLite file (`out/state.db`) tracks each page's status. Rerunning skips pages already done and retries the rest. |
| Batch | The batch id(s) in `out_batch/batch.json` are reused. It reconnects and downloads — no resubmission, no double charge. |

Individual pages that fail permanently are recorded (sync: in `state.db`; batch: in `failed_pages.json`) and do **not** block the other pages. `failed_pages.json` covers four cases: `errored`, `expired`, `truncated` (the model hit the output limit mid-page), and `empty` (the model returned nothing) — all four just need resubmitting the same way.

**If you rerun `batch_extractor.py` with a different `--dpi`, `--model`, `--pages`, or against a
different PDF than the `batch.json` in that `--out` folder was created with, it refuses to run**
and tells you what doesn't match, rather than silently mixing results from two different requests.
Use a fresh `--out` for a genuinely different run.

---

## 8. Your output files

Everything lands in the `--out` folder:

| File | What it is |
|------|-----------|
| `document.md` | The full document as Markdown — read it, or feed it to an AI |
| `chunks.jsonl` | One JSON object per page — ready to load into a RAG / embedding pipeline |
| `document.json` | Structured records plus document-level metadata |
| `figures/` | Every illustration saved as an image file |
| `state.db` / `batch.json` | Resume data — safe to keep, safe to delete once finished |

**A note on page images:** pages are normally rendered as PNG (lossless — best for dense Thai text
and tables). If a page's rendered PNG would be too large to send a provider (rare — usually only a
very image-heavy page at high DPI), the tool automatically falls back to JPEG at decreasing quality
until it fits. This is automatic and needs no action from you; it only affects the image sent to
the vision model, not anything saved to disk.

### What's in each chunk

```json
{
  "pdf_index": 24,
  "printed_page": 14,
  "text": "## เนื้อหา\n\n...",
  "n_figures": 1,
  "source_file": "doc.pdf",
  "title": "คู่มือผู้ดำเนินการฯ",
  "publisher": "กรมสนับสนุนบริการสุขภาพ",
  "extracted_at": "2026-07-12",
  "model": "claude-sonnet-5"
}
```

- **`pdf_index`** — position in the PDF file. Use this to fetch the page image.
- **`printed_page`** — the number printed on the paper. Use this when citing to a human. It's `null` for front matter.

**Pages that couldn't be extracted are left out of `chunks.jsonl`.** If a page permanently failed
(or a run was stopped before it finished), `document.md` still shows a `*[page N: not extracted]*`
placeholder in its place and `document.json` still has an entry for it (with `"extracted": false`
and `"markdown": null`) — but it is **not** written to `chunks.jsonl`, since that placeholder text
isn't real content and shouldn't be embedded or retrieved as if it were. The tool prints a count
of how many pages this happened to.

### Illustrations are handled two ways

1. **Described in the text**, inline: `[FIGURE 1: แผนภาพแสดงขั้นตอนการดูแล]` — so the *meaning* is searchable.
2. **Saved as a file**: `figures/page0024_fig1.png` — so you keep the original visual.

### Page furniture is removed

The repeating footer (page number + book title + department) is **stripped from the body text**. If left in, that boilerplate would repeat hundreds of times and pollute your embeddings. The printed page number is recovered from `--page-offset` instead, so nothing is lost.

---

## 9. Full flag reference

**Shared by `extractor.py` and `batch_extractor.py`:**

| Flag | Default | Meaning |
|------|---------|---------|
| `--out` | varies | Output folder |
| `--dpi` | `150` | Render resolution. Use `200` for dense tables. |
| `--pages` | all | `pdf_index` pages (0-based = viewer page − 1): range `0-199`, list/ranges `2,105,180-182` |
| `--printed` | — | Select by **printed** page number instead, e.g. `96` or `96,120-150`. Needs `--page-offset`; overrides `--pages` |
| `--title` | — | Document title, attached to every chunk |
| `--publisher` | — | Publisher, attached to every chunk |
| `--page-offset` | — | `pdf_index − printed_page` (see §4.1) |

**`extractor.py` only:**

| Flag | Default | Meaning |
|------|---------|---------|
| `--provider` | `anthropic` | `anthropic`, `openai`, or `gemini` |
| `--model` | provider default | Model ID, e.g. `gemini-2.5-flash` (defaults: `claude-sonnet-5`, `gpt-6-sol`, `gemini-3.5-flash-lite`) |
| `--effort` | provider default | `low`/`medium`/`high`: Anthropic effort, OpenAI reasoning effort, Gemini 3 thinking level. Gemini defaults to its lowest thinking level. |
| `--service-tier` | `standard` | `flex` = OpenAI/Gemini flex tier (~50% off, slower) |
| `--workers N` | `1` | Parallel API calls. Mind your rate limits. |
| `--limit N` | all | Process at most N pending pages this run |

**`batch_extractor.py` only:**

| Flag | Default | Meaning |
|------|---------|---------|
| `--model` | `claude-sonnet-5` | Anthropic model to use |
| `--effort` | model default | `low`/`medium`/`high` — fewer output/thinking tokens at lower levels; test with `compare.py --effort` first |
| `--no-wait` | off | Submit and exit; rerun later to retrieve |

**`compare.py`:**

| Flag | Default | Meaning |
|------|---------|---------|
| `--count` | `3` | How many pages |
| `--start` | `0` | First page |
| `--pages` | — | `pdf_index` list/ranges, e.g. `2,105,180-182` (overrides count/start) |
| `--printed` | — | Printed page numbers instead, e.g. `96,120-122`; needs `--page-offset` |
| `--page-offset` | — | Same as the extractors; also labels the report "printed 96 · pdf_index 105 (viewer 106)" |
| `--providers` | all three | Comma list of `provider` or `provider:model` |
| `--effort` | — | Apply one effort/thinking level to every entry |
| `--out` | `./comparisons` | Timestamped subfolder is created |

**`estimate_cost.py`:**

| Flag | Default | Meaning |
|------|---------|---------|
| `--dpi` | `150` | Resolution to price |
| `--pages` | all | `pdf_index` range/list to price, e.g. `0-199` |
| `--out-tokens` | `750-1100` | Estimated output tokens per page |

---

## 10. Troubleshooting

| Symptom | Cause & fix |
|---------|-------------|
| `Unsupported parameter: 'max_tokens'` | Newer OpenAI models renamed it. Already handled — the code retries with `max_completion_tokens`. If you still see it, update the `openai` package. |
| `503 UNAVAILABLE ... high demand` (Gemini) | Transient overload. The code retries automatically. If it persists, try later or use another provider. |
| Tables come back as **empty dashes** | The model drew the frame but didn't read the cells. Raise `--dpi` to 200, or switch to a stronger model. This is the main quality risk — check for it in `compare.py`. |
| Missing API key error | Check `.env` is in the same folder as the scripts and the variable name matches exactly (e.g. `ANTHROPIC_API_KEY`). Run `python check_keys.py`. |
| `API key not valid` / `API_KEY_INVALID` | The provider rejected the key itself. Run `python check_keys.py --ping`: compare the last 4 characters with the key in the provider's console, and create a new key if it was deleted or restricted. (A stale system-environment key can no longer override `.env`.) |
| Validation error mentioning `thinking_level` or `service_tier` (Gemini) | Your `google-genai` is too old for these settings: `pip install -U google-genai`. |
| `--printed needs --page-offset` / `pages out of range` | Add `--page-offset` (see §4 Step 1), or check the page numbers — the error prints the valid `pdf_index` range. |
| Wrong printed page numbers | Your `--page-offset` is off, or the book restarts numbering mid-way. Re-derive it (§4.1); run sections separately if numbering resets. |
| Batch pages `expired` | The 24-hour window passed. Resubmit just those pages — see `failed_pages.json`. |
| A page's error says "truncated at max_tokens" | The model hit the output ceiling mid-page (rare — the ceiling is already generous at 16,384 tokens, since you only pay for what's generated). The page is treated as failed and retried on rerun (sync) or listed under `truncated` in `failed_pages.json` (batch). If it keeps happening on the same page, that page likely has an unusually dense table — try `compare.py` on it. |
| A page's error says "empty output" | The model returned nothing for that page. Treated as failed/retried the same way as a truncation. Usually transient; if it repeats, check the page image isn't corrupted or blank. |
| `batch_extractor.py` refuses to run, printing a parameter mismatch | The `batch.json` in that `--out` folder was created with different `--pages`/`--dpi`/`--model`, or a different PDF. Use a fresh `--out`, or delete `batch.json` if you deliberately want to change parameters (you lose the no-double-charge guarantee for anything already submitted). |

---

## 11. Cost strategy in one paragraph

Run `estimate_cost.py` to see the numbers, then `compare.py` to see the quality. The cheapest model that *actually transcribes your tables correctly* is the right answer — an empty table is worth nothing regardless of price. In practice, batch mode on a strong model for table-heavy sections plus a cheap model for plain prose usually beats picking one provider for everything.

**Prompt caching doesn't help here.** Caching discounts a *repeated* prompt prefix, but each request is one unique page image plus a ~290-token instruction — below every provider's minimum cacheable length. It would only start paying off if you grew the prompt with worked examples (e.g. a few thousand tokens of sample Thai tables); caching and batch discounts stack, so that would stay cheap.
