// Frontend smoke test: node smoke.mjs  (server must run on BASE, default http://localhost:8765)
import { JSDOM, VirtualConsole } from "jsdom";
import assert from "node:assert/strict";
const BASE = process.env.BASE || "http://localhost:8765";
const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", e => errors.push(String(e.stack || e)));
vc.on("error", e => errors.push(String(e)));
const html = await (await fetch(BASE + "/")).text();
const dom = new JSDOM(html, { url: BASE + "/", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: vc,
  beforeParse(w) {
    w.fetch = (u, o) => fetch(new URL(u, BASE), o);
    w.scrollTo = () => {};
    w.prompt = () => "prompted";
    w.SVGElement.prototype.createSVGPoint = function () { return { x: 0, y: 0, matrixTransform() { return { x: this.x, y: this.y }; } }; };
    w.SVGElement.prototype.getScreenCTM = () => ({ inverse: () => ({}) });
    w.SVGElement.prototype.setPointerCapture = () => {};
    w.HTMLCanvasElement.prototype.getContext = () => ({ beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, clearRect() {} });
  } });
const w = dom.window, d = w.document;
const wait = ms => new Promise(r => setTimeout(r, ms));
await wait(600);
const cards = [...d.querySelectorAll("a.card")];
assert.ok(cards.length >= 34, "cards on home: " + cards.length);
console.log("home cards:", cards.length);

// every tool page renders without errors
for (const c of cards) {
  w.location.hash = c.getAttribute("href");
  await wait(25);
  assert.ok(d.querySelector("h1")?.textContent, "title for " + c.getAttribute("href"));
  assert.ok(d.querySelector(".drop"), "dropzone for " + c.getAttribute("href"));
}
console.log("tool pages rendered OK");

// search filter
w.location.hash = "#/";
await wait(30);
const q = d.getElementById("q"); q.value = "watermark"; q.dispatchEvent(new w.Event("input"));
await wait(30);
assert.equal(d.querySelectorAll("a.card").length, 1);
q.value = ""; q.dispatchEvent(new w.Event("input"));

// visual editors against a real document
const pdf = await (await fetch(BASE + "/api/tools")).json();
const body = new FormData();
const mk = await import("node:child_process");
const bytes = mk.execFileSync("python3", ["-W", "ignore", "-c", "import pymupdf,sys;d=pymupdf.open();[d.new_page().insert_text((72,100),'P%d'%i) for i in range(3)];sys.stdout.buffer.write(d.tobytes())"], { maxBuffer: 1 << 24 });
body.append("file", new Blob([bytes], { type: "application/pdf" }), "t.pdf");
const info = await (await fetch(BASE + "/api/info", { method: "POST", body })).json();
assert.equal(info.pages.length, 3);
const ev = (type, x, y) => Object.assign(new w.Event(type, { bubbles: true }), { clientX: x, clientY: y, pointerId: 1 });
const drag = (svg, x0, y0, x1, y1) => { svg.dispatchEvent(ev("pointerdown", x0, y0)); svg.dispatchEvent(ev("pointermove", x1, y1)); svg.dispatchEvent(ev("pointerup", x1, y1)); };

for (const vis of ["redact", "crop", "sign", "edit", "forms_create", "organize", "forms_fill"]) {
  const S = { files: [], opts: {}, aux: [], ops: [], plan: info.pages.map((_, i) => ({ page: i + 1, rotate: 0 })), values: {}, info };
  const el = w.eval("buildEditor")({ visual: vis }, S);
  d.body.append(el);
  const svg = el.querySelector("svg");
  if (svg) {
    drag(svg, 10, 10, 120, 80);
    if (vis === "edit") {                       // click-text, then shapes via tab buttons
      svg.dispatchEvent(ev("pointerdown", 30, 30));
      const btns = [...el.querySelectorAll(".tabs button")];
      for (const b of btns.slice(1)) { b.click(); drag(svg, 20, 20, 90, 70); }
      assert.equal(S.ops.length, 6, "edit ops " + S.ops.length);
      assert.deepEqual(S.ops.map(o => o.type), ["text", "text", "rect", "ellipse", "line", "highlight"]);
    } else assert.equal(S.ops.length, 1, vis + " ops");
    assert.ok(svg.children.length >= 1, vis + " draws shapes");
    const del = el.querySelector("ul.files button"); del.click();
    assert.equal(S.ops.length, vis === "edit" ? 5 : 0, vis + " delete");
  }
  if (vis === "organize") {
    el.querySelectorAll(".tile")[0].querySelectorAll("button")[2].click();   // rotate right on first tile
    assert.equal(S.plan[0].rotate, 90);
    [...el.querySelectorAll("button")].find(b => b.textContent === "+ Blank page").click();
    assert.equal(S.plan.length, 4);
    el.querySelectorAll(".tile")[1].querySelectorAll("button")[3].click();   // delete tile 2
    assert.equal(S.plan.length, 3);
  }
  if (vis === "forms_fill") assert.ok(el.textContent.includes("No fillable form fields"));
  console.log("editor OK:", vis);
}

// full run: rotate via UI request path (uses Node FormData compat shim)
const fd = new FormData();
fd.append("files", new Blob([bytes], { type: "application/pdf" }), "t.pdf");
fd.append("options", JSON.stringify({ angle: 90 }));
const status = d.createElement("div");
w.URL.createObjectURL = () => "blob:x";
w.HTMLAnchorElement.prototype.click = function () {};
w.fetch = (u, o) => fetch(new URL(u, BASE), o);
const blob = await w.eval("post")("/api/run/rotate", fd, status);
assert.ok(blob && blob.size > 100, "post returns blob");
assert.ok(status.textContent.includes("Done"), status.textContent);
const bad = new FormData(); bad.append("options", "{}");
await w.eval("post")("/api/run/merge", bad, status);
assert.ok(status.querySelector(".err"), "error banner shown");
console.log("post() success + error paths OK");

assert.deepEqual(errors, [], "page errors:\n" + errors.join("\n"));
console.log("ALL FRONTEND CHECKS PASSED");
process.exit(0);
