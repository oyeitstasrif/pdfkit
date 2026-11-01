"""PDF tool implementations.

Every tool is ``fn(files: list[Path], opts: dict, work: Path) -> Path | list[Path]``
registered with the ``@tool`` decorator. ``main.py`` handles uploads, batching and zipping.
"""
import base64, difflib, glob, html, io, ipaddress, json, os, re, shutil, socket, subprocess, urllib.parse, zipfile
from functools import lru_cache
from pathlib import Path

import httpx
import pikepdf
import pymupdf
from PIL import Image, ImageChops, ImageOps

HERE = Path(__file__).parent


class ToolError(Exception):
    """User-facing error (HTTP 400)."""


TOOLS: dict[str, dict] = {}


def O(name, label, type="text", default="", choices=None, help="", aux=False):
    return dict(name=name, label=label, type=type, default=default, choices=choices, help=help, aux=aux)


def tool(id, name, cat, desc, exts=(".pdf",), min=1, max=None, multi=False, options=(), visual=None, ai=False, needs=None):
    def deco(fn):
        TOOLS[id] = dict(id=id, name=name, cat=cat, desc=desc, exts=list(exts), min=min, max=max, multi=multi,
                         options=list(options), visual=visual, ai=ai, needs=needs, fn=fn)
        return fn
    return deco


# ----------------------------------------------------------------------------- helpers
PW = O("password", "Password (if the PDF is protected)", "password")


def flag(o, k, d=False):
    v = o.get(k, d)
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")


def num(o, k, d):
    try:
        return float(o.get(k, d))
    except (TypeError, ValueError):
        return float(d)


def jlist(o, k):
    v = o.get(k) or []
    if isinstance(v, str):
        try:
            v = json.loads(v or "[]")
        except ValueError:
            raise ToolError(f"Invalid JSON in '{k}'")
    if not isinstance(v, list):
        raise ToolError(f"'{k}' must be a list")
    return v


def rgb(s, d="#000000"):
    s = (s or d).lstrip("#")
    try:
        return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return rgb(d)


def data_url(s):
    try:
        return base64.b64decode(s.split(",", 1)[1])
    except Exception:
        raise ToolError("Invalid image data")


def parse_pages(spec, n):
    """'1-3,5,8-' -> zero-based page indexes. Empty spec = all pages."""
    if spec is None or not str(spec).strip():
        return list(range(n))
    out = []
    for part in re.split(r"[,\s]+", str(spec).strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)", part)
        if m:
            a, b = int(m.group(1) or 1), int(m.group(2) or n)
            rng = range(a, b + (1 if a <= b else -1), 1 if a <= b else -1)
        elif part.isdigit():
            rng = [int(part)]
        else:
            raise ToolError(f"Invalid page range: '{part}'")
        for p in rng:
            if not 1 <= p <= n:
                raise ToolError(f"Page {p} is out of range (document has {n} pages)")
            out.append(p - 1)
    return out


def open_pdf(path, o=None):
    try:
        doc = pymupdf.open(path)
    except Exception:
        raise ToolError(f"'{Path(path).name}' is not a readable PDF (try the Repair tool).")
    if not doc.is_pdf:
        raise ToolError(f"'{Path(path).name}' is not a PDF.")
    if doc.needs_pass and not doc.authenticate((o or {}).get("password", "") or ""):
        raise ToolError(f"'{Path(path).name}' is password protected - enter the password or use Unlock PDF.")
    return doc


def outp(w, name):
    d = Path(w) / "out"
    d.mkdir(exist_ok=True)
    return d / name


def save(doc, path, **kw):
    doc.save(str(path), garbage=3, deflate=True, encryption=pymupdf.PDF_ENCRYPT_NONE, **kw)
    return Path(path)


def stem(p):
    return Path(p).stem


def upright(page):
    """Normalise /Rotate so visual coordinates == page coordinates (appearance unchanged)."""
    if page.rotation:
        page.remove_rotation()


class Fnt:
    def __init__(self, page, text):
        f = pick_font(frozenset(text))
        if f:
            self.name = "F" + format(abs(hash(f)) % 10 ** 6, "06d")
            page.insert_font(fontname=self.name, fontfile=f)
            self._font = pymupdf.Font(fontfile=f)
        else:
            self.name, self._font = "helv", pymupdf.Font("helv")

    def length(self, text, size):
        return self._font.text_length(text, fontsize=size)


@lru_cache(maxsize=256)
def pick_font(chars):
    if all(ord(c) < 256 for c in chars):
        return None
    for f in font_files():
        try:
            font = pymupdf.Font(fontfile=f)
            if all(font.has_glyph(ord(c)) for c in chars if not c.isspace()):
                return f
        except Exception:
            continue
    return None


@lru_cache(maxsize=1)
def font_files():
    files = []
    for root in ("/usr/share/fonts", "/usr/local/share/fonts", str(HERE / "fonts"), "/Library/Fonts", "C:/Windows/Fonts"):
        if Path(root).exists():
            files += [str(p) for p in Path(root).rglob("*") if p.suffix.lower() in (".ttf", ".otf")]
    return sorted(files, key=lambda p: (0 if re.search(r"NotoSans-Regular|DejaVuSans\.ttf", p) else 1, p))


def render(page, dpi=60):
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def pil_to_bytes(im, fmt="PNG", **kw):
    b = io.BytesIO()
    im.save(b, fmt, **kw)
    return b.getvalue()


def flatten_alpha(im):
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


ENGINES = {  # key -> (label, executable names, extra glob patterns, env override)
    "soffice": ("LibreOffice", ["soffice", "libreoffice"],
                [r"C:\Program Files\LibreOffice*\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice*\program\soffice.exe",
                 "/Applications/LibreOffice.app/Contents/MacOS/soffice"], "SOFFICE_PATH"),
    "tesseract": ("Tesseract OCR", ["tesseract"],
                  [r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                   "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"], "TESSERACT_PATH"),
    "gs": ("Ghostscript", ["gs", "gswin64c", "gswin32c"],
           [r"C:\Program Files\gs\gs*\bin\gswin64c.exe", r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe",
            "/opt/homebrew/bin/gs", "/usr/local/bin/gs"], "GS_PATH"),
}


def find_exe(key):
    """Locate an external engine: env override -> PATH -> well-known install folders (Windows/macOS)."""
    label, names, patterns, env = ENGINES[key]
    if os.environ.get(env) and Path(os.environ[env]).exists():
        return os.environ[env]
    for n in names:
        if p := shutil.which(n):
            return p
    for pat in patterns:
        if hits := sorted(glob.glob(pat)):
            return hits[-1]
    return None


def has_weasyprint():
    try:
        import weasyprint  # noqa: F401  (needs the Pango/GTK runtime; missing on a plain Windows install)
        return True
    except Exception:
        return False


def engine_status():
    st = {k: bool(find_exe(k)) for k in ENGINES}
    st["weasyprint"] = has_weasyprint()
    return st


