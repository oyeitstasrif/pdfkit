import io, json, shutil, zipfile
import pymupdf, pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import app
from app import tools as T

c = TestClient(app)


def mkpdf(n=3, prefix="Hello"):
    d = pymupdf.open()
    for i in range(n):
        p = d.new_page()
        p.insert_text((72, 100), f"{prefix} page {i + 1}", fontsize=24)
        p.insert_text((72, 150), "Contact: jane.doe@example.com Secret123", fontsize=12)
    return d.tobytes()


def png(color="red", size=(300, 200)):
    b = io.BytesIO()
    im = Image.new("RGB", size, color)
    ImageDraw.Draw(im).rectangle([20, 20, 120, 80], fill="blue")
    im.save(b, "PNG")
    return b.getvalue()


def run(tool, files, opts=None, aux=None, ok=True):
    r = c.post(f"/api/run/{tool}", files=[("files", f) for f in files] + [("aux", a) for a in (aux or [])],
               data={"options": json.dumps(opts or {})})
    if ok:
        assert r.status_code == 200, r.text
    return r


def pdf_of(r):
    return pymupdf.open(stream=r.content, filetype="pdf")


def F(name, data, mt="application/pdf"):
    return (name, data, mt)


A = F("a.pdf", mkpdf(3))


def test_all_tools_registered():
    names = {t["id"] for t in c.get("/api/tools").json()}
    assert len(names) >= 30 and {"merge", "split", "compress", "ocr", "redact", "sign"} <= names


def test_merge():
    r = run("merge", [A, F("b.pdf", mkpdf(2, "Second"))])
    d = pdf_of(r)
    assert len(d) == 5 and "Second" in d[3].get_text()


def test_merge_needs_two():
    assert run("merge", [A], ok=False).status_code == 400


def test_split_modes():
    z = zipfile.ZipFile(io.BytesIO(run("split", [A], {"mode": "ranges", "ranges": "1-2;3"}).content))
    assert len(z.namelist()) == 2
    z = zipfile.ZipFile(io.BytesIO(run("split", [A], {"mode": "every", "every": 1}).content))
    assert len(z.namelist()) == 3
    assert len(pdf_of(run("split", [A], {"mode": "extract", "ranges": "1,3"}))) == 2


def test_extract_remove():
    assert "page 2" in pdf_of(run("extract_pages", [A], {"pages": "2"}))[0].get_text()
    d = pdf_of(run("remove_pages", [A], {"pages": "1"}))
    assert len(d) == 2 and "page 2" in d[0].get_text()
    assert run("remove_pages", [A], {"pages": "1-3"}, ok=False).status_code == 400
    assert run("remove_pages", [A], {"pages": "9"}, ok=False).status_code == 400


def test_organize():
    plan = [{"page": 3}, {"page": 1, "rotate": 90}, {"blank": True}]
    d = pdf_of(run("organize", [A], {"plan": plan}))
    assert len(d) == 3 and "page 3" in d[0].get_text() and d[1].rotation == 90 and not d[2].get_text().strip()
    assert "page 2" in pdf_of(run("organize", [A], {"order": "2"}))[0].get_text()


def test_compress_reduces_image_pdf():
    d = pymupdf.open()
    import os
    noise = Image.frombytes("RGB", (1500, 1500), os.urandom(1500 * 1500 * 3))
    b = io.BytesIO()
    noise.save(b, "PNG")
    d.new_page().insert_image(pymupdf.Rect(0, 0, 500, 500), stream=b.getvalue())
    src = d.tobytes()
    r = run("compress", [F("big.pdf", src)], {"level": "extreme"})
    assert len(r.content) < len(src) * 0.5 and int(r.headers["x-original-size"]) == len(src)
    assert len(pdf_of(r)) == 1


def test_repair_truncated():
    good = mkpdf(2)
    r = run("repair", [F("bad.pdf", good[:-60])], ok=False)
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert len(pdf_of(r)) >= 1
    assert run("repair", [F("junk.pdf", b"not a pdf at all")], ok=False).status_code == 400


