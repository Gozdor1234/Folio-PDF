// Folio PDF (browser edition): a fast PDF viewer for filling forms and signing.
// Runs entirely in the page. Files are read and written locally; nothing is uploaded.
import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs"; // legacy build: polyfilled for older Edge versions
import { PDFDocument, PDFName, StandardFonts, degrees, rgb } from "pdf-lib";
import WORKER_SRC from "./worker-src.js";

const PX_PER_PT = 96 / 72;
const MIN_ZOOM = 0.1;
const MAX_ZOOM = 6;
const MAX_CANVAS_PX = 32_000_000;
const TEXT_LINE_HEIGHT = 1.2; // must match .item.text .txt line-height in CSS
const ARIAL_ASCENT = 0.905;
const ARIAL_DESCENT = 0.212;
const SCRIPT_FONTS = ["Segoe Script", "Ink Free", "Lucida Handwriting", "Segoe Print", "Brush Script MT",
  "Freestyle Script", "Bradley Hand ITC", "Mistral", "Kristen ITC"];
const INK = { black: "#141414", blue: "#1034a6" };
const NO_FS = new URLSearchParams(location.search).has("nofs"); // test hook: force download fallback

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, attrs = {}) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem("folio." + key); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("folio." + key, JSON.stringify(value)); return true; } catch { return false; }
  },
};

// ------------------------------------------------------------------ state
const S = {
  pdf: null,          // PDF.js document
  bytes: null,        // original file bytes (Uint8Array), the base for every save
  name: "document.pdf",
  handle: null,       // FileSystemFileHandle when the browser gave us one
  password: null,
  pages: [],          // {i, page, w, h, rot, div, canvas, overlay, textLayer, renderedAt, task, near, overlayDone}
  zoom: 1,
  fields: new Map(),  // fieldName -> {kind, value, dirty, widgets: [{el, a}]}
  items: [],          // placed signatures/text: {id, kind, page, x, y, w, h, png, text, size, el}
  placing: null,      // {kind: "sig"|"text", png}
  selected: null,
  dirty: false,
  find: { query: "", hits: [], pos: -1, index: null },
  signedWarned: false,
};
let nextId = 1;

// ------------------------------------------------------------------ PDF.js worker
// The worker source is embedded in this file and started from a blob URL, so the
// page works offline and from a local file with no other files next to it.
pdfjs.GlobalWorkerOptions.workerPort = new Worker(
  URL.createObjectURL(new Blob([WORKER_SRC], { type: "text/javascript" })));

// ------------------------------------------------------------------ helpers
const viewer = $("#viewer");
const pagesEl = $("#pages");
const scale = () => S.zoom * PX_PER_PT;

function status(msg, isError = false) {
  const m = $("#statusMsg");
  m.textContent = msg || "";
  m.className = "msg" + (isError ? " err" : "");
}

function setDirty(on) {
  S.dirty = on;
  const s = $("#saveState");
  s.textContent = on ? "Unsaved changes" : "";
  s.className = on ? "dirty" : "";
  document.title = (on ? "• " : "") + (S.pdf ? S.name + " - " : "") + "Folio PDF";
}

function displayRect(p, rect) {
  // PDF user-space rect -> displayed page coords (points, top-left origin, rotation applied)
  const vp = p.page.getViewport({ scale: 1 });
  const [x1, y1] = vp.convertToViewportPoint(rect[0], rect[1]);
  const [x2, y2] = vp.convertToViewportPoint(rect[2], rect[3]);
  return [Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1)];
}

function place(e, r, s = scale()) {
  e._r = r;
  e.style.left = r[0] * s + "px";
  e.style.top = r[1] * s + "px";
  e.style.width = r[2] * s + "px";
  e.style.height = r[3] * s + "px";
  if (e._fs) e.style.fontSize = e._fs * s + "px";
}

// ------------------------------------------------------------------ opening
async function openFile(file, handle = null) {
  if (S.dirty && !confirm("Discard unsaved changes to " + S.name + "?")) return;
  const bytes = new Uint8Array(await file.arrayBuffer());
  await openBytes(bytes, file.name, handle);
}

async function openBytes(bytes, name, handle, keepView = false) {
  status("Opening " + name + "…");
  const view = keepView ? captureView() : null;
  const task = pdfjs.getDocument({
    data: bytes.slice(), // PDF.js transfers its buffer to the worker; keep our own copy
    useSystemFonts: true,
    isEvalSupported: false,
    enableXfa: false,
    password: keepView ? S.password ?? undefined : undefined,
  });
  let password = keepView ? S.password : null;
  task.onPassword = (update, reason) => {
    const pw = prompt(reason === pdfjs.PasswordResponses.INCORRECT_PASSWORD
      ? "Incorrect password. Try again:" : "This PDF is password protected. Password:");
    if (pw === null) { task.destroy(); return; }
    password = pw;
    update(pw);
  };
  let pdf;
  const slow = setTimeout(() => status("Still opening " + name + "… (large or damaged file?)"), 8000);
  try {
    pdf = await task.promise;
  } catch (e) {
    if (e?.name !== "AbortException") status("Could not open " + name + ": " + e.message, true);
    else status("");
    return false;
  } finally {
    clearTimeout(slow);
  }
  const oldTask = S.loadingTask;
  Object.assign(S, {
    loadingTask: task, pdf, bytes, name, handle, password, pages: [], fields: new Map(), items: [], placing: null, selected: null,
    find: { query: "", hits: [], pos: -1, index: null }, signedWarned: false,
  });
  setPlacing(null);
  for (const d of pagesEl.children) { nearObs.unobserve(d); farObs.unobserve(d); }
  pagesEl.textContent = "";
  oldTask?.destroy().catch(() => {});
  $("#welcome").hidden = true;
  setDirty(false);
  clearFind();

  const pages = await Promise.all(Array.from({ length: pdf.numPages }, (_, k) => pdf.getPage(k + 1)));
  for (const [k, page] of pages.entries()) {
    const vp = page.getViewport({ scale: 1 });
    const div = el("div", "page");
    const overlay = el("div", "overlay");
    div.append(overlay);
    const p = { i: k, page, w: vp.width, h: vp.height, rot: vp.rotation, div, overlay, canvas: null, textLayer: null,
      renderedAt: 0, task: null, near: false, overlayDone: false };
    div._p = p;
    S.pages.push(p);
    pagesEl.append(div);
    nearObs.observe(div);
    farObs.observe(div);
  }
  enableUi(true);
  $("#pageCount").textContent = "/ " + pdf.numPages;
  if (view) {
    S.zoom = view.zoom;
    layout();
    restoreView(view);
  } else {
    fitWidth(1.5);
    viewer.scrollTop = 0;
  }
  onScroll();
  status(pdf.numPages + (pdf.numPages === 1 ? " page" : " pages"));
  return true;
}

