# PDFKit - a self-hosted all-in-one PDF toolbox

34 PDF tools behind one web UI and one HTTP API. All code here is original; it re-implements
the *kind of functionality* online PDF suites offer, using open-source libraries.
No third-party site code, assets, branding or APIs are used.

## Run it

### Windows (easiest)
1. Install **Python 3.12** from python.org (tick *Add python.exe to PATH*).
2. Unzip, then **double-click `run.bat`**. It creates a virtualenv, installs the requirements and opens http://127.0.0.1:8000.

That alone enables the core tools (merge, split, compress, rotate, watermark, redact, protect, sign, PDF → JPG/Excel/Markdown/Word, edit, forms, compare, ...).
Tools that need an external program stay greyed-out in the UI ("needs X installed") until you install it - PDFKit finds the default install folders automatically:

| Tool | Install |
|---|---|
| Word/PowerPoint/Excel → PDF | LibreOffice |
| OCR | Tesseract (UB Mannheim installer; add extra language data such as `ben` during setup) |
| PDF/A | Ghostscript (64-bit) |
| HTML → PDF | WeasyPrint needs the GTK/Pango runtime, which is awkward on Windows - use Docker or WSL for this one tool |

Override locations with env vars `SOFFICE_PATH`, `TESSERACT_PATH`, `GS_PATH` if you installed elsewhere. `http://127.0.0.1:8000/api/health` shows which engines were found.

### Linux / macOS
```bash
sudo apt install libreoffice-writer libreoffice-calc libreoffice-impress \
     tesseract-ocr tesseract-ocr-eng tesseract-ocr-ben ghostscript fonts-dejavu-core fonts-noto-core \
     libpango-1.0-0 libpangoft2-1.0-0            # macOS: brew install libreoffice tesseract ghostscript pango
./run.sh                                          # open http://127.0.0.1:8000
```
### Docker (everything included)
`docker build -t pdfkit . && docker run -p 8000:8000 pdfkit`  (not built in the dev sandbox - see "Not verified").

Optional environment variables: `MAX_MB` (per-file limit, default 100), `MAX_FILES` (30),
`SESSION_TTL` (preview lifetime in s, 3600), `ANTHROPIC_API_KEY` + `LLM_MODEL` (AI tools, default `claude-haiku-5-5`).

## Tests
```bash
pip install -r requirements-dev.txt && python -m pytest -q          # 38 backend tests, real files, full HTTP path
cd tests/frontend && npm i && node smoke.mjs                         # jsdom UI test (server must be running on :8765, or set BASE)
```

## Features
| Group | Tools |
|---|---|
| Organize | Merge, Split (ranges / every N / extract), Extract pages, Remove pages, Organize (reorder, rotate, delete, add blank - thumbnails UI), Scan to PDF (phone camera) |
| Optimize | Compress (3 levels), Repair, OCR (Tesseract, any installed language) |
| To PDF | Images (JPG/PNG/WebP/TIFF/BMP/GIF), Word, PowerPoint, Excel (LibreOffice), HTML file or URL |
| From PDF | JPG/PNG (pages or embedded images), Word (pdf2docx), PowerPoint (image or editable-text mode), Excel (tables), PDF/A-2b, Markdown |
| Edit | Rotate (batch), Page numbers, Watermark (text/image, opacity, angle, tile, over/under), Crop (draw or margins), Edit (text, shapes, highlight, freehand), PDF forms (create / auto-detect `____` blanks / fill / flatten) |
| Security | Protect (AES-256 + permissions), Unlock, Sign (drawn/typed visual signature), Redact (area, words, regex; real removal), Compare (text + pixel diff report) |
| AI (needs key) | Summarizer, Translate (layout-preserving) |
| Extra | Workflows: chain tools server-side, save chains in the browser; batch processing (several files → ZIP) |

API: `POST /api/run/{tool}` (multipart `files`, optional `aux` images, JSON `options`), `GET /api/tools` (schema that drives the UI),
`POST /api/workflow`. Interactive docs at `/api/docs`.

## Architecture
```
web/index.html     vanilla-JS SPA; builds every tool form from GET /api/tools; SVG overlay editors (no CDN, no build step)
app/main.py        FastAPI: upload limits, per-request temp dir (always deleted), batch→zip, preview sessions, workflows
app/tools.py       one decorated function per tool: fn(files, opts, workdir) -> Path | [Path]
```
Add a tool = one `@tool(...)` function with its option schema; the API and the UI pick it up automatically.
Stack choice: Python because PyMuPDF/pikepdf/pdfplumber/pdf2docx give best-in-class PDF handling; LibreOffice and
Tesseract are the proven engines for Office conversion and OCR.

## Known limitations (honest list)
- **Sign** is a *visual* signature. It is not a cryptographic/certificate signature, and there is no "request signatures from others" flow (needs accounts + email).
- **PDF → Word/PowerPoint/Excel** quality depends on the PDF; scanned PDFs need OCR first (the tool tells you). Layout is good for simple documents, not pixel-exact.
- **Edit** adds new content; it does not re-flow or edit existing paragraphs. Complex scripts (e.g. Bengali conjuncts) are drawn without shaping, so some glyph clusters may look wrong; Latin/Cyrillic/Greek work.
- **Compress** recompresses images and cleans structure; text-only PDFs shrink little.
- **Crop** sets the page crop box (content stays in the file). **Unlock** works with the password, or on files with only owner restrictions.
- **AI tools** send document text to the Claude API; they are disabled without `ANTHROPIC_API_KEY`.
- **PDF/A** uses Ghostscript's PDF/A-2b conversion; run a validator (e.g. veraPDF) if you need certified compliance.
- Not included: user accounts, payments, desktop/mobile apps, cloud-drive import, e-sign requests.

## Security notes
- Uploads: extension allow-list per tool, size limits, sanitized unique names, never executed; temp dirs removed after each response.
- HTML-to-PDF URL mode blocks non-public addresses (incl. on redirects and sub-resources). It is best-effort (DNS rebinding is possible), so also run the container with an egress firewall if you expose it publicly.
- Redaction removes text and image pixels and scrubs metadata; verify important redactions by re-extracting text.
- No authentication or rate limiting: put it behind a reverse proxy (auth, TLS, rate limits) before exposing it to the internet.
- Only use Unlock/OCR/Redact on documents you own or are authorised to process.

## Not verified in the build environment
Browser behaviour (real pointer/touch drawing, layout on phones), the Docker image, **Windows itself** (engine auto-detection and file-handling fixes are unit-tested on Linux only), the AI tools against the live API
(tested with a mocked LLM), non-English OCR language packs.