def run(cmd, timeout=180):
    """Run an external engine. cmd[0] is an ENGINES key (resolved to a real path) or a plain executable."""
    cmd = list(cmd)
    if cmd[0] in ENGINES:
        exe = find_exe(cmd[0])
        if not exe:
            raise ToolError(f"{ENGINES[cmd[0]][0]} is not installed on the server - see the README (or set {ENGINES[cmd[0]][3]}).")
        cmd[0] = exe
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise ToolError(f"'{cmd[0]}' is not installed on the server.")
    except subprocess.TimeoutExpired:
        raise ToolError("The conversion took too long and was cancelled.")
    return r


# ----------------------------------------------------------------------------- ORGANIZE
@tool("merge", "Merge PDF", "organize", "Combine PDFs in the order you want.", min=2, multi=True,
      options=[O("password", "Password for protected files", "password")])
def merge(files, o, w):
    out = pymupdf.open()
    for f in files:
        out.insert_pdf(open_pdf(f, o))
    return save(out, outp(w, "merged.pdf"))


def pick(doc, idx):
    out = pymupdf.open()
    for i in idx:
        out.insert_pdf(doc, from_page=i, to_page=i)
    return out


@tool("split", "Split PDF", "organize", "Split by ranges, every N pages, or extract selected pages.",
      options=[O("mode", "Mode", "select", "ranges", [["ranges", "Custom ranges"], ["every", "Every N pages"], ["extract", "Extract pages into one PDF"]]),
               O("ranges", "Ranges / pages", "text", "", help="Ranges: '1-3;4-6' = two files. Extract: '1,3,5-7'"),
               O("every", "N pages per file", "number", 1), PW])
def split(files, o, w):
    doc = open_pdf(files[0], o)
    n, base, mode = len(doc), stem(files[0]), o.get("mode", "ranges")
    if mode == "every":
        k = max(1, int(num(o, "every", 1)))
        groups = [list(range(i, min(i + k, n))) for i in range(0, n, k)]
    elif mode == "extract":
        groups = [parse_pages(o.get("ranges"), n)]
    else:
        spec = str(o.get("ranges", "")).strip()
        if not spec:
            raise ToolError("Enter at least one range, e.g. 1-3;4-6")
        groups = [parse_pages(g, n) for g in spec.split(";") if g.strip()]
    outs = []
    for i, g in enumerate(groups, 1):
        name = f"{base}_extracted.pdf" if mode == "extract" else f"{base}_part{i}.pdf"
        outs.append(save(pick(doc, g), outp(w, name)))
    return outs


@tool("extract_pages", "Extract pages", "organize", "Keep only the pages you choose.",
      options=[O("pages", "Pages to keep", "text", "1", help="e.g. 1,3,5-7"), PW])
def extract_pages(files, o, w):
    doc = open_pdf(files[0], o)
    return save(pick(doc, parse_pages(o.get("pages"), len(doc))), outp(w, f"{stem(files[0])}_extracted.pdf"))


@tool("remove_pages", "Remove pages", "organize", "Delete the pages you don't need.",
      options=[O("pages", "Pages to remove", "text", "", help="e.g. 2,4-6"), PW])
def remove_pages(files, o, w):
    doc = open_pdf(files[0], o)
    drop = set(parse_pages(o.get("pages"), len(doc))) if str(o.get("pages", "")).strip() else set()
    if not drop:
        raise ToolError("Enter the pages to remove.")
    keep = [i for i in range(len(doc)) if i not in drop]
    if not keep:
        raise ToolError("You can't remove every page.")
    return save(pick(doc, keep), outp(w, f"{stem(files[0])}_removed.pdf"))


@tool("organize", "Organize PDF", "organize", "Reorder, rotate, delete or add blank pages.", visual="organize",
      options=[O("order", "Page order (optional text form)", "text", "", help="e.g. 3,1,2 - pages not listed are dropped"),
               O("plan", "plan", "hidden", ""), PW])
def organize(files, o, w):
    doc = open_pdf(files[0], o)
    plan = jlist(o, "plan")
    if not plan:
        plan = [{"page": i + 1} for i in parse_pages(o.get("order"), len(doc))]
    if not plan:
        raise ToolError("Nothing to do.")
    out = pymupdf.open()
    for item in plan:
        if item.get("blank"):
            r = doc[0].rect
            out.new_page(width=r.width, height=r.height)
            continue
        p = int(item.get("page", 0)) - 1
        if not 0 <= p < len(doc):
            raise ToolError(f"Page {p + 1} is out of range.")
        out.insert_pdf(doc, from_page=p, to_page=p)
        if item.get("rotate"):
            pg = out[-1]
            pg.set_rotation((pg.rotation + int(item["rotate"])) % 360)
    return save(out, outp(w, f"{stem(files[0])}_organized.pdf"))


# ----------------------------------------------------------------------------- OPTIMIZE
@tool("compress", "Compress PDF", "optimize", "Shrink file size while keeping quality readable.",
      options=[O("level", "Compression", "select", "recommended", [["low", "Less compression, high quality"], ["recommended", "Recommended"], ["extreme", "Extreme compression"]]), PW])
def compress(files, o, w):
    q, scale = {"low": (80, 1.0), "recommended": (60, 0.75), "extreme": (35, 0.5)}.get(o.get("level", "recommended"), (60, .75))
    doc = open_pdf(files[0], o)
    seen = set()
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                pix = pymupdf.Pixmap(doc, xref)
                if pix.alpha or img[1]:  # skip images with transparency masks
                    continue
                if pix.colorspace is None or pix.n - pix.alpha >= 4:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                im = Image.frombytes("L" if pix.n == 1 else "RGB", (pix.width, pix.height), pix.samples)
                if scale < 1 and min(im.size) > 200:
                    im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
                data = pil_to_bytes(im, "JPEG", quality=q, optimize=True)
                if len(data) < len(doc.xref_stream_raw(xref)):
                    page.replace_image(xref, stream=data)
            except Exception:
                continue
    doc.subset_fonts()
    return save(doc, outp(w, f"{stem(files[0])}_compressed.pdf"), clean=True)


@tool("repair", "Repair PDF", "optimize", "Recover data from a damaged PDF.", options=[])
def repair(files, o, w):
    out = outp(w, f"{stem(files[0])}_repaired.pdf")
    try:
        with pikepdf.open(files[0], attempt_recovery=True) as p:
            p.save(out)
        return out
    except Exception:
        pass
    try:
        doc = pymupdf.open(files[0])
        if doc.page_count == 0:
            raise ValueError
        return save(doc, out, clean=True)
    except Exception:
        raise ToolError("This file is too damaged to repair.")