// ------------------------------------------------------------------ layout, zoom, scroll
function layout() {
  const s = scale();
  for (const p of S.pages) {
    p.div.style.width = p.w * s + "px";
    p.div.style.height = p.h * s + "px";
    for (const e of p.overlay.children) if (e._r) place(e, e._r, s);
    if (p.textLayer) p.textLayer.style.setProperty("--total-scale-factor", s);
  }
  $("#zoomLabel").textContent = Math.round(S.zoom * 100) + "%";
  scheduleRender();
}

function captureView() {
  const p = S.pages[currentPage()];
  if (!p) return { zoom: S.zoom, page: 0, frac: 0 };
  const y = viewer.scrollTop + viewer.clientHeight / 2 - p.div.offsetTop;
  return { zoom: S.zoom, page: p.i, frac: y / p.div.offsetHeight, x: viewer.scrollLeft / Math.max(1, viewer.scrollWidth) };
}

function restoreView(v) {
  const p = S.pages[Math.min(v.page, S.pages.length - 1)];
  if (!p) return;
  viewer.scrollTop = p.div.offsetTop + v.frac * p.div.offsetHeight - viewer.clientHeight / 2;
  if (v.x !== undefined) viewer.scrollLeft = v.x * viewer.scrollWidth;
}

function setZoom(z) {
  z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
  if (!S.pages.length || Math.abs(z - S.zoom) < 1e-4) { S.zoom = z; return; }
  const v = captureView();
  S.zoom = z;
  layout();
  restoreView(v);
}

function fitWidth(cap = MAX_ZOOM) {
  if (!S.pages.length) return;
  const maxW = Math.max(...S.pages.map((p) => p.w));
  setZoom(Math.min(cap, (viewer.clientWidth - 40) / (maxW * PX_PER_PT)));
  if (S.zoom !== cap) layout();
}

function fitPage() {
  const p = S.pages[currentPage()];
  if (!p) return;
  const z = Math.min((viewer.clientWidth - 40) / (p.w * PX_PER_PT), (viewer.clientHeight - 30) / (p.h * PX_PER_PT));
  setZoom(z);
  goToPage(p.i);
}

function currentPage() {
  const c = viewer.scrollTop + viewer.clientHeight / 2;
  let lo = 0, hi = S.pages.length - 1;
  while (lo < hi) { // first page whose bottom is below the viewport centre
    const mid = (lo + hi) >> 1;
    const d = S.pages[mid].div;
    if (d.offsetTop + d.offsetHeight + 6 < c) lo = mid + 1; else hi = mid;
  }
  return lo;
}

function goToPage(i) {
  const p = S.pages[Math.max(0, Math.min(S.pages.length - 1, i))];
  if (p) viewer.scrollTop = p.div.offsetTop - 8;
}

function onScroll() {
  if (!S.pages.length) return;
  const i = currentPage();
  const inp = $("#pageNum");
  if (document.activeElement !== inp) inp.value = i + 1;
}

// ------------------------------------------------------------------ rendering
const nearObs = new IntersectionObserver((entries) => {
  for (const e of entries) {
    const p = e.target._p;
    p.near = e.isIntersecting;
    if (p.near) { renderPage(p); setupOverlay(p); }
  }
}, { root: viewer, rootMargin: "100% 0px" });

const farObs = new IntersectionObserver((entries) => {
  for (const e of entries) if (!e.isIntersecting) releasePage(e.target._p);
}, { root: viewer, rootMargin: "500% 0px" });

let renderTimer = 0;
function scheduleRender() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(() => { for (const p of S.pages) if (p.near) renderPage(p); }, 90);
}

async function renderPage(p) {
  let target = scale() * (window.devicePixelRatio || 1);
  if (p.w * p.h * target * target > MAX_CANVAS_PX) target = Math.sqrt(MAX_CANVAS_PX / (p.w * p.h));
  if (Math.abs(p.renderedAt - target) < 1e-3 || p.pending === target) return;
  if (p.task) p.task.cancel();
  p.pending = target;
  const vp = p.page.getViewport({ scale: target });
  const canvas = el("canvas", "render");
  canvas.width = Math.max(1, Math.floor(vp.width));
  canvas.height = Math.max(1, Math.floor(vp.height));
  const task = p.page.render({ canvas, viewport: vp, annotationMode: pdfjs.AnnotationMode.ENABLE });
  p.task = task;
  try {
    await task.promise;
  } catch (e) {
    if (e?.name !== "RenderingCancelledException") console.error(e);
    if (p.task === task) { p.task = null; p.pending = 0; }
    return;
  }
  if (p.task !== task) return;
  p.task = null;
  p.pending = 0;
  if (p.canvas) p.canvas.replaceWith(canvas); else p.div.prepend(canvas);
  p.canvas = canvas;
  p.renderedAt = target;
  if (!p.textLayer) buildTextLayer(p);
}

function releasePage(p) {
  if (p.task) { p.task.cancel(); p.task = null; p.pending = 0; }
  if (p.canvas) { p.canvas.width = 0; p.canvas.remove(); p.canvas = null; }
  if (p.textLayer) { p.textLayer.remove(); p.textLayer = null; }
  p.renderedAt = 0;
}

