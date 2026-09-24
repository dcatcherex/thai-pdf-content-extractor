# Thai PDF Extractor

Turns Thai-heavy PDFs (tables + illustrations, corrupted text layer) into clean, AI-ready content: Markdown, RAG chunks, and structured JSON — with page-level provenance.

Works by **rendering each page to an image and reading it with a vision model**, which sidesteps the scrambled Thai text layer that defeats normal PDF text extraction.

## Documentation

| Doc | Read this if you... |
|-----|---------------------|
| **[STAFF_GUIDE_TH.md](STAFF_GUIDE_TH.md)** | are staff using the web app (Thai) |
| **[USER_MANUAL.md](USER_MANUAL.md)** | want to **run** the tool |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | want to **modify, extend, or port** it (human or AI developer) |

## Quick start

```bash
pip install -r requirements.txt

# copy .env.example to .env and fill in your keys, then:
python check_keys.py --ping                  # 0. are the keys OK?

python estimate_cost.py doc.pdf              # 1. what will this cost?
python compare.py doc.pdf --printed 96,172 --page-offset 9
                                             # 2. which provider reads my tables?
python batch_extractor.py doc.pdf --out ./out \
  --title "..." --page-offset 9              # 3. extract (50% off via batch)
```

## Web app for staff

```bash
streamlit run app.py          # or double-click start_app.bat on Windows
```

Staff open `http://<computer-name>:8501` in a browser. They can upload a PDF, get the page
offset detected automatically, see the cost in baht, run the job, review flagged pages side by side
with the page image, fix or re-read those pages, and download a zip. The Thai interface is described in
`STAFF_GUIDE_TH.md`, and setup is covered in USER_MANUAL.md §12.

## The scripts

| Script | Purpose |
|--------|---------|
| `estimate_cost.py` | Price a run before committing |
| `compare.py` | Compare provider quality side by side |
| `extractor.py` | Sync extraction; multi-provider; resumable |
| `batch_extractor.py` | Async batch extraction at **50% cost** (Anthropic, or Gemini with `--provider gemini`) |
| `common.py` | Shared prompt, assembly, metadata, `.env` |
| `providers.py` | Vision backends + pricing table |
| `check_keys.py` | Show / test which API key each provider uses |
| `app.py` | Web app (Streamlit, Thai UI) |
| `jobs.py` / `quality.py` / `offset_detect.py` | Web app back end: background jobs, automatic page checks, automatic page offset |

Pages can be chosen by position (`--pages 105`, 0-based) or by the number printed on the page
(`--printed 96 --page-offset 9`) in every script. See USER_MANUAL.md §4 for finding the offset.

Both extraction modes **resume after any crash or interruption** — just rerun the same command.

Tests (no API keys or network needed): `python -m unittest discover -s tests`. Changes are logged in `CHANGELOG.md`.
