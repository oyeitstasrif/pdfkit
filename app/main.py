"""PDFKit server. Run:  uvicorn app.main:app --port 8000"""
import gc, json, logging, os, re, shutil, tempfile, time, uuid, zipfile
from pathlib import Path

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import tools as T

log = logging.getLogger("pdfkit")
MAX_MB = int(os.environ.get("MAX_MB", 100))          # per file
MAX_FILES = int(os.environ.get("MAX_FILES", 30))
SESSION_TTL = int(os.environ.get("SESSION_TTL", 3600))
SESS = Path(tempfile.gettempdir()) / "pdfkit_sessions"
SESS.mkdir(exist_ok=True)
WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="PDFKit", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.exception_handler(T.ToolError)
async def tool_error(_, e):
    return JSONResponse({"detail": str(e)}, status_code=400)


def safe_name(name, taken):
    base = re.sub(r"[^\w.\- ]", "_", Path(name or "file").name)[:120] or "file"
    n, stem, suf = 1, Path(base).stem, Path(base).suffix.lower()
    while base in taken:
        n += 1
        base = f"{stem}_{n}{suf}"
    taken.add(base)
    return base


def save_uploads(items, folder, allowed):
    folder.mkdir(parents=True, exist_ok=True)
    taken, paths = set(), []
    for up in items:
        name = safe_name(up.filename, taken)
        if "*" not in allowed and Path(name).suffix.lower() not in allowed:
            raise HTTPException(400, f"'{name}' has an unsupported type. Allowed: {', '.join(allowed)}")
        p, size = folder / name, 0
        with open(p, "wb") as f:
            while chunk := up.file.read(1 << 20):
                size += len(chunk)
                if size > MAX_MB << 20:
                    raise HTTPException(413, f"'{name}' is larger than {MAX_MB} MB")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, f"'{name}' is empty")
        paths.append(p)
    return paths


def execute(t, files, opts, work):
    outs = []
    for r in ([t["fn"](files, opts, work)] if t["multi"] else [t["fn"]([f], opts, work) for f in files]):
        outs += r if isinstance(r, list) else [r]
    if not outs:
        raise T.ToolError("Nothing to process.")
    if len(outs) == 1:
        return outs[0]
    z, used = work / "result.zip", set()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in outs:
            zf.write(p, safe_name(p.name, used))
    return z


def parse_opts(s):
    try:
        o = json.loads(s or "{}")
        assert isinstance(o, dict)
        return o
    except Exception:
        raise HTTPException(400, "Invalid options")


def check_count(t, n):
    if n < t["min"]:
        raise HTTPException(400, f"{t['name']} needs at least {t['min']} file(s)")
    if (t["max"] and n > t["max"]) or n > MAX_FILES:
        raise HTTPException(400, f"{t['name']} accepts at most {t['max'] or MAX_FILES} file(s)")


def cleanup(path):
    gc.collect()                      # release any PDF handles first (Windows cannot delete open files)
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists():
            return
        time.sleep(.3)


def respond(out, work, sizes=None):
    mt = {".pdf": "application/pdf", ".zip": "application/zip", ".html": "text/html", ".md": "text/markdown"}.get(out.suffix.lower())
    headers = {"X-Output-Size": str(out.stat().st_size)}
    if sizes:
        headers["X-Original-Size"] = str(sizes)
    headers["Access-Control-Expose-Headers"] = "X-Output-Size, X-Original-Size, Content-Disposition"
    return FileResponse(out, media_type=mt, filename=out.name, headers=headers,
                        background=BackgroundTask(cleanup, work))


@app.get("/api/tools")
def list_tools():
    st = T.engine_status()
    return [{**{k: v for k, v in t.items() if k != "fn"}, "available": st.get(t["needs"], True) if t["needs"] else True} for t in T.TOOLS.values()]


@app.post("/api/run/{tool_id}")
def run_tool(tool_id: str, files: list[UploadFile] = File(default=[]), aux: list[UploadFile] = File(default=[]), options: str = Form("{}")):
    t = T.TOOLS.get(tool_id)
    if not t:
        raise HTTPException(404, "Unknown tool")
    check_count(t, len(files))
    work = Path(tempfile.mkdtemp(prefix="pdfkit_"))
    try:
        opts = parse_opts(options)
        paths = save_uploads(files, work / "in", t["exts"])
        opts["_aux"] = save_uploads(aux, work / "aux", T.IMG + (".pdf",)) if aux else []
        out = execute(t, paths, opts, work)
        return respond(out, work, sum(p.stat().st_size for p in paths) if tool_id == "compress" else None)
    except BaseException as e:
        cleanup(work)
        if isinstance(e, (T.ToolError, HTTPException)):
            raise
        log.exception("tool %s failed", tool_id)
        raise HTTPException(500, "Something went wrong while processing this file.")