async function buildTextLayer(p) {
  // transparent, selectable text over the page so text can be copied. PDF.js positions the
  // spans in percentages of the layer, so on zoom only the font scale variable changes (layout()).
  const s0 = scale();
  const div = el("div", "textLayer");
  div.style.setProperty("--total-scale-factor", s0);
  p.textLayer = div;
  p.overlay.before(div);
  try {
    const tl = new pdfjs.TextLayer({ textContentSource: p.page.streamTextContent(), container: div,
      viewport: p.page.getViewport({ scale: s0 }) });
    await tl.render();
  } catch (e) {
    console.warn("text layer", e);
  }
  if (p.textLayer === div) div.style.setProperty("--total-scale-factor", scale());
}

// ------------------------------------------------------------------ form fields
async function setupOverlay(p) {
  if (p.overlayDone) return;
  p.overlayDone = true;
  let annots;
  try { annots = await p.page.getAnnotations({ intent: "display" }); } catch { return; }
  const s = scale();
  for (const a of annots) {
    if (a.annotationType !== 20 || a.hidden || !a.fieldName) continue; // 20 = widget
    const r = displayRect(p, a.rect);
    if (r[2] < 2 || r[3] < 2) continue;
    let e = null;
    if (a.fieldType === "Tx") e = makeTextField(a, r);
    else if (a.fieldType === "Btn" && (a.checkBox || a.radioButton)) e = makeCheck(a, r);
    else if (a.fieldType === "Ch") e = makeChoice(a, r);
    else if (a.fieldType === "Sig" && !a.fieldValue) e = makeSignField(p, a, r);
    if (!e) continue;
    place(e, r, s);
    p.overlay.append(e);
  }
}

function fieldModel(name, kind, initial) {
  let f = S.fields.get(name);
  if (!f) { f = { kind, value: initial, dirty: false, widgets: [] }; S.fields.set(name, f); }
  return f;
}

function setField(name, value, source) {
  const f = S.fields.get(name);
  f.value = value;
  f.dirty = true;
  setDirty(true);
  for (const w of f.widgets) if (w !== source) w.sync(value);
}

function autoFontSize(a, h, multiline) {
  const fs = a.defaultAppearanceData?.fontSize;
  if (fs && fs > 0) return fs;
  return multiline ? 10 : Math.max(6, Math.min(h * 0.7, 12));
}

function makeTextField(a, r) {
  const multi = !!a.multiLine;
  const e = el(multi ? "textarea" : "input", "field");
  if (!multi) e.type = a.password ? "password" : "text";
  const f = fieldModel(a.fieldName, "text", a.fieldValue ?? "");
  e.value = f.value ?? "";
  e._fs = autoFontSize(a, r[3], multi);
  if (a.readOnly) e.readOnly = true;
  if (a.maxLen) e.maxLength = a.maxLen;
  e.spellcheck = false;
  e.title = a.alternativeText || a.fieldName;
  e.sync = (v) => { e.value = v ?? ""; };
  f.widgets.push(e);
  e.addEventListener("input", () => setField(a.fieldName, e.value, e));
  return e;
}

const CHECK_SVG = '<svg class="mark" viewBox="0 0 10 10"><path d="M1.5 5.3 4 7.8 8.6 2.2" fill="none" stroke="#000" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function makeCheck(a, r) {
  const radio = !!a.radioButton;
  const e = el("div", "check" + (radio ? " radio" : "") + (a.readOnly ? " ro" : ""), { tabindex: "0", role: radio ? "radio" : "checkbox" });
  e.innerHTML = '<div class="box">' + (radio ? '<div class="mark"></div>' : CHECK_SVG) + "</div>";
  e.title = a.alternativeText || a.fieldName;
  const onValue = radio ? a.buttonValue : a.exportValue;
  const initial = radio ? (a.fieldValue && a.fieldValue === a.buttonValue ? a.buttonValue : null)
    : (a.fieldValue && a.fieldValue !== "Off" ? true : false);
  const f = fieldModel(a.fieldName, radio ? "radio" : "check", initial);
  if (radio && f.value === null && initial) f.value = initial; // another widget registered first
  e.onValue = onValue;
  e.sync = (v) => e.classList.toggle("on", radio ? v === onValue : !!v);
  e.sync(f.value);
  f.widgets.push(e);
  const toggle = () => {
    if (a.readOnly) return;
    if (radio) { if (f.value !== onValue) { setField(a.fieldName, onValue, e); e.sync(onValue); } }
    else { const v = !e.classList.contains("on"); setField(a.fieldName, v, e); e.sync(v); }
  };
  e.addEventListener("click", toggle);
  e.addEventListener("keydown", (ev) => { if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); toggle(); } });
  return e;
}

function makeChoice(a, r) {
  const opts = (a.options || []).map((o) => ({ value: String(o.exportValue ?? o.displayValue), label: String(o.displayValue ?? o.exportValue) }));
  let v = Array.isArray(a.fieldValue) ? a.fieldValue[0] : a.fieldValue;
  const f = fieldModel(a.fieldName, "choice", v ?? "");
  let e;
  if (a.combo && a.customText !== undefined && a.editable) {
    e = el("input", "field");
    const dl = el("datalist", "", { id: "dl" + nextId++ });
    for (const o of opts) dl.append(Object.assign(el("option"), { value: o.label }));
    e.setAttribute("list", dl.id);
    e.after(dl);
    document.body.append(dl);
    e.value = f.value ?? "";
    e.addEventListener("input", () => setField(a.fieldName, e.value, e));
  } else {
    e = el("select", "field");
    if (!opts.some((o) => o.value === f.value)) e.append(Object.assign(el("option"), { value: "", textContent: "" }));
    for (const o of opts) e.append(Object.assign(el("option"), { value: o.value, textContent: o.label }));
    e.value = f.value ?? "";
    e.addEventListener("change", () => setField(a.fieldName, e.value, e));
  }
  e._fs = autoFontSize(a, r[3], false);
  e.title = a.alternativeText || a.fieldName;
  if (a.readOnly) e.disabled = true;
  e.sync = (val) => { e.value = val ?? ""; };
  f.widgets.push(e);
  return e;
}