@tool("ocr", "OCR PDF", "optimize", "Turn scanned pages into searchable, selectable text.", needs="tesseract",
      options=[O("lang", "Language (Tesseract code)", "text", "eng", help="e.g. eng, ben, eng+ben (language pack must be installed)"),
               O("force", "Re-OCR pages that already contain text", "check", False), O("dpi", "Scan DPI", "number", 200), PW])
def ocr(files, o, w):
    lang = str(o.get("lang", "eng")).strip() or "eng"
    if not re.fullmatch(r"[A-Za-z0-9_+]+", lang):
        raise ToolError("Invalid language code.")
    dpi = int(min(max(num(o, "dpi", 200), 100), 400))
    doc = open_pdf(files[0], o)
    out = pymupdf.open()
    for i, page in enumerate(doc):
        if page.get_text().strip() and not flag(o, "force"):
            out.insert_pdf(doc, from_page=i, to_page=i)
            continue
        pix = page.get_pixmap(dpi=dpi)
        pix.set_dpi(dpi, dpi)
        png = Path(w) / f"ocr{i}.png"
        pix.save(str(png))
        r = run(["tesseract", str(png), str(png.with_suffix("")), "-l", lang, "pdf"], 300)
        res = png.with_suffix(".pdf")
        if r.returncode or not res.exists():
            msg = r.stderr.decode(errors="ignore")[:200]
            raise ToolError(f"OCR failed ({'language pack missing?' if 'load' in msg.lower() else msg})")
        out.insert_pdf(pymupdf.open(res))
    return save(out, outp(w, f"{stem(files[0])}_ocr.pdf"))


# ----------------------------------------------------------------------------- CONVERT TO PDF
IMG = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp")


def images_to_pdf(files, o, w):
    sizes = {"a4": (595, 842), "letter": (612, 792)}
    margin = {"none": 0, "small": 20, "big": 50}.get(o.get("margin", "none"), 0)
    psize, orient = o.get("pagesize", "fit"), o.get("orientation", "portrait")

    def build(imgs):
        doc = pymupdf.open()
        for f in imgs:
            try:
                im = flatten_alpha(ImageOps.exif_transpose(Image.open(f)))
            except Exception:
                raise ToolError(f"'{Path(f).name}' is not a valid image.")
            iw, ih = im.width * 0.75, im.height * 0.75
            if psize == "fit":
                pw, ph = iw + 2 * margin, ih + 2 * margin
            else:
                pw, ph = sizes.get(psize, sizes["a4"])
                land = (iw > ih) if orient == "auto" else orient == "landscape"
                if land:
                    pw, ph = ph, pw
            page = doc.new_page(width=pw, height=ph)
            box = pymupdf.Rect(margin, margin, pw - margin, ph - margin)
            k = min(box.width / iw, box.height / ih)
            r = pymupdf.Rect(box.x0 + (box.width - iw * k) / 2, box.y0 + (box.height - ih * k) / 2,
                             box.x0 + (box.width + iw * k) / 2, box.y0 + (box.height + ih * k) / 2)
            page.insert_image(r, stream=pil_to_bytes(im, "JPEG", quality=92))
        return doc

    if flag(o, "merge", True) or len(files) == 1:
        return save(build(files), outp(w, "images.pdf"))
    return [save(build([f]), outp(w, f"{stem(f)}.pdf")) for f in files]


_IMG_OPTS = [O("pagesize", "Page size", "select", "fit", [["fit", "Same as image"], ["a4", "A4"], ["letter", "US Letter"]]),
             O("orientation", "Orientation", "select", "portrait", [["portrait", "Portrait"], ["landscape", "Landscape"], ["auto", "Auto"]]),
             O("margin", "Margin", "select", "none", [["none", "None"], ["small", "Small"], ["big", "Big"]]),
             O("merge", "Merge all images into one PDF", "check", True)]
tool("jpg_to_pdf", "JPG to PDF", "to_pdf", "Convert images (JPG, PNG, WebP, TIFF...) to PDF.", exts=IMG, multi=True, options=_IMG_OPTS)(images_to_pdf)
tool("scan_to_pdf", "Scan to PDF", "organize", "Take photos with your phone camera and turn them into a PDF.", exts=IMG, multi=True,
     options=_IMG_OPTS, visual="camera")(images_to_pdf)


def office_to_pdf(exts):
    def fn(files, o, w):
        outs = []
        for f in files:
            prof = Path(w) / "lo_profile"
            r = run(["soffice", f"-env:UserInstallation={prof.as_uri()}", "--headless", "--convert-to", "pdf",
                     "--outdir", str(Path(w) / "out"), str(f)], 180)
            pdf = Path(w) / "out" / (Path(f).stem + ".pdf")
            if r.returncode or not pdf.exists():
                raise ToolError(f"Could not convert '{Path(f).name}'.")
            outs.append(pdf)
        return outs
    return fn


tool("word_to_pdf", "Word to PDF", "to_pdf", "Convert DOC/DOCX/ODT/RTF to PDF.", exts=(".doc", ".docx", ".odt", ".rtf", ".txt"), needs="soffice")(office_to_pdf(0))
tool("powerpoint_to_pdf", "PowerPoint to PDF", "to_pdf", "Convert PPT/PPTX/ODP slideshows to PDF.", exts=(".ppt", ".pptx", ".odp"), needs="soffice")(office_to_pdf(0))
tool("excel_to_pdf", "Excel to PDF", "to_pdf", "Convert XLS/XLSX/ODS/CSV spreadsheets to PDF.", exts=(".xls", ".xlsx", ".ods", ".csv"), needs="soffice")(office_to_pdf(0))


def check_url(u):
    """SSRF guard: only public http(s) hosts."""
    p = urllib.parse.urlparse(u)
    if p.scheme == "data":
        return
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ToolError("Only http(s) URLs are allowed.")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or 80)
    except OSError:
        raise ToolError(f"Cannot resolve {p.hostname}")
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if not ip.is_global:
            raise ToolError("Private or local addresses are not allowed.")


def fetch_page(url, hops=5):
    """Fetch a page, validating every redirect hop against the SSRF guard."""
    for _ in range(hops):
        check_url(url)
        try:
            r = httpx.get(url, follow_redirects=False, timeout=15, headers={"User-Agent": "PDFKit/1.0"})
        except httpx.HTTPError as e:
            raise ToolError(f"Could not fetch the page: {str(e)[:100]}")
        if r.is_redirect:
            url = urllib.parse.urljoin(url, r.headers.get("location", ""))
            continue
        if len(r.content) > 10 << 20:
            raise ToolError("The page is too large.")
        return str(r.url), r.text
    raise ToolError("Too many redirects.")


def safe_fetcher():
    """URL fetcher for sub-resources: public http(s)/data only, no redirects."""
    try:
        from weasyprint import URLFetcher

        class Safe(URLFetcher):
            def __init__(self):
                super().__init__(allowed_protocols={"http", "https", "data"}, allow_redirects=False)

            def fetch(self, url, headers=None):
                check_url(url)
                return super().fetch(url, headers)
        return Safe()
    except ImportError:  # WeasyPrint < 66: function based API
        from weasyprint import default_url_fetcher

        def fetcher(url, *a, **k):
            check_url(url)
            return default_url_fetcher(url, *a, **k)
        return fetcher