def test_ocr():
    d = pymupdf.open()
    im = Image.new("RGB", (1000, 300), "white")
    ImageDraw.Draw(im).text((40, 100), "INVOICE TOTAL 1234", fill="black", font_size=60)
    b = io.BytesIO(); im.save(b, "PNG")
    d.new_page(width=500, height=150).insert_image(pymupdf.Rect(0, 0, 500, 150), stream=b.getvalue())
    res = pdf_of(run("ocr", [F("scan.pdf", d.tobytes())], {"lang": "eng"}))
    assert "INVOICE" in res[0].get_text().upper()
    assert run("ocr", [A], {"lang": "eng;rm"}, ok=False).status_code == 400


def test_images_to_pdf():
    r = run("jpg_to_pdf", [F("1.png", png("red"), "image/png"), F("2.png", png("green", (200, 400)), "image/png")], {"pagesize": "a4", "margin": "small"})
    d = pdf_of(r)
    assert len(d) == 2 and round(d[0].rect.width) == 595
    z = zipfile.ZipFile(io.BytesIO(run("jpg_to_pdf", [F("1.png", png()), F("2.png", png())], {"merge": False}).content))
    assert len(z.namelist()) == 2
    assert run("jpg_to_pdf", [F("x.png", b"zzz")], ok=False).status_code == 400
    assert run("jpg_to_pdf", [F("x.exe", b"zzz")], ok=False).status_code == 400


def test_scan_to_pdf():
    assert len(pdf_of(run("scan_to_pdf", [F("s.jpg", png(), "image/jpeg")]))) == 1


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice missing")
def test_office_to_pdf():
    import docx, openpyxl, pptx
    b = io.BytesIO(); dd = docx.Document(); dd.add_paragraph("Word body text"); dd.save(b)
    assert "Word body text" in pdf_of(run("word_to_pdf", [F("d.docx", b.getvalue())]))[0].get_text()
    b = io.BytesIO(); wb = openpyxl.Workbook(); wb.active["A1"] = "Cell value"; wb.save(b)
    assert "Cell value" in pdf_of(run("excel_to_pdf", [F("x.xlsx", b.getvalue())]))[0].get_text()
    b = io.BytesIO(); pr = pptx.Presentation(); s = pr.slides.add_slide(pr.slide_layouts[0]); s.shapes.title.text = "Slide title"; pr.save(b)
    assert "Slide title" in pdf_of(run("powerpoint_to_pdf", [F("p.pptx", b.getvalue())]))[0].get_text()


def test_html_to_pdf_file_and_ssrf():
    r = run("html_to_pdf", [F("p.html", b"<h1>Hello HTML</h1><p>para</p>", "text/html")])
    assert "Hello HTML" in pdf_of(r)[0].get_text()
    for bad in ("http://127.0.0.1:8000/api/health", "http://169.254.169.254/latest", "file:///etc/passwd", "ftp://x.y"):
        assert run("html_to_pdf", [], {"url": bad}, ok=False).status_code == 400, bad
    r = run("html_to_pdf", [F("p.html", b"<img src='http://127.0.0.1/x.png'>", "text/html")], ok=False)
    assert r.status_code in (200, 400)  # blocked subresource must not leak


def test_pdf_to_jpg():
    z = zipfile.ZipFile(io.BytesIO(run("pdf_to_jpg", [A], {"dpi": 72}).content))
    assert len(z.namelist()) == 3 and z.namelist()[0].endswith(".jpg")
    d = pymupdf.open(); d.new_page().insert_image(pymupdf.Rect(0, 0, 100, 100), stream=png())
    r = run("pdf_to_jpg", [F("i.pdf", d.tobytes())], {"mode": "extract"})
    assert r.content[:4] == b"\x89PNG"
    assert run("pdf_to_jpg", [A], {"mode": "extract"}, ok=False).status_code == 400