function makeSignField(p, a, r) {
  const e = el("div", "signfield", { title: "Click to place a signature here" });
  e.textContent = "Click to sign";
  e._fs = Math.max(4, Math.min(r[3] * 0.22, 7));
  e.addEventListener("click", (ev) => openSignMenu(ev.clientX, ev.clientY, { page: p.i, rect: e._r }));
  return e;
}

// ------------------------------------------------------------------ placed signatures and text
function setPlacing(pl) {
  S.placing = pl;
  viewer.classList.toggle("placing", !!pl);
  $("#textBtn").classList.toggle("on", pl?.kind === "text");
  if (pl) status(pl.kind === "text" ? "Click where the text should start (Esc to cancel)" : "Click where the signature should go (Esc to cancel)");
  else status("");
}

function select(item) {
  if (S.selected && S.selected !== item) S.selected.el?.classList.remove("selected");
  S.selected = item;
  item?.el.classList.add("selected");
}

function removeItem(item) {
  item.el.remove();
  S.items = S.items.filter((x) => x !== item);
  if (S.selected === item) S.selected = null;
  setDirty(true);
}

function positionItem(item) {
  const s = scale();
  const e = item.el;
  if (item.kind === "sig") { place(e, [item.x, item.y, item.w, item.h], s); return; }
  e._r = [item.x, item.y, 0, 0];
  e.style.left = item.x * s + "px";
  e.style.top = item.y * s + "px";
  e.querySelector(".txt").style.fontSize = item.size * s + "px";
}

function addSignature(pageIndex, png, at, fit = null) {
  const p = S.pages[pageIndex];
  const img = new Image();
  img.onload = () => {
    const aspect = img.naturalWidth / img.naturalHeight;
    let w, h, x, y;
    if (fit) {
      w = Math.min(fit[2], fit[3] * aspect); h = w / aspect;
      x = fit[0] + (fit[2] - w) / 2; y = fit[1] + (fit[3] - h) / 2;
    } else {
      w = Math.min(150, p.w * 0.6); h = w / aspect;
      x = at[0] - w / 2; y = at[1] - h / 2;
    }
    const item = { id: nextId++, kind: "sig", page: pageIndex, x, y, w, h, png, aspect };
    clampItem(item, p);
    const e = el("div", "item sig", { tabindex: "0" });
    e.append(img, el("div", "handle"), Object.assign(el("button", "del", { title: "Remove" }), { textContent: "×" }));
    item.el = e;
    e._item = item;
    p.overlay.append(e);
    positionItem(item);
    S.items.push(item);
    wireDrag(item, e, e);
    wireResize(item, e.querySelector(".handle"));
    e.querySelector(".del").addEventListener("click", (ev) => { ev.stopPropagation(); removeItem(item); });
    select(item);
    e.focus({ preventScroll: true });
    setDirty(true);
  };
  img.src = png;
}

function addText(pageIndex, at) {
  const p = S.pages[pageIndex];
  const size = Number($("#textSize").value) || 11;
  const item = { id: nextId++, kind: "text", page: pageIndex, x: at[0], y: at[1] - size * 0.6, size, text: "" };
  const e = el("div", "item text");
  const grip = el("div", "grip", { title: "Drag to move" });
  const txt = el("div", "txt");
  txt.contentEditable = "plaintext-only";
  txt.spellcheck = false;
  e.append(grip, txt, Object.assign(el("button", "del", { title: "Remove" }), { textContent: "×" }));
  item.el = e;
  e._item = item;
  p.overlay.append(e);
  positionItem(item);
  S.items.push(item);
  wireDrag(item, grip, e);
  txt.addEventListener("input", () => { item.text = txt.innerText.replace(/\n$/, ""); setDirty(true); });
  txt.addEventListener("focus", () => select(item));
  txt.addEventListener("blur", () => { if (!item.text.trim()) removeItem(item); });
  txt.addEventListener("keydown", (ev) => { if (ev.key === "Escape") txt.blur(); });
  e.querySelector(".del").addEventListener("mousedown", (ev) => { ev.preventDefault(); ev.stopPropagation(); removeItem(item); });
  select(item);
  setTimeout(() => txt.focus(), 0);
  setDirty(true);
}

function clampItem(item, p) {
  const w = item.w || 10, h = item.h || 10;
  item.x = Math.max(-w * 0.5, Math.min(p.w - w * 0.5, item.x));
  item.y = Math.max(-h * 0.5, Math.min(p.h - h * 0.5, item.y));
}

function wireDrag(item, handleEl, rootEl) {
  handleEl.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || ev.target.classList.contains("handle") || ev.target.classList.contains("del")) return;
    ev.preventDefault();
    ev.stopPropagation();
    select(item);
    rootEl.focus?.({ preventScroll: true });
    const s = scale();
    const start = { cx: ev.clientX, cy: ev.clientY, x: item.x, y: item.y };
    handleEl.setPointerCapture(ev.pointerId);
    const move = (m) => {
      item.x = start.x + (m.clientX - start.cx) / s;
      item.y = start.y + (m.clientY - start.cy) / s;
      clampItem(item, S.pages[item.page]);
      positionItem(item);
    };
    const up = () => {
      handleEl.removeEventListener("pointermove", move);
      handleEl.removeEventListener("pointerup", up);
      if (item.x !== start.x || item.y !== start.y) setDirty(true);
    };
    handleEl.addEventListener("pointermove", move);
    handleEl.addEventListener("pointerup", up);
  });
}