@app.post("/api/workflow")
def run_workflow(files: list[UploadFile] = File(...), steps: str = Form(...), aux: list[UploadFile] = File(default=[])):
    try:
        plan = json.loads(steps)
        assert isinstance(plan, list) and 1 <= len(plan) <= 10
    except Exception:
        raise HTTPException(400, "A workflow needs 1-10 steps")
    work = Path(tempfile.mkdtemp(prefix="pdfkit_wf_"))
    try:
        first = T.TOOLS.get(plan[0].get("tool"))
        if not first:
            raise HTTPException(404, "Unknown tool in workflow")
        cur = save_uploads(files, work / "in", first["exts"])
        auxp = save_uploads(aux, work / "aux", T.IMG) if aux else []
        out = None
        for i, step in enumerate(plan):
            t = T.TOOLS.get(step.get("tool"))
            if not t:
                raise HTTPException(404, f"Unknown tool: {step.get('tool')}")
            if any(Path(p).suffix.lower() not in t["exts"] for p in cur):
                raise HTTPException(400, f"Step {i + 1} ({t['name']}) cannot accept the previous output")
            check_count(t, len(cur))
            sw = work / f"step{i}"
            sw.mkdir()
            o = dict(step.get("options") or {})
            o["_aux"] = auxp
            out = execute(t, cur, o, sw)
            if i < len(plan) - 1:
                if out.suffix.lower() == ".zip":
                    raise HTTPException(400, f"Step {i + 1} ({t['name']}) produced several files; it can only be the last step")
                cur = [out]
        return respond(out, work)
    except BaseException as e:
        cleanup(work)
        if isinstance(e, (T.ToolError, HTTPException)):
            raise
        log.exception("workflow failed")
        raise HTTPException(500, "Something went wrong while processing this file.")


# --- document sessions for previews / form-field discovery -------------------------------------
def purge():
    now = time.time()
    for d in SESS.iterdir():
        if now - d.stat().st_mtime > SESSION_TTL:
            cleanup(d)


@app.post("/api/info")
def info(file: UploadFile = File(...), password: str = Form("")):
    purge()
    sid = uuid.uuid4().hex
    d = SESS / sid
    p = save_uploads([file], d, (".pdf",))[0]
    try:
        try:
            with pymupdf.open(p) as probe:
                was_encrypted = probe.needs_pass       # authenticate() clears is_encrypted, so ask first
        except Exception:
            was_encrypted = False
        doc = T.open_pdf(p, {"password": password})
        fields = [dict(name=w.field_name, type=w.field_type_string, page=pg.number + 1, value=w.field_value, choices=w.choice_values or [])
                  for pg in doc for w in (pg.widgets() or [])]
        pages = [{"w": round(pg.rect.width, 2), "h": round(pg.rect.height, 2)} for pg in doc]
        if was_encrypted:                          # keep a decrypted copy so previews work
            T.save(doc, d / "view.pdf")
        doc.close()                                # Windows: a file cannot be renamed/deleted while open
        if (d / "view.pdf").exists():
            p.unlink()
            (d / "view.pdf").rename(d / "doc.pdf")
        else:
            p.rename(d / "doc.pdf")
    except BaseException:
        cleanup(d)
        raise
    return {"id": sid, "pages": pages, "fields": fields, "title": Path(file.filename or "").name}


@app.get("/api/preview/{sid}/{n}")
def preview(sid: str, n: int, zoom: float = 1.5):
    if not re.fullmatch(r"[0-9a-f]{32}", sid):
        raise HTTPException(404)
    p = SESS / sid / "doc.pdf"
    if not p.exists():
        raise HTTPException(404, "Preview expired - reload the file")
    with pymupdf.open(p) as doc:
        if not 1 <= n <= len(doc):
            raise HTTPException(404)
        pix = doc[n - 1].get_pixmap(matrix=pymupdf.Matrix(*(min(max(zoom, .1), 3),) * 2), alpha=False)
        png = pix.tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@app.get("/api/health")
def health():
    return {"ok": True, "tools": len(T.TOOLS), "engines": T.engine_status()}


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