@tool("html_to_pdf", "HTML to PDF", "to_pdf", "Convert a web page URL or an HTML file to PDF.", exts=(".html", ".htm"), min=0, max=1, multi=True, needs="weasyprint",
      options=[O("url", "Page URL (or upload an HTML file)", "text", "https://"),
               O("pagesize", "Page size", "select", "A4", [["A4", "A4"], ["Letter", "US Letter"]])])
def html_to_pdf(files, o, w):
    try:
        from weasyprint import CSS, HTML
    except Exception:
        raise ToolError("HTML to PDF needs WeasyPrint's Pango/GTK libraries (easiest via Docker or WSL; see README). All other tools work without it.")
    size = "Letter" if o.get("pagesize") == "Letter" else "A4"
    css = [CSS(string=f"@page {{ size: {size}; margin: 15mm }}")]
    if files:
        base, text = None, Path(files[0]).read_text(errors="ignore")
    else:
        url = str(o.get("url", "")).strip()
        if not url or url == "https://":
            raise ToolError("Enter a URL or upload an HTML file.")
        base, text = fetch_page(url)
    out = outp(w, "page.pdf")
    try:
        HTML(string=text, base_url=base, url_fetcher=safe_fetcher()).write_pdf(str(out), stylesheets=css)
    except Exception as e:
        raise ToolError(f"Could not render the page: {str(e)[:150]}")
    return out


# ----------------------------------------------------------------------------- CONVERT FROM PDF
@tool("pdf_to_jpg", "PDF to JPG", "from_pdf", "Render each page as an image, or extract embedded images.",
      options=[O("mode", "Mode", "select", "pages", [["pages", "Convert pages"], ["extract", "Extract embedded images"]]),
               O("format", "Format", "select", "jpg", [["jpg", "JPG"], ["png", "PNG"]]), O("dpi", "Quality (DPI)", "number", 150),
               O("pages", "Pages (blank = all)", "text", ""), PW])
def pdf_to_jpg(files, o, w):
    doc, base = open_pdf(files[0], o), stem(files[0])
    idx, outs = parse_pages(o.get("pages"), len(doc)), []
    if o.get("mode") == "extract":
        seen = set()
        for i in idx:
            for img in doc[i].get_images(full=True):
                if img[0] in seen:
                    continue
                seen.add(img[0])
                d = doc.extract_image(img[0])
                p = outp(w, f"{base}_p{i + 1}_img{img[0]}.{d['ext']}")
                p.write_bytes(d["image"])
                outs.append(p)
        if not outs:
            raise ToolError("No embedded images found.")
        return outs
    png = o.get("format") == "png"
    dpi = int(min(max(num(o, "dpi", 150), 36), 400))
    for i in idx:
        pix = doc[i].get_pixmap(dpi=dpi, alpha=False)
        p = outp(w, f"{base}_page{i + 1}.{'png' if png else 'jpg'}")
        pix.save(str(p))
        outs.append(p)
    return outs


@tool("pdf_to_word", "PDF to Word", "from_pdf", "Convert a PDF into an editable DOCX (best for text-based PDFs).", options=[PW])
def pdf_to_word(files, o, w):
    from pdf2docx import Converter
    doc = open_pdf(files[0], o)
    if not any(p.get_text().strip() for p in doc):
        raise ToolError("This PDF looks scanned (no text). Run OCR PDF first, then convert.")
    src = save(doc, Path(w) / "plain.pdf")          # always a decrypted copy (authenticate() hides the encryption flag)
    out = outp(w, f"{stem(files[0])}.docx")
    cv = Converter(str(src))
    try:
        cv.convert(str(out))
    except Exception as e:
        raise ToolError(f"Conversion failed: {str(e)[:120]}")
    finally:
        cv.close()
    return out


@tool("pdf_to_powerpoint", "PDF to PowerPoint", "from_pdf", "One slide per page, as images or editable text boxes.",
      options=[O("mode", "Mode", "select", "image", [["image", "Pages as images (exact look)"], ["text", "Editable text boxes"]]), PW])
def pdf_to_powerpoint(files, o, w):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Pt
    doc = open_pdf(files[0], o)
    prs = Presentation()
    r0 = doc[0].rect
    prs.slide_width, prs.slide_height = Emu(int(r0.width * 12700)), Emu(int(r0.height * 12700))
    for page in doc:
        s = prs.slides.add_slide(prs.slide_layouts[6])
        if o.get("mode") == "text":
            for b in page.get_text("dict")["blocks"]:
                if b["type"] != 0:
                    continue
                tb = s.shapes.add_textbox(*(Emu(int(v * 12700)) for v in (b["bbox"][0], b["bbox"][1], b["bbox"][2] - b["bbox"][0] + 6, b["bbox"][3] - b["bbox"][1] + 4)))
                tf = tb.text_frame
                tf.word_wrap = True
                for li, line in enumerate(b["lines"]):
                    para = tf.paragraphs[0] if li == 0 else tf.add_paragraph()
                    for sp in line["spans"]:
                        run_ = para.add_run()
                        run_.text = sp["text"]
                        run_.font.size = Pt(max(4, sp["size"]))
                        run_.font.color.rgb = RGBColor((sp["color"] >> 16) & 255, (sp["color"] >> 8) & 255, sp["color"] & 255)
        else:
            png = Path(w) / f"s{page.number}.png"
            page.get_pixmap(dpi=150).save(str(png))
            s.shapes.add_picture(str(png), 0, 0, prs.slide_width, prs.slide_height)
    out = outp(w, f"{stem(files[0])}.pptx")
    prs.save(out)
    return out


@tool("pdf_to_excel", "PDF to Excel", "from_pdf", "Pull tables from a PDF into an XLSX workbook.",
      options=[O("single", "Put all tables on one sheet", "check", False), PW])
def pdf_to_excel(files, o, w):
    import pdfplumber
    from openpyxl import Workbook
    doc = open_pdf(files[0], o)
    src = save(doc, Path(w) / "plain.pdf")
    wb = Workbook()
    wb.remove(wb.active)
    found = False
    with pdfplumber.open(str(src)) as pdf:
        one = wb.create_sheet("Tables") if flag(o, "single") else None
        for pi, page in enumerate(pdf.pages, 1):
            for ti, tbl in enumerate(page.extract_tables(), 1):
                found = True
                ws = one or wb.create_sheet(f"p{pi}_t{ti}")
                if one and ws.max_row > 1:
                    ws.append([])
                for row in tbl:
                    ws.append([("" if c is None else c.replace("\n", " ")) for c in row])
        if not found:
            ws = wb.create_sheet("Text")
            for page in pdf.pages:
                for line in (page.extract_text() or "").splitlines():
                    ws.append(re.split(r"\s{2,}", line.strip()))
    out = outp(w, f"{stem(files[0])}.xlsx")
    wb.save(out)
    return out