function wireResize(item, handle) {
  handle.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const s = scale();
    const start = { cx: ev.clientX, cy: ev.clientY, w: item.w };
    handle.setPointerCapture(ev.pointerId);
    const move = (m) => {
      const dw = Math.max((m.clientX - start.cx) / s, ((m.clientY - start.cy) / s) * item.aspect);
      item.w = Math.max(12, start.w + dw);
      item.h = item.w / item.aspect;
      positionItem(item);
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      setDirty(true);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
}

pagesEl.addEventListener("pointerdown", (ev) => {
  const pageDiv = ev.target.closest(".page");
  if (!pageDiv) return;
  if (S.placing && ev.button === 0) {
    ev.preventDefault();
    const p = pageDiv._p;
    const rect = pageDiv.getBoundingClientRect();
    const s = scale();
    const at = [(ev.clientX - rect.left) / s, (ev.clientY - rect.top) / s];
    const pl = S.placing;
    setPlacing(null);
    if (pl.kind === "sig") addSignature(p.i, pl.png, at);
    else addText(p.i, at);
    return;
  }
  if (!ev.target.closest(".item")) select(null);
});

// ------------------------------------------------------------------ saving
class UserError extends Error {}

async function buildPdf() {
  let doc;
  try {
    doc = await PDFDocument.load(S.bytes, { updateMetadata: false });
  } catch (e) {
    if (/encrypt/i.test(e.message)) throw new UserError("This PDF is encrypted. Folio can view it but can't save changes to it.");
    throw e;
  }
  // 1. form fields (only the ones the user changed)
  const dirty = [...S.fields.entries()].filter(([, f]) => f.dirty);
  if (dirty.length) {
    const form = doc.getForm();
    for (const [name, f] of dirty) {
      let field;
      try { field = form.getField(name); } catch { throw new UserError(`Could not find form field "${name}" when saving.`); }
      const type = field.constructor.name;
      try {
        if (f.kind === "text") {
          field.setText(f.value || undefined);
        } else if (f.kind === "check") {
          f.value ? field.check() : field.uncheck();
        } else if (f.kind === "radio") {
          if (f.value) {
            field.acroField.setValue(PDFName.of(f.value));
            field.markAsDirty?.();
          }
        } else if (f.kind === "choice") {
          const opts = field.getOptions ? field.getOptions() : [];
          if (!f.value) field.clear?.();
          else if (opts.includes(f.value)) field.select(f.value);
          else { field.enableEditing?.(); field.select(f.value); }
        }
      } catch (e) {
        throw new UserError(`Could not write field "${name}" (${type}): ${e.message}`);
      }
    }
  }
  // 2. signatures and 3. free text
  let helv = null;
  for (const item of S.items) {
    const p = S.pages[item.page];
    const page = doc.getPage(item.page);
    const vp = p.page.getViewport({ scale: 1 });
    const rot = degrees(vp.rotation);
    if (item.kind === "sig") {
      const img = await doc.embedPng(item.png);
      const [x, y] = vp.convertToPdfPoint(item.x, item.y + item.h); // displayed bottom-left
      page.drawImage(img, { x, y, width: item.w, height: item.h, rotate: rot });
    } else if (item.text.trim()) {
      helv ??= await doc.embedFont(StandardFonts.Helvetica);
      const lh = item.size * TEXT_LINE_HEIGHT;
      const baseline = (lh - (ARIAL_ASCENT + ARIAL_DESCENT) * item.size) / 2 + ARIAL_ASCENT * item.size;
      const [x, y] = vp.convertToPdfPoint(item.x, item.y + baseline);
      try {
        page.drawText(item.text, { x, y, size: item.size, font: helv, color: rgb(0, 0, 0), lineHeight: lh, rotate: rot });
      } catch (e) {
        throw new UserError(`The text "${item.text.slice(0, 30)}" has characters the built-in font can't write (${e.message}).`);
      }
    }
  }
  return await doc.save();
}

async function hasSignedFields() {
  for (const p of S.pages) {
    const annots = await p.page.getAnnotations();
    if (annots.some((a) => a.fieldType === "Sig" && a.fieldValue)) return true;
  }
  return false;
}

async function confirmSigned() {
  if (S.signedWarned) return true;
  if (!(await hasSignedFields())) return true;
  const ok = confirm("This PDF contains a digital signature. Saving changes here will invalidate it.\n\nContinue?");
  if (ok) S.signedWarned = true;
  return ok;
}

function download(bytes, name) {
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
  const a = el("a", "", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

function suggestedName(suffix) {
  const base = S.name.replace(/\.pdf$/i, "");
  return base + suffix + ".pdf";
}

async function save(saveAs = false) {
  if (!S.pdf) return;
  if (!saveAs && !S.dirty) { status("No changes to save"); return; }
  if (!(await confirmSigned())) return;
  status("Saving…");
  let out;
  try {
    out = await buildPdf();
  } catch (e) {
    status(e instanceof UserError ? e.message : "Save failed: " + e.message, true);
    if (!(e instanceof UserError)) console.error(e);
    return;
  }
  const fsApi = !NO_FS && "showSaveFilePicker" in window;
  let name = S.name, handle = S.handle;
  try {
    if (saveAs || !handle) {
      if (fsApi) {
        handle = await window.showSaveFilePicker({
          suggestedName: saveAs || !S.handle ? suggestedName(saveAs ? "_copy" : "_filled") : S.name,
          types: [{ description: "PDF", accept: { "application/pdf": [".pdf"] } }],
        });
        name = handle.name;
      } else {
        name = saveAs ? suggestedName("_copy") : suggestedName("_filled");
        download(out, name);
        handle = null;
      }
    }
    if (handle) {
      const w = await handle.createWritable();
      await w.write(out);
      await w.close();
    }
  } catch (e) {
    if (e?.name === "AbortError") { status("Save cancelled"); return; }
    status("Save failed: " + (e?.message || e) + ". Your edits are still here; try Save As.", true);
    return;
  }
  await openBytes(out, name, handle, true);
  status(handle ? "Saved " + name : "Downloaded " + name + " to your Downloads folder");
}

async function printDoc() {
  if (!S.pdf) return;
  let out = S.bytes;
  if (S.dirty || S.items.length) {
    try { out = await buildPdf(); } catch (e) { status(e.message, true); return; }
  }
  const url = URL.createObjectURL(new Blob([out], { type: "application/pdf" }));
  const w = window.open(url, "_blank");
  if (!w) status("Pop-up blocked: allow pop-ups for this page to print.", true);
  else status("Opened a print copy in a new tab. Press Ctrl+P there.");
}

// ------------------------------------------------------------------ find
async function buildFindIndex() {
  const index = [];
  for (const p of S.pages) {
    const tc = await p.page.getTextContent();
    index.push(tc.items.filter((it) => it.str));
  }
  return index;
}

function clearFind() {
  for (const h of document.querySelectorAll(".hl")) h.remove();
  S.find.hits = [];
  S.find.pos = -1;
  $("#findCount").textContent = "";
}

async function runFind(query) {
  clearFind();
  S.find.query = query;
  if (!query || !S.pdf) return;
  S.find.index ??= await buildFindIndex();
  const q = query.toLowerCase();
  for (const [pi, items] of S.find.index.entries()) {
    const p = S.pages[pi];
    for (const it of items) {
      const str = it.str.toLowerCase();
      let k = str.indexOf(q);
      while (k >= 0) {
        const [, , , , e, f] = it.transform;
        const h = it.height || Math.hypot(it.transform[2], it.transform[3]);
        const x0 = e + (it.width * k) / str.length;
        const x1 = e + (it.width * (k + q.length)) / str.length;
        const r = displayRect(p, [x0, f - h * 0.22, x1, f + h * 0.9]);
        const hl = el("div", "hl");
        place(hl, r);
        p.overlay.prepend(hl);
        S.find.hits.push({ p, hl });
        k = str.indexOf(q, k + q.length);
      }
    }
  }
  $("#findCount").textContent = S.find.hits.length ? "" : "No matches";
}

async function findNext(back = false) {
  const q = $("#findInput").value;
  if (q !== S.find.query || !S.find.hits.length) await runFind(q);
  const hits = S.find.hits;
  if (!hits.length) return;
  hits[S.find.pos]?.hl.classList.remove("current");
  S.find.pos = (S.find.pos + (back ? -1 : 1) + hits.length) % hits.length;
  const { p, hl } = hits[S.find.pos];
  hl.classList.add("current");
  const s = scale();
  viewer.scrollTop = p.div.offsetTop + hl._r[1] * s - viewer.clientHeight / 2;
  viewer.scrollLeft = p.div.offsetLeft + hl._r[0] * s - viewer.clientWidth / 2;
  $("#findCount").textContent = `${S.find.pos + 1} of ${hits.length}`;
}

function showFind() {
  if (!S.pdf) return;
  $("#findbar").classList.add("show");
  $("#findInput").focus();
  $("#findInput").select();
}

function hideFind() {
  $("#findbar").classList.remove("show");
  clearFind();
  S.find.query = "";
}

// ------------------------------------------------------------------ signatures: saved list + menu
const sigStore = {
  list: () => store.get("signatures", []),
  add(png) { const l = sigStore.list(); l.push(png); return store.set("signatures", l); },
  remove(i) { const l = sigStore.list(); l.splice(i, 1); store.set("signatures", l); },
};

function closeMenus() { for (const m of document.querySelectorAll(".menu")) m.remove(); }

function openSignMenu(x, y, fit = null) {
  closeMenus();
  if (!S.pdf) return;
  const m = el("div", "menu");
  const sigs = sigStore.list();
  sigs.forEach((png, i) => {
    const b = el("button", "sigopt", { title: "Place this signature" });
    const img = el("img");
    img.src = png;
    const x = el("span", "x", { title: "Delete saved signature" });
    x.textContent = "✕";
    b.append(img, x);
    b.addEventListener("click", (ev) => {
      if (ev.target === x) {
        if (confirm("Delete this saved signature?")) sigStore.remove(i);
        closeMenus();
        return;
      }
      closeMenus();
      useSignature(png, fit);
    });
    m.append(b);
  });
  if (sigs.length) m.append(el("hr"));
  const nb = el("button");
  nb.textContent = "New signature…";
  nb.addEventListener("click", () => { closeMenus(); newSignature(fit); });
  m.append(nb);
  document.body.append(m);
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(x, innerWidth - r.width - 8) + "px";
  m.style.top = Math.min(y, innerHeight - r.height - 8) + "px";
  setTimeout(() => document.addEventListener("pointerdown", function off(ev) {
    if (!m.contains(ev.target)) { closeMenus(); document.removeEventListener("pointerdown", off); }
  }), 0);
}

function useSignature(png, fit) {
  if (fit) addSignature(fit.page, png, null, fit.rect);
  else setPlacing({ kind: "sig", png });
}

// ------------------------------------------------------------------ signature dialog
const dlg = $("#sigDialog");
const pad = $("#drawPad");
const drawing = { strokes: [], width: 3 };
let dlgFit = null;
let loadedImage = null;

function inkColor() { return INK[document.querySelector("input[name=ink]:checked")?.value || "black"]; }

function resizePad() {
  const dpr = window.devicePixelRatio || 1;
  const r = pad.getBoundingClientRect();
  pad.width = Math.round(r.width * dpr);
  pad.height = Math.round(r.height * dpr);
  redrawPad();
}

function strokePath(ctx, pts) {
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  if (pts.length < 3) { for (const p of pts.slice(1)) ctx.lineTo(p[0], p[1]); if (pts.length === 1) ctx.lineTo(pts[0][0] + 0.01, pts[0][1]); }
  else {
    for (let i = 1; i < pts.length - 1; i++) {
      const mx = (pts[i][0] + pts[i + 1][0]) / 2, my = (pts[i][1] + pts[i + 1][1]) / 2;
      ctx.quadraticCurveTo(pts[i][0], pts[i][1], mx, my);
    }
    const last = pts[pts.length - 1];
    ctx.lineTo(last[0], last[1]);
  }
  ctx.stroke();
}

function redrawPad() {
  const ctx = pad.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, pad.width, pad.height);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const h = pad.height / dpr, w = pad.width / dpr;
  ctx.strokeStyle = "#ccc"; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(18, h * 0.72); ctx.lineTo(w - 18, h * 0.72); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#aaa"; ctx.font = "12px Segoe UI, sans-serif"; ctx.fillText("Sign above the line", 18, h * 0.72 + 16);
  ctx.strokeStyle = inkColor(); ctx.lineWidth = drawing.width; ctx.lineCap = "round"; ctx.lineJoin = "round";
  for (const s of drawing.strokes) strokePath(ctx, s);
}

pad.addEventListener("pointerdown", (ev) => {
  pad.setPointerCapture(ev.pointerId);
  const r = pad.getBoundingClientRect();
  drawing.strokes.push([[ev.clientX - r.left, ev.clientY - r.top]]);
  redrawPad();
});
pad.addEventListener("pointermove", (ev) => {
  if (!(ev.buttons & 1) || !drawing.strokes.length) return;
  const r = pad.getBoundingClientRect();
  drawing.strokes[drawing.strokes.length - 1].push([ev.clientX - r.left, ev.clientY - r.top]);
  redrawPad();
});

function drawnPng() {
  const pts = drawing.strokes.flat();
  if (!pts.length) return null;
  const k = 3, padPx = drawing.width * 2;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs) - padPx, y0 = Math.min(...ys) - padPx;
  const w = Math.max(...xs) - x0 + padPx, h = Math.max(...ys) - y0 + padPx;
  const c = el("canvas");
  c.width = Math.ceil(w * k); c.height = Math.ceil(h * k);
  const ctx = c.getContext("2d");
  ctx.setTransform(k, 0, 0, k, -x0 * k, -y0 * k);
  ctx.strokeStyle = inkColor(); ctx.lineWidth = drawing.width; ctx.lineCap = "round"; ctx.lineJoin = "round";
  for (const s of drawing.strokes) strokePath(ctx, s);
  return c.toDataURL("image/png");
}

function trimCanvas(c, padPx = 6) {
  const ctx = c.getContext("2d");
  const { data, width, height } = ctx.getImageData(0, 0, c.width, c.height);
  let x0 = width, y0 = height, x1 = -1, y1 = -1;
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    if (data[(y * width + x) * 4 + 3]) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
  }
  if (x1 < 0) return null;
  x0 = Math.max(0, x0 - padPx); y0 = Math.max(0, y0 - padPx);
  x1 = Math.min(width - 1, x1 + padPx); y1 = Math.min(height - 1, y1 + padPx);
  const out = el("canvas");
  out.width = x1 - x0 + 1; out.height = y1 - y0 + 1;
  out.getContext("2d").drawImage(c, -x0, -y0);
  return out;
}