def test_pdf_to_word_excel_ppt_md_pdfa():
    import docx
    r = run("pdf_to_word", [A])
    assert "Hello page 1" in "\n".join(p.text for p in docx.Document(io.BytesIO(r.content)).paragraphs)
    from pptx import Presentation
    assert len(Presentation(io.BytesIO(run("pdf_to_powerpoint", [A]).content)).slides) == 3
    t = Presentation(io.BytesIO(run("pdf_to_powerpoint", [A], {"mode": "text"}).content))
    assert "Hello" in " ".join(sh.text_frame.text for sh in t.slides[0].shapes if sh.has_text_frame)
    md = run("pdf_to_markdown", [A]).text
    assert "Hello page 1" in md
    # table -> xlsx
    d = pymupdf.open(); pg = d.new_page()
    for i in range(3):
        for j in range(3):
            r_ = pymupdf.Rect(72 + j * 80, 100 + i * 25, 152 + j * 80, 125 + i * 25)
            pg.draw_rect(r_); pg.insert_textbox(r_, f"r{i}c{j}", fontsize=11)
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(run("pdf_to_excel", [F("t.pdf", d.tobytes())]).content))
    assert wb.worksheets[0]["B2"].value == "r1c1"
    if shutil.which("gs"):
        pa = run("pdf_to_pdfa", [A]).content
        import pikepdf
        with pikepdf.open(io.BytesIO(pa)) as p:
            assert "pdfaid" in str(p.open_metadata()) or b"pdfaid" in pa


def test_to_scanned_pdf_word_gives_hint():
    d = pymupdf.open(); d.new_page().insert_image(pymupdf.Rect(0, 0, 100, 100), stream=png())
    r = run("pdf_to_word", [F("s.pdf", d.tobytes())], ok=False)
    assert r.status_code == 400 and "OCR" in r.json()["detail"]


def test_rotate_batch():
    r = run("rotate", [F("a.pdf", mkpdf(2)), F("b.pdf", mkpdf(1))], {"angle": 90, "pages": ""})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert len(z.namelist()) == 2 and all(pymupdf.open(stream=z.read(n), filetype="pdf")[0].rotation == 90 for n in z.namelist())
    d = pdf_of(run("rotate", [A], {"angle": 180, "pages": "2"}))
    assert [p.rotation for p in d] == [0, 180, 0]


def test_page_numbers_incl_rotated_page():
    d = pdf_of(run("page_numbers", [A], {"format": "Page {n} of {total}", "start": 5, "position": "top-right"}))
    assert "Page 5 of 7" in d[0].get_text() and "Page 7 of 7" in d[2].get_text()
    rot = pymupdf.open(stream=mkpdf(1)); rot[0].set_rotation(90)
    out = pdf_of(run("page_numbers", [F("r.pdf", rot.tobytes())], {"format": "{n}"}))
    assert out[0].get_text().count("1") >= 1


def test_watermark_text_image_and_angle():
    d = pdf_of(run("watermark", [A], {"text": "DRAFT", "angle": 45, "opacity": 0.4, "pages": "1"}))
    assert "DRAFT" in d[0].get_text() and "DRAFT" not in d[1].get_text()
    line = [l for b in d[0].get_text("dict")["blocks"] if b["type"] == 0 for l in b["lines"] if "DRAFT" in "".join(s["text"] for s in l["spans"])][0]
    dx, dy = line["dir"]
    assert dx > 0.6 and dy < -0.6, line["dir"]          # text climbs to the upper-right (45° CCW)
    d = pdf_of(run("watermark", [A], {"type": "image", "scale": 30}, aux=[F("w.png", png(), "image/png")]))
    assert d[0].get_images()
    assert run("watermark", [A], {"type": "image"}, ok=False).status_code == 400
    assert "DRAFT" in pdf_of(run("watermark", [A], {"text": "DRAFT", "position": "tile", "layer": "under"}))[1].get_text()