@tool("pdf_to_pdfa", "PDF to PDF/A", "from_pdf", "Convert to PDF/A-2b for long-term archiving.", needs="gs", options=[PW])
def pdf_to_pdfa(files, o, w):
    doc = open_pdf(files[0], o)
    src = Path(w) / "src.pdf"
    save(doc, src)
    gs = find_exe("gs")
    cands = ([Path(gs).resolve().parent.parent / "iccprofiles" / "srgb.icc", Path(gs).resolve().parent.parent / "lib" / "srgb.icc"] if gs else [])
    cands += [Path(x) for pat in ("/usr/share/color/icc/**/srgb.icc", "/usr/share/ghostscript/*/iccprofiles/srgb.icc",
                                  "/opt/homebrew/share/ghostscript/*/iccprofiles/srgb.icc", "/usr/local/share/ghostscript/*/iccprofiles/srgb.icc",
                                  r"C:\Program Files\gs\gs*\iccprofiles\srgb.icc") for x in glob.glob(pat, recursive=True)]
    icc = next((c for c in cands if c.exists()), None)
    if not icc:
        raise ToolError("sRGB ICC profile (ghostscript) not found on the server.")
    ps = Path(w) / "pdfa_def.ps"
    ps.write_text(f"""%!
[/_objdef {{icc_PDFA}} /type /stream /OBJ pdfmark
[{{icc_PDFA}} <</N 3>> /PUT pdfmark
[{{icc_PDFA}} ({icc.as_posix()}) (r) file /PUT pdfmark
[/_objdef {{OutputIntent_PDFA}} /type /dict /OBJ pdfmark
[{{OutputIntent_PDFA}} <</Type /OutputIntent /S /GTS_PDFA1 /DestOutputProfile {{icc_PDFA}} /OutputConditionIdentifier (sRGB) /Info (sRGB)>> /PUT pdfmark
[{{Catalog}} <</OutputIntents [ {{OutputIntent_PDFA}} ]>> /PUT pdfmark
""")
    out = outp(w, f"{stem(files[0])}_pdfa.pdf")
    r = run(["gs", "-dPDFA=2", "-dBATCH", "-dNOPAUSE", "-dQUIET", f"--permit-file-read={icc.as_posix()}", f"--permit-file-read={ps.as_posix()}",
             "-sColorConversionStrategy=RGB", "-sProcessColorModel=DeviceRGB", "-dPDFACompatibilityPolicy=1",
             "-sDEVICE=pdfwrite", f"-sOutputFile={out}", ps.as_posix(), src.as_posix()], 300)
    if r.returncode or not out.exists():
        raise ToolError("PDF/A conversion failed.")
    return out


@tool("pdf_to_markdown", "PDF to Markdown", "from_pdf", "Headings, lists, tables and links as Markdown.", options=[PW])
def pdf_to_markdown(files, o, w):
    import pymupdf4llm
    doc = open_pdf(files[0], o)
    out = outp(w, f"{stem(files[0])}.md")
    out.write_text(pymupdf4llm.to_markdown(doc), encoding="utf-8")
    return out


# ----------------------------------------------------------------------------- EDIT
@tool("rotate", "Rotate PDF", "edit", "Rotate all or selected pages (works on many files at once).",
      options=[O("angle", "Rotation", "select", "90", [["90", "90° clockwise"], ["180", "180°"], ["270", "90° counter-clockwise"]]),
               O("pages", "Pages (blank = all)", "text", ""), PW])
def rotate(files, o, w):
    doc = open_pdf(files[0], o)
    ang = int(num(o, "angle", 90))
    if ang % 90:
        raise ToolError("Angle must be a multiple of 90.")
    for i in parse_pages(o.get("pages"), len(doc)):
        doc[i].set_rotation((doc[i].rotation + ang) % 360)
    return save(doc, outp(w, f"{stem(files[0])}_rotated.pdf"))


POS9 = [[p, p.replace("-", " ").title()] for p in ("top-left", "top-center", "top-right", "middle-left", "center", "middle-right", "bottom-left", "bottom-center", "bottom-right")]


@tool("page_numbers", "Add page numbers", "edit", "Number pages with your own position, format and style.",
      options=[O("position", "Position", "select", "bottom-center", [p for p in POS9 if "middle" not in p[0] and p[0] != "center"]),
               O("format", "Format", "select", "{n}", [["{n}", "1"], ["Page {n}", "Page 1"], ["{n} / {total}", "1 / 10"], ["Page {n} of {total}", "Page 1 of 10"]]),
               O("start", "First number", "number", 1), O("pages", "Pages to number (blank = all)", "text", ""),
               O("size", "Font size", "number", 11), O("color", "Colour", "color", "#000000"), O("margin", "Margin (pt)", "number", 24), PW])
def page_numbers(files, o, w):
    doc = open_pdf(files[0], o)
    idx = parse_pages(o.get("pages"), len(doc))
    start, size, m = int(num(o, "start", 1)), num(o, "size", 11), num(o, "margin", 24)
    total = start + len(idx) - 1
    pos = o.get("position", "bottom-center")
    for k, i in enumerate(idx):
        page = doc[i]
        upright(page)
        text = str(o.get("format", "{n}")).replace("{n}", str(start + k)).replace("{total}", str(total))
        f = Fnt(page, text)
        tw = f.length(text, size)
        v, h = pos.split("-")
        r = page.rect
        x = {"left": r.x0 + m, "center": (r.x0 + r.x1 - tw) / 2, "right": r.x1 - m - tw}[h]
        y = r.y0 + m + size if v == "top" else r.y1 - m
        page.insert_text((x, y), text, fontsize=size, fontname=f.name, color=rgb(o.get("color")))
    return save(doc, outp(w, f"{stem(files[0])}_numbered.pdf"))


@tool("watermark", "Add watermark", "edit", "Stamp text or an image over (or under) your pages.",
      options=[O("type", "Watermark type", "select", "text", [["text", "Text"], ["image", "Image"]]),
               O("text", "Text", "text", "CONFIDENTIAL"), O("image", "Image (PNG/JPG)", "file", aux=True),
               O("size", "Font size", "number", 48), O("scale", "Image width (% of page)", "number", 40),
               O("color", "Colour", "color", "#ff0000"), O("opacity", "Opacity (0-1)", "number", 0.3),
               O("angle", "Rotation (° counter-clockwise)", "number", 45),
               O("position", "Position", "select", "center", POS9 + [["tile", "Tile"]]),
               O("layer", "Layer", "select", "over", [["over", "Over content"], ["under", "Under content"]]),
               O("pages", "Pages (blank = all)", "text", ""), PW])