function typedPng() {
  const text = $("#typeName").value.trim();
  if (!text) return null;
  const font = `110px "${$("#typeFont").value}", cursive`;
  const c = el("canvas");
  const ctx = c.getContext("2d");
  ctx.font = font;
  c.width = Math.ceil(ctx.measureText(text).width + 80);
  c.height = 200;
  ctx.font = font;
  ctx.fillStyle = inkColor();
  ctx.textBaseline = "middle";
  ctx.fillText(text, 40, 100);
  return trimCanvas(c)?.toDataURL("image/png") ?? null;
}

function imagePng() {
  if (!loadedImage) return null;
  const maxW = 1600;
  const k = Math.min(1, maxW / loadedImage.naturalWidth);
  const c = el("canvas");
  c.width = Math.round(loadedImage.naturalWidth * k);
  c.height = Math.round(loadedImage.naturalHeight * k);
  const ctx = c.getContext("2d");
  ctx.drawImage(loadedImage, 0, 0, c.width, c.height);
  if ($("#whiten").checked) {
    const id = ctx.getImageData(0, 0, c.width, c.height);
    const d = id.data;
    for (let i = 0; i < d.length; i += 4) if (d[i] > 225 && d[i + 1] > 225 && d[i + 2] > 225) d[i + 3] = 0;
    ctx.putImageData(id, 0, 0);
  }
  return trimCanvas(c, 2)?.toDataURL("image/png") ?? null;
}