def test_crop_by_rect_and_margins():
    ops = [{"type": "crop", "page": 1, "x0": 50, "y0": 50, "x1": 350, "y1": 250}]
    d = pdf_of(run("crop", [A], {"ops": ops, "pages": "1"}))
    assert round(d[0].rect.width) == 300 and round(d[0].rect.height) == 200 and round(d[1].rect.width) == 595
    d = pdf_of(run("crop", [A], {"top": 10, "bottom": 10, "left": 20, "right": 20}))
    assert round(d[0].rect.width) == 555
    assert run("crop", [A], {"top": 5000}, ok=False).status_code == 400


def test_edit_all_op_types():
    ops = [{"type": "text", "page": 1, "x0": 100, "y0": 300, "text": "Added text\nline two", "size": 16, "color": "#0000ff"},
           {"type": "rect", "page": 1, "x0": 50, "y0": 400, "x1": 150, "y1": 450, "fill": "#00ff00"},
           {"type": "ellipse", "page": 1, "x0": 200, "y0": 400, "x1": 300, "y1": 450},
           {"type": "line", "page": 1, "x0": 50, "y0": 500, "x1": 300, "y1": 520},
           {"type": "highlight", "page": 1, "x0": 72, "y0": 85, "x1": 250, "y1": 105},
           {"type": "ink", "page": 1, "points": [[10, 10], [30, 40], [60, 20]]},
           {"type": "image", "page": 1, "x0": 400, "y0": 400, "x1": 500, "y1": 470, "data": "data:image/png;base64," + __import__("base64").b64encode(png()).decode()}]
    d = pdf_of(run("edit", [A], {"ops": ops}))
    assert "Added text" in d[0].get_text() and "line two" in d[0].get_text() and d[0].get_images() and len(d[0].get_drawings()) >= 4
    assert run("edit", [A], {"ops": []}, ok=False).status_code == 400


def test_edit_non_latin_text():
    ops = [{"type": "text", "page": 1, "x0": 100, "y0": 300, "text": "Привет мир", "size": 18}]
    assert "Привет" in pdf_of(run("edit", [A], {"ops": ops}))[0].get_text()


def test_forms_create_fill_flatten():
    ops = [{"type": "text", "page": 1, "x0": 100, "y0": 300, "x1": 300, "y1": 325, "name": "full_name"},
           {"type": "checkbox", "page": 1, "x0": 100, "y0": 340, "x1": 118, "y1": 358, "name": "agree"}]
    form = run("forms_create", [A], {"ops": ops}).content
    info = c.post("/api/info", files={"file": ("f.pdf", form, "application/pdf")}).json()
    assert {f["name"] for f in info["fields"]} == {"full_name", "agree"}
    filled = run("forms_fill", [F("f.pdf", form)], {"values": {"full_name": "Jane Roe", "agree": True}}).content
    vals = {w.field_name: w.field_value for w in pymupdf.open(stream=filled, filetype="pdf")[0].widgets()}
    assert vals["full_name"] == "Jane Roe" and vals["agree"] not in ("Off", False)
    flat = run("forms_fill", [F("f.pdf", form)], {"values": {"full_name": "Jane Roe"}, "flatten": True}).content
    d = pymupdf.open(stream=flat, filetype="pdf")
    assert not list(d[0].widgets()) and "Jane Roe" in d[0].get_text()
    assert run("forms_fill", [A], {"values": {"nope": "x"}}, ok=False).status_code == 400


def test_forms_auto_detect_blanks():
    d = pymupdf.open(); d.new_page().insert_text((72, 100), "Name: ______________  Date: ________", fontsize=12)
    out = run("forms_create", [F("b.pdf", d.tobytes())], {"auto": True}).content
    assert len(list(pymupdf.open(stream=out, filetype="pdf")[0].widgets())) == 2