def watermark(files, o, w):
    doc = open_pdf(files[0], o)
    alpha, ang, over = min(max(num(o, "opacity", .3), 0), 1), num(o, "angle", 45), o.get("layer", "over") != "under"
    is_img = o.get("type") == "image"
    text = str(o.get("text", "")).strip()
    img = None
    if is_img:
        aux = o.get("_aux", [])
        if not aux:
            raise ToolError("Upload a watermark image.")
        try:
            img = Image.open(aux[0]).convert("RGBA")
        except Exception:
            raise ToolError("Invalid watermark image.")
        img.putalpha(img.getchannel("A").point(lambda v: int(v * alpha)))
    elif not text:
        raise ToolError("Enter the watermark text.")
    grid = {"left": .2, "center": .5, "right": .8, "top": .2, "middle": .5, "bottom": .8}
    for i in parse_pages(o.get("pages"), len(doc)):
        page = doc[i]
        upright(page)
        r = page.rect
        pos = o.get("position", "center")
        if pos == "tile":
            centers = [pymupdf.Point(r.x0 + r.width * a, r.y0 + r.height * b) for a in (.2, .5, .8) for b in (.15, .38, .62, .85)]
        elif pos == "center":
            centers = [r.tl + (r.br - r.tl) * .5]
        else:
            v, h = pos.split("-")
            centers = [pymupdf.Point(r.x0 + r.width * grid[h], r.y0 + r.height * grid[v])]
        if is_img:
            wpt = r.width * num(o, "scale", 40) / 100
            rot = img.rotate(ang, expand=True, resample=Image.BICUBIC)
            k = wpt / img.width
            data = pil_to_bytes(rot)
            for c in centers:
                box = pymupdf.Rect(c.x - rot.width * k / 2, c.y - rot.height * k / 2, c.x + rot.width * k / 2, c.y + rot.height * k / 2)
                page.insert_image(box, stream=data, overlay=over)
        else:
            size = num(o, "size", 48)
            f = Fnt(page, text)
            tw = f.length(text, size)
            for c in centers:
                page.insert_text((c.x - tw / 2, c.y + size * .3), text, fontsize=size, fontname=f.name, color=rgb(o.get("color"), "#ff0000"),
                                 fill_opacity=alpha, morph=(c, pymupdf.Matrix(ang)), overlay=over)
    return save(doc, outp(w, f"{stem(files[0])}_watermarked.pdf"))


@tool("crop", "Crop PDF", "edit", "Crop by drawing a box on the page or by margins; apply to selected pages.", visual="crop",
      options=[O("top", "Top margin (pt)", "number", 0), O("right", "Right margin (pt)", "number", 0),
               O("bottom", "Bottom margin (pt)", "number", 0), O("left", "Left margin (pt)", "number", 0),
               O("pages", "Pages (blank = all)", "text", ""), O("ops", "ops", "hidden", ""), PW])
def crop(files, o, w):
    doc = open_pdf(files[0], o)
    boxes = [x for x in jlist(o, "ops") if x.get("type") == "crop"]
    for i in parse_pages(o.get("pages"), len(doc)):
        page = doc[i]
        upright(page)
        r = page.rect
        if boxes:
            b = boxes[0]
            new = pymupdf.Rect(b["x0"], b["y0"], b["x1"], b["y1"]) & r
        else:
            new = pymupdf.Rect(r.x0 + num(o, "left", 0), r.y0 + num(o, "top", 0), r.x1 - num(o, "right", 0), r.y1 - num(o, "bottom", 0))
        if new.is_empty or new.width < 10 or new.height < 10:
            raise ToolError("The crop area is empty or outside the page.")
        page.set_cropbox(new)
    return save(doc, outp(w, f"{stem(files[0])}_cropped.pdf"))


@tool("edit", "Edit PDF", "edit", "Add text, shapes, highlights and freehand drawings.", visual="edit",
      options=[O("ops", "ops", "hidden", ""), PW])
def edit(files, o, w):
    doc = open_pdf(files[0], o)
    ops = jlist(o, "ops")
    if not ops:
        raise ToolError("Add at least one element to the page first.")
    for op in ops:
        pi = int(op.get("page", 1)) - 1
        if not 0 <= pi < len(doc):
            continue
        page = doc[pi]
        upright(page)
        t, col, wd = op.get("type"), rgb(op.get("color"), "#000000"), num(op, "width", 2)
        if t in ("rect", "ellipse", "highlight", "line"):
            a, b = pymupdf.Point(op["x0"], op["y0"]), pymupdf.Point(op["x1"], op["y1"])
            r = pymupdf.Rect(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))
        if t == "text":
            size, text = num(op, "size", 14), str(op.get("text", ""))
            f = Fnt(page, text)
            for li, line in enumerate(text.split("\n")):
                page.insert_text((op["x0"], op["y0"] + size + li * size * 1.2), line, fontsize=size, fontname=f.name, color=col)
        elif t == "rect":
            page.draw_rect(r, color=col, fill=rgb(op["fill"]) if op.get("fill") else None, width=wd)
        elif t == "ellipse":
            page.draw_oval(r, color=col, fill=rgb(op["fill"]) if op.get("fill") else None, width=wd)
        elif t == "line":
            page.draw_line(a, b, color=col, width=wd)
        elif t == "highlight":
            page.draw_rect(r, color=None, fill=rgb(op.get("color"), "#ffeb3b"), fill_opacity=.35)
        elif t == "ink":
            pts = [pymupdf.Point(x, y) for x, y in op.get("points", [])]
            if len(pts) > 1:
                page.draw_polyline(pts, color=col, width=wd, closePath=False)
        elif t == "image":
            page.insert_image(pymupdf.Rect(op["x0"], op["y0"], op["x1"], op["y1"]), stream=data_url(op["data"]))
    return save(doc, outp(w, f"{stem(files[0])}_edited.pdf"))


@tool("forms_fill", "Fill PDF form", "edit", "Fill existing form fields (and optionally flatten).", visual="forms_fill",
      options=[O("values", "values", "hidden", ""), O("flatten", "Flatten (make fields non-editable)", "check", False), PW])
def forms_fill(files, o, w):
    doc = open_pdf(files[0], o)
    vals = o.get("values") or {}
    if isinstance(vals, str):
        vals = json.loads(vals or "{}")
    n = 0
    for page in doc:
        for wd in page.widgets():
            if wd.field_name in vals:
                v = vals[wd.field_name]
                if wd.field_type == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
                    wd.field_value = wd.on_state() if v in (True, "true", "on", "yes", "1", 1) else "Off"
                else:
                    wd.field_value = str(v)
                wd.update()
                n += 1
    if not n:
        raise ToolError("No matching form fields were found to fill.")
    if flag(o, "flatten"):
        doc.bake(annots=True, widgets=True)
    return save(doc, outp(w, f"{stem(files[0])}_filled.pdf"))