function fontInstalled(name) {
  const ctx = el("canvas").getContext("2d");
  const t = "AaBbGgQqWwMm1234";
  for (const base of ["monospace", "serif"]) {
    ctx.font = `40px ${base}`;
    const a = ctx.measureText(t).width;
    ctx.font = `40px "${name}", ${base}`;
    if (ctx.measureText(t).width !== a) return true;
  }
  return false;
}

function updateTypePreview() {
  const png = typedPng();
  const pv = $("#typePreview");
  pv.textContent = "";
  if (png) { const i = el("img"); i.src = png; pv.append(i); }
}

function updateImagePreview() {
  const png = imagePng();
  const pv = $("#imagePreview");
  pv.textContent = "";
  if (png) { const i = el("img"); i.src = png; pv.append(i); }
  else pv.textContent = loadedImage ? "Image is empty after removing the background" : "No image chosen";
}

function setTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("on", b.dataset.tab === name);
  for (const p of document.querySelectorAll(".tabpane")) p.classList.toggle("on", p.id === "tab-" + name);
  if (name === "draw") resizePad();
}

function newSignature(fit = null) {
  dlgFit = fit;
  const fonts = SCRIPT_FONTS.filter(fontInstalled);
  const sel = $("#typeFont");
  if (!sel.options.length) {
    for (const f of (fonts.length ? fonts : ["cursive", "Georgia", "Arial"])) sel.append(Object.assign(el("option"), { value: f, textContent: f }));
  }
  dlg.showModal();
  setTab(document.querySelector(".tabs button.on")?.dataset.tab || "draw");
  $("#rememberNote").textContent = store.set("probe", 1) ? "" : "(this browser can't remember signatures)";
}