def test_protect_unlock_roundtrip():
    enc = run("protect", [A], {"user_password": "s3cret", "allow_copy": False}).content
    assert pymupdf.open(stream=enc, filetype="pdf").needs_pass
    assert run("merge", [F("e.pdf", enc), A], ok=False).status_code == 400          # needs password
    assert len(pdf_of(run("merge", [F("e.pdf", enc), A], {"password": "s3cret"}))) == 6
    assert run("unlock", [F("e.pdf", enc)], {"password": "wrong"}, ok=False).status_code == 400
    plain = run("unlock", [F("e.pdf", enc)], {"password": "s3cret"}).content
    assert not pymupdf.open(stream=plain, filetype="pdf").needs_pass
    assert run("protect", [A], {}, ok=False).status_code == 400


def test_sign_drawn_and_typed():
    sig = "data:image/png;base64," + __import__("base64").b64encode(png("white", (300, 100))).decode()
    ops = [{"type": "sign", "page": 2, "x0": 100, "y0": 600, "x1": 260, "y1": 650}]
    d = pdf_of(run("sign", [A], {"signature": sig, "ops": ops, "date": True}))
    assert d[1].get_images() and not d[0].get_images()
    d = pdf_of(run("sign", [A], {"name": "Jane Roe"}))
    assert "Jane Roe" in d[2].get_text()
    assert run("sign", [A], {}, ok=False).status_code == 400


def test_redact_really_removes_text():
    r = run("redact", [A], {"terms": "Secret123", "regex": r"[\w.]+@[\w.]+\.\w+"})
    d = pdf_of(r)
    for p in d:
        assert "Secret123" not in p.get_text() and "example.com" not in p.get_text() and "Hello" in p.get_text()
    assert b"Secret123" not in r.content
    ops = [{"type": "redact", "page": 1, "x0": 60, "y0": 70, "x1": 400, "y1": 110}]
    assert "Hello page 1" not in pdf_of(run("redact", [A], {"ops": ops}))[0].get_text()
    assert run("redact", [A], {"terms": "zzzzzz"}, ok=False).status_code == 400
    assert run("redact", [A], {"regex": "(("}, ok=False).status_code == 400


def test_compare():
    b = F("b.pdf", mkpdf(3, "Changed"))
    html = run("compare", [A, b]).text
    assert "Page 1 - changed" in html and "Changed" in html
    same = run("compare", [A, A]).text
    assert "No differences found" in same
    assert run("compare", [A], ok=False).status_code == 400


def test_ai_tools_with_mocked_llm(monkeypatch):
    def fake(system, prompt, max_tokens=0):
        if "ONLY a JSON array" in system:
            return json.dumps(["Hola"] * len(json.loads(prompt[prompt.index("["):])))
        return "- point one\n- point two"
    monkeypatch.setattr(T, "llm", fake)
    assert "point one" in run("summarize", [A], {}).text
    d = pdf_of(run("translate", [A], {"target": "Spanish"}))
    assert "Hola" in d[0].get_text() and "Contact:" not in d[0].get_text()


def test_ai_tools_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = run("summarize", [A], {}, ok=False)
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_workflow_chain():
    steps = [{"tool": "rotate", "options": {"angle": 90}}, {"tool": "watermark", "options": {"text": "WF"}}, {"tool": "compress", "options": {}}]
    r = c.post("/api/workflow", files=[("files", A)], data={"steps": json.dumps(steps)})
    assert r.status_code == 200
    d = pdf_of(r)
    assert d[0].rect.width > d[0].rect.height and "WF" in d[0].get_text()   # rotated page keeps its landscape look
    bad = [{"tool": "pdf_to_jpg", "options": {}}, {"tool": "rotate", "options": {}}]
    assert c.post("/api/workflow", files=[("files", A)], data={"steps": json.dumps(bad)}).status_code == 400
    assert c.post("/api/workflow", files=[("files", A)], data={"steps": "[]"}).status_code == 400