@tool("forms_create", "Create PDF form", "edit", "Add fillable fields - draw them or auto-detect '_____' blanks.", visual="forms_create",
      options=[O("auto", "Auto-detect blanks (____) as text fields", "check", False), O("ops", "ops", "hidden", ""), PW])
def forms_create(files, o, w):
    doc = open_pdf(files[0], o)
    ops = [x for x in jlist(o, "ops") if x.get("type") in ("text", "checkbox")]
    if flag(o, "auto"):
        for pi, page in enumerate(doc):
            for k, wd in enumerate(x for x in page.get_text("words") if re.fullmatch(r"_{3,}", x[4])):
                ops.append(dict(type="text", page=pi + 1, x0=wd[0], y0=wd[1] - 2, x1=wd[2], y1=wd[3] + 2, name=f"auto_p{pi + 1}_{k + 1}"))
    if not ops:
        raise ToolError("Draw at least one field or enable auto-detect.")
    for k, op in enumerate(ops, 1):
        page = doc[int(op.get("page", 1)) - 1]
        upright(page)
        wd = pymupdf.Widget()
        wd.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX if op["type"] == "checkbox" else pymupdf.PDF_WIDGET_TYPE_TEXT
        wd.field_name = re.sub(r"[^\w .-]", "_", str(op.get("name") or f"field_{k}"))
        wd.rect = pymupdf.Rect(op["x0"], op["y0"], op["x1"], op["y1"])
        wd.border_color, wd.fill_color = (0, 0, 0), (.93, .96, 1)
        if wd.field_type == pymupdf.PDF_WIDGET_TYPE_TEXT:
            wd.text_fontsize = 0
        page.add_widget(wd)
    return save(doc, outp(w, f"{stem(files[0])}_form.pdf"))


# ----------------------------------------------------------------------------- SECURITY
@tool("protect", "Protect PDF", "security", "Encrypt with a password (AES-256) and restrict permissions.",
      options=[O("user_password", "Password to open", "password"), O("owner_password", "Owner password (optional)", "password"),
               O("allow_print", "Allow printing", "check", True), O("allow_copy", "Allow copying text", "check", True),
               O("allow_modify", "Allow editing", "check", False), PW])
def protect(files, o, w):
    up = str(o.get("user_password", ""))
    if not up:
        raise ToolError("Enter a password.")
    doc = open_pdf(files[0], o)
    tmp = save(doc, Path(w) / "plain.pdf")
    perm = pikepdf.Permissions(print_lowres=flag(o, "allow_print", True), print_highres=flag(o, "allow_print", True),
                               extract=flag(o, "allow_copy", True), modify_annotation=flag(o, "allow_modify"),
                               modify_assembly=flag(o, "allow_modify"), modify_form=flag(o, "allow_modify"),
                               modify_other=flag(o, "allow_modify"), accessibility=True)
    out = outp(w, f"{stem(files[0])}_protected.pdf")
    with pikepdf.open(tmp) as p:
        p.save(out, encryption=pikepdf.Encryption(user=up, owner=str(o.get("owner_password") or up), R=6, allow=perm))
    return out


@tool("unlock", "Unlock PDF", "security", "Remove the password/restrictions from a PDF you own or may modify.",
      options=[O("password", "Current password (if needed)", "password")])
def unlock(files, o, w):
    out = outp(w, f"{stem(files[0])}_unlocked.pdf")
    try:
        with pikepdf.open(files[0], password=str(o.get("password", "") or "")) as p:
            p.save(out, encryption=False)
    except pikepdf.PasswordError:
        raise ToolError("Wrong password - it is required to open this file.")
    except Exception:
        raise ToolError("Could not read this PDF.")
    return out


@tool("sign", "Sign PDF", "security", "Place a drawn/typed signature (visual signature, not a certificate signature).", visual="sign",
      options=[O("name", "Typed signature (if not drawing)", "text", ""), O("signature", "signature", "hidden", ""),
               O("date", "Add today's date under the signature", "check", False), O("ops", "ops", "hidden", ""), PW])
def sign(files, o, w):
    import datetime
    doc = open_pdf(files[0], o)
    sig = str(o.get("signature") or "")
    name = str(o.get("name") or "").strip()
    if not sig and not name:
        raise ToolError("Draw or type a signature first.")
    boxes = [x for x in jlist(o, "ops") if x.get("type") == "sign"]
    for b in boxes or [None]:
        pi = int(b["page"]) - 1 if b else len(doc) - 1
        page = doc[pi]
        upright(page)
        r = page.rect
        box = pymupdf.Rect(b["x0"], b["y0"], b["x1"], b["y1"]) if b else pymupdf.Rect(r.x1 - 200, r.y1 - 90, r.x1 - 40, r.y1 - 40)
        if sig:
            page.insert_image(box, stream=data_url(sig), keep_proportion=True)
        else:
            f = Fnt(page, name)
            size = box.height * .7
            while f.length(name, size) > box.width and size > 6:
                size -= 1
            page.insert_text((box.x0, box.y1 - box.height * .25), name, fontsize=size, fontname=f.name, color=(0, 0, .5))
        if flag(o, "date"):
            page.insert_text((box.x0, box.y1 + 11), datetime.date.today().isoformat(), fontsize=9, color=(.3, .3, .3))
    return save(doc, outp(w, f"{stem(files[0])}_signed.pdf"))


@tool("redact", "Redact PDF", "security", "Permanently remove text and graphics (drawn areas, words or regex).", visual="redact",
      options=[O("terms", "Words/phrases to redact (one per line)", "textarea", ""),
               O("regex", "Regex patterns (one per line)", "textarea", "", help=r"e.g. \b[\w.]+@[\w.]+\.\w+\b for e-mails"),
               O("scrub", "Also remove metadata & hidden data", "check", True), O("ops", "ops", "hidden", ""), PW])
def redact(files, o, w):
    doc = open_pdf(files[0], o)
    terms = [t.strip() for t in str(o.get("terms", "")).splitlines() if t.strip()]
    pats = []
    for p in str(o.get("regex", "")).splitlines():
        if p.strip():
            try:
                pats.append(re.compile(p.strip()))
            except re.error:
                raise ToolError(f"Invalid regex: {p.strip()}")
    boxes = [x for x in jlist(o, "ops") if x.get("type") == "redact"]
    if not (terms or pats or boxes):
        raise ToolError("Draw areas or enter words/patterns to redact.")
    count = 0
    for pi, page in enumerate(doc):
        upright(page)
        hits = []
        for t in terms:
            hits += page.search_for(t)
        if pats:
            text = page.get_text()
            for p in pats:
                for m in p.finditer(text):
                    hits += page.search_for(m.group(0))
        hits += [pymupdf.Rect(b["x0"], b["y0"], b["x1"], b["y1"]) for b in boxes if int(b["page"]) - 1 == pi]
        for r in hits:
            page.add_redact_annot(r, fill=(0, 0, 0))
        count += len(hits)
        if hits:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
    if not count:
        raise ToolError("Nothing matched - no redactions were applied.")
    if flag(o, "scrub", True):
        doc.set_metadata({})
        doc.scrub(remove_links=False, reset_fields=False)
    return save(doc, outp(w, f"{stem(files[0])}_redacted.pdf"))


