"""End-to-end test of dist/FolioPDF.html in headless Chromium (same engine as Edge).

Drives the real UI with mouse and keyboard, saves, then verifies the saved PDF
independently with PyMuPDF. Run:
  PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/test_web.py OUTDIR
"""
import base64
import os
import pathlib
import sys

import pymupdf
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "folio-pdf"))
from tests.make_fixture import build  # noqa: E402  (fixture from the desktop project)

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
form = out / "form.pdf"
build(str(form))
url = (HERE.parent / "dist" / "FolioPDF.html").resolve().as_uri()
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(url + "?nofs")

    # ---- open
    page.set_input_files("#fileInput", str(form))
    page.wait_for_function("window.__folio.S.pages.length === 3")
    page.wait_for_selector(".page canvas.render")
    page.wait_for_selector(".overlay .field")
    check(page.evaluate("[...document.querySelectorAll('.page')].length") == 3, "3 pages shown")
    dims = page.evaluate("window.__folio.S.pages.map(p => [Math.round(p.w), Math.round(p.h)])")
    check(dims[1] == [792, 612], f"rotated page laid out landscape {dims[1]}")
    check(page.evaluate("document.querySelector('select.field').value") == "Detroit", "combo initial value read")
    radios = page.locator(".check.radio")
    check(radios.count() == 2, "2 radio widgets")
    check(page.evaluate("[...document.querySelectorAll('.check.radio')].map(e => e.classList.contains('on'))") == [True, False],
          "radio initial state (low selected)")
    check(page.evaluate("window.__folio.S.dirty") is False, "clean after open")

    # ---- fill fields with real input
    page.locator("input.field").first.click()
    page.keyboard.type("Nate Gozdor")
    page.locator("textarea.field").click()
    page.keyboard.type("Line one")
    page.keyboard.press("Enter")
    page.keyboard.type("Line two")
    page.locator(".check:not(.radio)").click()
    radios.nth(1).click()
    check(page.evaluate("[...document.querySelectorAll('.check.radio')].map(e => e.classList.contains('on'))") == [False, True],
          "radio click switches selection")
    page.select_option("select.field", "Chicago")
    check(page.evaluate("window.__folio.S.dirty") is True, "dirty after edits")
    check("Unsaved" in page.inner_text("#saveState"), "unsaved indicator shown")

    # ---- signature into the signature field: click field, New signature, draw, Place
    page.locator(".signfield").click()
    page.get_by_role("button", name="New signature…").click()
    pad = page.locator("#drawPad").bounding_box()
    page.mouse.move(pad["x"] + 40, pad["y"] + 110)
    page.mouse.down()
    for i in range(40):
        page.mouse.move(pad["x"] + 40 + i * 10, pad["y"] + 110 - 25 * ((i % 6) / 6))
    page.mouse.up()
    page.click("#sigPlace")
    page.wait_for_selector(".item.sig img")
    check(page.locator(".item.sig").count() == 1, "drawn signature placed")

    # ---- typed signature, placed by clicking on page 3
    page.evaluate("window.__folio.goToPage(2)")
    page.wait_for_timeout(300)
    page.click("#signBtn")
    check(page.locator(".menu .sigopt").count() == 1, "drawn signature was saved for reuse")
    page.get_by_role("button", name="New signature…").click()
    page.click("[data-tab=type]")
    page.fill("#typeName", "N. Gozdor")
    page.check("input[name=ink][value=blue]")
    page.click("#sigPlace")
    p3 = page.locator(".page").nth(2).bounding_box()
    s = page.evaluate("window.__folio.scale()")
    page.mouse.click(p3["x"] + 400 * s, p3["y"] + 300 * s)
    page.wait_for_function("document.querySelectorAll('.item.sig').length === 2")
    check(True, "typed signature placed by click")

    # ---- free text on page 3
    page.click("#textBtn")
    page.mouse.click(p3["x"] + 72 * s, p3["y"] + 200 * s)
    page.wait_for_selector(".item.text .txt:focus")
    page.keyboard.type("Verified 10/20/2026")
    page.mouse.click(p3["x"] + 300 * s, p3["y"] + 500 * s)  # click away: deselect, keep text
    check(page.locator(".item.text").count() == 1, "free text kept after clicking away")

    # ---- rotated page field
    page.evaluate("window.__folio.goToPage(1)")
    page.wait_for_selector(".page:nth-child(2) .overlay .field")
    page.locator(".page").nth(1).locator("input.field").click()
    page.keyboard.type("Sideways OK")

    # ---- find
    page.keyboard.press("Control+f")
    page.fill("#findInput", "thermometer")
    page.keyboard.press("Enter")
    page.wait_for_function("window.__folio.S.find.hits.length > 0")
    check(page.evaluate("window.__folio.S.find.hits.length") == 2, "find: 2 hits")
    check(page.inner_text("#findCount") == "1 of 2", "find counter")
    page.keyboard.press("Escape")

    # ---- text layer exists for copy/paste
    page.evaluate("window.__folio.goToPage(2)")
    page.wait_for_timeout(400)
    check(page.evaluate("document.querySelectorAll('.page:nth-child(3) .textLayer span').length") > 0, "selectable text layer built")

    # ---- zoom keeps overlays aligned
    def field_box():
        return page.evaluate("""(() => { const f = document.querySelector('input.field'); const p = f.closest('.page');
            return [f.offsetLeft / p.offsetWidth, f.offsetTop / p.offsetHeight, f.offsetWidth / p.offsetWidth]; })()""")
    before = field_box()
    page.evaluate("window.__folio.setZoom(window.__folio.S.zoom * 1.7)")
    page.wait_for_timeout(300)
    after = field_box()
    check(all(abs(a - b) < 0.002 for a, b in zip(before, after)), f"field stays aligned after zoom {before} vs {after}")
    # text layer spans must sit on the rendered text at any zoom: compare against PDF.js's own
    # text position (page 3 line 1 is at x=72pt, baseline y=100pt)
    def span_box():
        return page.evaluate("""(() => { const p = document.querySelector('.page:nth-child(3)');
            const sp = [...p.querySelectorAll('.textLayer span')].find(s => s.textContent.startsWith('Searchable'));
            const r = sp.getBoundingClientRect(), pr = p.getBoundingClientRect(); const s = window.__folio.scale();
            return [(r.left - pr.left) / s, (r.bottom - pr.top) / s, r.width / s]; })()""")
    page.evaluate("window.__folio.goToPage(2)")
    page.wait_for_timeout(300)
    b1 = span_box()
    page.evaluate("window.__folio.setZoom(window.__folio.S.zoom / 1.7)")
    page.wait_for_timeout(300)
    b2 = span_box()
    check(abs(b1[0] - 72) < 1.5 and abs(b2[0] - 72) < 1.5 and abs(b1[1] - b2[1]) < 1.5 and abs(b1[2] - b2[2]) < 3,
          f"text layer stays on the text across zoom {b1} vs {b2}")

    # ---- screenshot of page 1
    page.evaluate("window.__folio.setZoom(1.0)")
    page.evaluate("window.__folio.goToPage(0)")
    page.wait_for_timeout(500)
    page.screenshot(path=str(out / "web_page1.png"))

    # ---- save (download path)
    with page.expect_download() as dl:
        page.keyboard.press("Control+s")
    saved = out / "saved.pdf"
    dl.value.save_as(str(saved))
    check(dl.value.suggested_filename == "form_filled.pdf", f"download name {dl.value.suggested_filename}")
    try:
        page.wait_for_function("window.__folio.S.dirty === false", timeout=15000)
    except Exception:
        print("status after save:", page.inner_text("#statusMsg"))
        raise
    check(page.evaluate("window.__folio.S.items.length") == 0, "after save the edits are baked into the reloaded file")

    d = pymupdf.open(str(saved))
    vals = {}
    for pg_ in d:
        for w in pg_.widgets():
            vals.setdefault(w.field_name, []).append(w.field_value)
    check(vals["name"] == ["Nate Gozdor"], f"name saved {vals['name']}")
    check(vals["notes"][0].replace("\r", "\n") == "Line one\nLine two", f"multiline saved {vals['notes']!r}")
    check(vals["urgent"] == ["Yes"], f"checkbox saved {vals['urgent']}")
    check(vals["priority"] == ["Off", "high"], f"radio saved {vals['priority']}")
    check(vals["lab"] == ["Chicago"], f"combo saved {vals['lab']}")
    check(vals["rotated_field"] == ["Sideways OK"], f"rotated field saved {vals['rotated_field']}")
    i0 = d[0].get_image_info()
    check(len(i0) == 1 and pymupdf.Rect(160, 350, 360, 400).contains(pymupdf.Rect(i0[0]["bbox"])),
          f"drawn signature inside signature field {i0[0]['bbox'] if i0 else None}")
    i2 = d[2].get_image_info()
    if i2:
        b = pymupdf.Rect(i2[0]["bbox"])
        c = ((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2)
        check(abs(c[0] - 400) < 1.5 and abs(c[1] - 300) < 1.5, f"typed signature centred on click point {c}")
    else:
        check(False, "typed signature present on page 3")
    hits = d[2].search_for("Verified 10/20/2026")
    check(bool(hits), "free text saved as real searchable text")
    if hits:
        check(abs(hits[0].x0 - 72) < 2.5, f"free text x position {hits[0].x0:.1f}")
    d.close()

    # ---- the saved file reopens cleanly in the app, values shown
    page.set_input_files("#fileInput", str(saved))
    page.wait_for_function("window.__folio.S.pages.length === 3 && document.querySelector('input.field')")
    page.wait_for_timeout(300)
    check(page.evaluate("document.querySelector('input.field').value") == "Nate Gozdor", "reopened file shows saved value")

    # ---- rotation correctness for placed items on 0/90/180/270 pages
    rot = pymupdf.open()
    for r in (0, 90, 180, 270):
        p = rot.new_page(width=400, height=600)
        p.set_rotation(r)
    rot_path = out / "rot.pdf"
    rot.save(str(rot_path))
    page.set_input_files("#fileInput", str(rot_path))
    page.wait_for_function("window.__folio.S.pages.length === 4")
    # test image: red left half, blue right half, green strip on top
    from PIL import Image
    im = Image.new("RGBA", (200, 100), (255, 0, 0, 255))
    for x in range(100, 200):
        for y in range(100):
            im.putpixel((x, y), (0, 0, 255, 255))
    for x in range(200):
        for y in range(20):
            im.putpixel((x, y), (0, 200, 0, 255))
    import io
    buf = io.BytesIO()
    im.save(buf, "PNG")
    png = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    for i in range(4):
        page.evaluate("([i, png]) => window.__folio.addSignature(i, png, null, [50, 60, 100, 50])", [i, png])
        page.evaluate("(i) => window.__folio.addText(i, [200, 300 + 11 * 0.6])", i)
        page.wait_for_timeout(50)
        # give each text box content before the next one takes focus (empty boxes are discarded on blur)
        page.evaluate("window.__folio.S.items.filter(x => x.kind === 'text').forEach(t => { t.text = 'Hi'; t.size = 20; t.y = 300; })")
    page.wait_for_function("window.__folio.S.items.filter(x => x.kind === 'sig').length === 4")
    check(page.evaluate("window.__folio.S.items.filter(x => x.kind === 'text').length") == 4, "4 test text boxes present")
    out_b64 = page.evaluate("window.__folio.buildPdf().then(b => { let s=''; for (const c of b) s += String.fromCharCode(c); return btoa(s); })")
    rd = pymupdf.open("pdf", base64.b64decode(out_b64))
    for i, r in enumerate((0, 90, 180, 270)):
        pix = rd[i].get_pixmap()
        ok = pix.pixel(60, 90) == (255, 0, 0) and pix.pixel(140, 90) == (0, 0, 255) and pix.pixel(100, 63) == (0, 200, 0)
        check(ok, f"signature upright and in place on {r}° page")
        dk = [(x, y) for x in range(180, 260) for y in range(280, 340) if sum(pix.pixel(x, y)) < 200]
        tb = (min(x for x, _ in dk), min(y for _, y in dk), max(x for x, _ in dk), max(y for _, y in dk)) if dk else None
        check(tb is not None and abs(tb[0] - 200) <= 3 and 300 <= tb[1] <= 310 and tb[2] - tb[0] > tb[3] - tb[1],
              f"text upright and in place on {r}° page (bbox {tb})")

    check(not errors, "no page errors: " + "; ".join(errors[:5]))
    browser.close()

print("\nFAILURES:" if failures else "\nALL PASSED", *failures, sep="\n  ")
sys.exit(1 if failures else 0)