def test_preview_and_limits(monkeypatch):
    info = c.post("/api/info", files={"file": ("a.pdf", A[1], "application/pdf")}).json()
    assert len(info["pages"]) == 3
    r = c.get(f"/api/preview/{info['id']}/2?zoom=0.5")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert c.get(f"/api/preview/{info['id']}/9").status_code == 404
    assert c.get("/api/preview/../../etc/passwd/1").status_code in (404, 422)
    assert c.get("/api/preview/" + "0" * 32 + "/1").status_code == 404
    import app.main as m
    monkeypatch.setattr(m, "MAX_MB", 0)
    assert run("rotate", [A], ok=False).status_code == 413


def test_unknown_and_empty():
    assert c.post("/api/run/nope", data={"options": "{}"}).status_code == 404
    assert run("rotate", [F("e.pdf", b"")], ok=False).status_code == 400
    assert run("rotate", [A], ok=False, opts={"angle": 45}).status_code == 400


def test_health_reports_engines():
    h = c.get("/api/health").json()
    assert set(h["engines"]) == {"soffice", "tesseract", "gs", "weasyprint"}
    avail = {t["id"]: t["available"] for t in c.get("/api/tools").json()}
    assert avail["merge"] is True and avail["ocr"] == h["engines"]["tesseract"]


def test_find_exe_windows_style_install_dir(tmp_path, monkeypatch):
    fake = tmp_path / "Tesseract-OCR" / "tesseract.exe"
    fake.parent.mkdir(); fake.write_text("x")
    monkeypatch.setattr(T.shutil, "which", lambda n: None)                       # not on PATH, like a default Windows install
    monkeypatch.delenv("TESSERACT_PATH", raising=False)
    monkeypatch.setitem(T.ENGINES, "tesseract", (*T.ENGINES["tesseract"][:2], [str(tmp_path / "Tesseract-*" / "tesseract.exe")], "TESSERACT_PATH"))
    assert T.find_exe("tesseract") == str(fake)
    monkeypatch.setenv("GS_PATH", str(fake))
    assert T.find_exe("gs") == str(fake)                                          # env override wins


def test_missing_engine_gives_clear_error(monkeypatch):
    monkeypatch.setattr(T.shutil, "which", lambda n: None)
    for k in T.ENGINES:
        monkeypatch.delenv(T.ENGINES[k][3], raising=False)
        monkeypatch.setitem(T.ENGINES, k, (*T.ENGINES[k][:2], [], T.ENGINES[k][3]))
    r = run("ocr", [F("s.pdf", pymupdf.open().tobytes() if False else __import__("tests.test_tools", fromlist=["x"]).mkpdf(1))], {"force": True}, ok=False)
    assert r.status_code == 400 and "Tesseract OCR is not installed" in r.json()["detail"]
    r = run("pdf_to_pdfa", [A], ok=False)
    assert r.status_code == 400 and "Ghostscript is not installed" in r.json()["detail"]


def test_info_then_files_are_closed_and_encrypted_preview():
    enc = run("protect", [A], {"user_password": "pw"}).content
    r = c.post("/api/info", files={"file": ("e.pdf", enc, "application/pdf")}, data={"password": "pw"})
    assert r.status_code == 200
    assert c.get(f"/api/preview/{r.json()['id']}/1?zoom=.4").status_code == 200
    assert c.post("/api/info", files={"file": ("e.pdf", enc, "application/pdf")}, data={"password": "no"}).status_code == 400


def test_password_protected_input_works_for_converters():
    import docx
    enc = run("protect", [A], {"user_password": "pw"}).content
    r = run("pdf_to_word", [F("e.pdf", enc)], {"password": "pw"})
    assert "Hello page 1" in "\n".join(p.text for p in docx.Document(io.BytesIO(r.content)).paragraphs)
    assert run("pdf_to_word", [F("e.pdf", enc)], ok=False).status_code == 400
    assert run("pdf_to_excel", [F("e.pdf", enc)], {"password": "pw"}).status_code == 200