@tool("compare", "Compare PDF", "security", "Side-by-side text and visual differences between two PDFs.", min=2, max=2, multi=True,
      options=[O("password", "Password (if both are protected with the same one)", "password")])
def compare(files, o, w):
    a, b = open_pdf(files[0], o), open_pdf(files[1], o)
    rows, sections = [], []
    uri = lambda im: "data:image/png;base64," + base64.b64encode(pil_to_bytes(im)).decode()
    for i in range(max(len(a), len(b))):
        ta = a[i].get_text().splitlines() if i < len(a) else []
        tb = b[i].get_text().splitlines() if i < len(b) else []
        diff = [l for l in difflib.unified_diff(ta, tb, lineterm="", n=0) if not l.startswith(("---", "+++", "@@"))]
        vis = 0.0
        imgs = ""
        if i < len(a) and i < len(b):
            ia, ib = render(a[i]), render(b[i])
            ib = ib.resize(ia.size)
            d = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > 40 else 0)
            vis = d.histogram()[255] / (d.width * d.height) * 100
            if vis > 0.05:
                ov = ia.copy()
                ov.paste(Image.new("RGB", ov.size, (255, 0, 0)), mask=d)
                imgs = f"<div class=im><figure><img src='{uri(ia)}'><figcaption>A</figcaption></figure><figure><img src='{uri(ib)}'><figcaption>B</figcaption></figure><figure><img src='{uri(ov)}'><figcaption>Differences</figcaption></figure></div>"
        status = "only in A" if i >= len(b) else "only in B" if i >= len(a) else ("changed" if diff or vis > 0.05 else "same")
        rows.append((i + 1, status, len([d for d in diff if d[0] in "+-"]), vis))
        if status != "same":
            lines = "".join(f"<div class='{'add' if d[0] == '+' else 'del'}'>{html.escape(d)}</div>" for d in diff[:200])
            sections.append(f"<section><h3>Page {i + 1} - {status}</h3>{imgs}{lines}</section>")
    table = "".join(f"<tr><td>{p}</td><td>{s}</td><td>{c}</td><td>{v:.2f}%</td></tr>" for p, s, c, v in rows)
    page = f"""<!doctype html><meta charset=utf-8><title>PDF comparison</title><style>
body{{font:15px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse}}td,th{{border:1px solid #8884;padding:.3rem .7rem}}
.im{{display:flex;gap:8px}}.im img{{width:100%;border:1px solid #8886}}figure{{margin:0;flex:1}}.add{{background:#c8f7c8;color:#064}}.del{{background:#ffd0d0;color:#700}}
div.add,div.del{{font-family:monospace;white-space:pre-wrap;padding:1px 6px}}</style>
<h1>PDF comparison</h1><p><b>A:</b> {html.escape(Path(files[0]).name)} ({len(a)} pages) &nbsp; <b>B:</b> {html.escape(Path(files[1]).name)} ({len(b)} pages)</p>
<table><tr><th>Page<th>Status<th>Changed lines<th>Visual diff</tr>{table}</table>{''.join(sections) or '<p>No differences found.</p>'}"""
    out = outp(w, "comparison.html")
    out.write_text(page, encoding="utf-8")
    return out


# ----------------------------------------------------------------------------- AI (needs ANTHROPIC_API_KEY)
def llm(system, prompt, max_tokens=3000):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ToolError("AI tools need ANTHROPIC_API_KEY set on the server.")
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=180,
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                       json={"model": os.environ.get("LLM_MODEL", "claude-haiku-5-5"), "max_tokens": max_tokens, "system": system,
                             "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])
    except httpx.HTTPError as e:
        raise ToolError(f"AI request failed: {str(e)[:120]}")


@tool("summarize", "AI Summarizer", "intelligence", "Concise summary with key points (sends the text to the Claude API).", ai=True,
      options=[O("length", "Length", "select", "medium", [["short", "Short"], ["medium", "Medium"], ["long", "Detailed"]]),
               O("language", "Output language", "text", "same as document"), PW])
def summarize(files, o, w):
    doc = open_pdf(files[0], o)
    text = "\n".join(p.get_text() for p in doc).strip()
    if not text:
        raise ToolError("No text found - run OCR first.")
    chunks = [text[i:i + 60000] for i in range(0, len(text), 60000)][:8]
    sysmsg = "You summarise documents faithfully. Never invent facts. Output Markdown."
    parts = [llm(sysmsg, f"Summarise this part of a document in bullet points:\n\n{c}", 1500) for c in chunks] if len(chunks) > 1 else [text]
    final = llm(sysmsg, f"Write a {o.get('length', 'medium')} summary (with a short 'Key points' list) in: {o.get('language', 'same as document')}.\n\n" + "\n\n".join(parts), 2000)
    out = outp(w, f"{stem(files[0])}_summary.md")
    out.write_text(final, encoding="utf-8")
    return out


@tool("translate", "Translate PDF", "intelligence", "Translate text in place, keeping the layout (sends text to the Claude API).", ai=True,
      options=[O("target", "Translate to", "text", "Bengali"), PW])
def translate(files, o, w):
    doc = open_pdf(files[0], o)
    target = str(o.get("target", "")).strip()
    if not target:
        raise ToolError("Enter the target language.")
    for page in doc:
        upright(page)
        blocks = []
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            txt = "\n".join("".join(s["text"] for s in l["spans"]) for l in b["lines"]).strip()
            sp = b["lines"][0]["spans"][0] if b["lines"] and b["lines"][0]["spans"] else None
            if txt and sp:
                blocks.append((pymupdf.Rect(b["bbox"]), txt, sp["size"], sp["color"]))
        if not blocks:
            continue
        reply = llm("You are a translation engine. Return ONLY a JSON array of strings, same length and order as the input.",
                    f"Translate each string to {target}:\n{json.dumps([b[1] for b in blocks], ensure_ascii=False)}", 4000)
        try:
            tr = json.loads(reply[reply.index("["):reply.rindex("]") + 1])
            assert len(tr) == len(blocks)
        except Exception:
            raise ToolError("The AI returned an unexpected format; please retry.")
        for r, *_ in blocks:
            page.add_redact_annot(r, fill=(1, 1, 1))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
        for (r, _, size, c), t in zip(blocks, tr):
            f = Fnt(page, t)
            col = (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)
            s = size
            while s >= 5 and page.insert_textbox(r, t, fontsize=s, fontname=f.name, color=col) < 0:
                s *= .9
    return save(doc, outp(w, f"{stem(files[0])}_{re.sub(r'[^A-Za-z]', '', target)[:12]}.pdf"))