for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => setTab(b.dataset.tab));
$("#clearPad").addEventListener("click", () => { drawing.strokes = []; redrawPad(); });
$("#penWidth").addEventListener("input", (e) => { drawing.width = Number(e.target.value); redrawPad(); });
for (const r of document.querySelectorAll("input[name=ink]")) r.addEventListener("change", () => { redrawPad(); updateTypePreview(); });
$("#typeName").addEventListener("input", updateTypePreview);
$("#typeFont").addEventListener("change", updateTypePreview);
$("#imageFile").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const img = new Image();
  img.onload = () => { loadedImage = img; updateImagePreview(); };
  img.onerror = () => { loadedImage = null; $("#imagePreview").textContent = "Could not read that image"; };
  img.src = URL.createObjectURL(f);
});
$("#whiten").addEventListener("change", updateImagePreview);
$("#sigCancel").addEventListener("click", () => dlg.close());
$("#sigPlace").addEventListener("click", () => {
  const tab = document.querySelector(".tabs button.on").dataset.tab;
  const png = tab === "draw" ? drawnPng() : tab === "type" ? typedPng() : imagePng();
  if (!png) { $("#sigError").textContent = tab === "draw" ? "Draw your signature first." : tab === "type" ? "Type your name first." : "Choose an image first."; return; }
  $("#sigError").textContent = "";
  if ($("#remember").checked) sigStore.add(png);
  dlg.close();
  drawing.strokes = [];
  useSignature(png, dlgFit);
});

// ------------------------------------------------------------------ opening files (picker, input, drag-drop)
async function openDialog() {
  if (!NO_FS && "showOpenFilePicker" in window) {
    try {
      const [handle] = await window.showOpenFilePicker({ types: [{ description: "PDF", accept: { "application/pdf": [".pdf"] } }] });
      await openFile(await handle.getFile(), handle);
    } catch (e) {
      if (e?.name !== "AbortError") status("Could not open: " + e.message, true);
    }
  } else {
    $("#fileInput").click();
  }
}
$("#fileInput").addEventListener("change", (e) => { const f = e.target.files[0]; if (f) openFile(f); e.target.value = ""; });

let dragDepth = 0;
window.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { dragDepth++; document.body.classList.add("dragging"); e.preventDefault(); } });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", async (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  const item = [...(e.dataTransfer?.items || [])].find((i) => i.kind === "file");
  if (!item) return;
  let handle = null;
  if (!NO_FS && item.getAsFileSystemHandle) { try { handle = await item.getAsFileSystemHandle(); } catch { handle = null; } }
  const file = handle ? await handle.getFile() : item.getAsFile();
  if (!file || !/\.pdf$/i.test(file.name)) { status("That isn't a PDF file", true); return; }
  openFile(file, handle?.kind === "file" ? handle : null);
});

// ------------------------------------------------------------------ toolbar, keys, wheel
function enableUi(on) {
  for (const b of document.querySelectorAll("[data-needs-doc]")) b.disabled = !on;
}

$("#openBtn").addEventListener("click", openDialog);
$("#welcomeOpen").addEventListener("click", openDialog);
$("#saveBtn").addEventListener("click", () => save(false));
$("#saveAsBtn").addEventListener("click", () => save(true));
$("#printBtn").addEventListener("click", printDoc);
$("#zoomIn").addEventListener("click", () => setZoom(S.zoom * 1.2));
$("#zoomOut").addEventListener("click", () => setZoom(S.zoom / 1.2));
$("#fitWidth").addEventListener("click", () => fitWidth());
$("#fitPage").addEventListener("click", fitPage);
$("#signBtn").addEventListener("click", (e) => { const r = e.currentTarget.getBoundingClientRect(); openSignMenu(r.left, r.bottom + 2); });
$("#textBtn").addEventListener("click", () => setPlacing(S.placing?.kind === "text" ? null : { kind: "text" }));
$("#findBtn").addEventListener("click", showFind);
$("#findNext").addEventListener("click", () => findNext(false));
$("#findPrev").addEventListener("click", () => findNext(true));
$("#findClose").addEventListener("click", hideFind);
$("#findInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); findNext(e.shiftKey); }
  if (e.key === "Escape") hideFind();
});
$("#pageNum").addEventListener("change", (e) => goToPage(Number(e.target.value) - 1));
$("#textSize").value = store.get("textSize", 11);
$("#textSize").addEventListener("change", (e) => store.set("textSize", Number(e.target.value)));
viewer.addEventListener("scroll", onScroll, { passive: true });
window.addEventListener("resize", () => { scheduleRender(); });

viewer.addEventListener("wheel", (e) => {
  if (!e.ctrlKey || !S.pdf) return;
  e.preventDefault();
  setZoom(S.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12));
}, { passive: false });

window.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  const k = e.key.toLowerCase();
  if (mod && k === "o") { e.preventDefault(); openDialog(); return; }
  if (mod && k === "s") { e.preventDefault(); save(e.shiftKey); return; }
  if (mod && k === "p") { e.preventDefault(); printDoc(); return; }
  if (mod && k === "f") { e.preventDefault(); showFind(); return; }
  if (mod && (k === "=" || k === "+")) { e.preventDefault(); setZoom(S.zoom * 1.2); return; }
  if (mod && k === "-") { e.preventDefault(); setZoom(S.zoom / 1.2); return; }
  if (mod && k === "0") { e.preventDefault(); fitWidth(); return; }
  if (mod && k === "t") { e.preventDefault(); if (S.pdf) setPlacing({ kind: "text" }); return; }
  if (e.key === "Escape") { if (S.placing) setPlacing(null); closeMenus(); select(null); return; }
  const editing = e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if ((e.key === "Delete" || e.key === "Backspace") && S.selected && !editing) { e.preventDefault(); removeItem(S.selected); }
});

window.addEventListener("beforeunload", (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });

enableUi(false);
setDirty(false);

// test hook (used by the automated tests; harmless otherwise)
window.__folio = { S, openBytes, buildPdf, setZoom, goToPage, addSignature, addText, useSignature, scale };
