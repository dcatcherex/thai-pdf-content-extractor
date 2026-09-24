# Thai PDF Extractor

Turns Thai-heavy PDFs (tables + illustrations, corrupted text layer) into clean, AI-ready content: Markdown, RAG chunks, and structured JSON — with page-level provenance.

Works by **rendering each page to an image and reading it with a vision model**, which sidesteps the scrambled Thai text layer that defeats normal PDF text extraction.

## Documentation

| Doc | Read this if you... |
|-----|---------------------|
| **[USER_MANUAL.md](USER_MANUAL.md)** | want to **run** the tool |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | want to **modify, extend, or port** it (human or AI developer) |

## Quick start

```bash
pip install pymupdf python-dotenv anthropic

# put your keys in .env  (ANTHROPIC_API_KEY=... / OPENAI_API_KEY=... / GEMINI_API_KEY=...)

python estimate_cost.py doc.pdf              # 1. what will this cost?
python compare.py doc.pdf --pages 2,182      # 2. which provider reads my tables?
python batch_extractor.py doc.pdf --out ./out \
  --title "..." --page-offset 9              # 3. extract (50% off via batch)
```

## The scripts

| Script | Purpose |
|--------|---------|
| `estimate_cost.py` | Price a run before committing |
| `compare.py` | Compare provider quality side by side |
| `extractor.py` | Sync extraction; multi-provider; resumable |
| `batch_extractor.py` | Async batch extraction at **50% cost** (Anthropic) |
| `common.py` | Shared prompt, assembly, metadata, `.env` |
| `providers.py` | Vision backends + pricing table |

Both extraction modes **resume after any crash or interruption** — just rerun the same command.
