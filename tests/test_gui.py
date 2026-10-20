"""End-to-end test: drive the real UI offscreen, save, and verify the PDF.

Run:  QT_QPA_PLATFORM=offscreen python tests/test_gui.py OUTDIR
"""
import faulthandler
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from folio.form_widgets import CheckEditor, ComboEditor, MultiLineEditor, TextFieldEditor  # noqa: E402
from folio.main_window import MainWindow  # noqa: E402
from folio.signature import DrawCanvas, qimage_to_png, render_typed, trim_transparent, whiten_to_alpha  # noqa: E402
from tests.make_fixture import build  # noqa: E402

faulthandler.dump_traceback_later(int(os.environ.get("HANG_TIMEOUT", "0")) or 10**6, exit=True)
if os.environ.get("GITHUB_ACTIONS"):
    import traceback

    def _annotate(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb)).replace("\n", "%0A")
        print(f"::error title=Uncaught exception::{text}", flush=True)
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _annotate
out = sys.argv[1]
os.makedirs(out, exist_ok=True)
src = os.path.join(out, "form.pdf")
build(src)
os.environ.setdefault("XDG_DATA_HOME", os.path.join(out, "appdata"))

app = QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Discard)
win = MainWindow()
win.resize(1000, 900)
win.show()
QTest.qWait(50)
assert win.open_path(src)
QTest.qWait(100)
view = win.view
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=Test failed::{msg}")


eds = {ed.meta.field_name + (f":{ed.meta.xref}" if isinstance(ed, CheckEditor) and ed.radio else ""): ed
       for ed in view.editors}
check(len(view.pages) == 3, "3 pages loaded")
check(abs(view.pages[1].w - 792) < 1 and abs(view.pages[1].h - 612) < 1, "rotated page shown landscape")
check(eds["lab"].currentText() == "Detroit", "combo initial value read")
radios = [e for e in view.editors if isinstance(e, CheckEditor) and e.radio]
check([r.checked for r in radios] == [True, False], "radio initial state read (low selected)")
check(not view.modified, "not modified after open")


def type_into(widget, text):
    widget.setFocus()
    QTest.keyClicks(widget, text)


# text field via real key events
type_into(eds["name"], "Nate Gozdor")
check(eds["name"].text() == "Nate Gozdor", "typing into text field")
check(view.modified, "modified after typing")
type_into(eds["notes"], "Line one")
QTest.keyClick(eds["notes"], Qt.Key_Return)
QTest.keyClicks(eds["notes"], "Line two")
# checkbox and radio via mouse clicks
QTest.mouseClick(eds["urgent"], Qt.LeftButton)
check(eds["urgent"].checked, "checkbox click toggles on")
QTest.mouseClick(radios[1], Qt.LeftButton)
check(radios[1].checked and not radios[0].checked, "radio click switches selection")
# combo
eds["lab"].setCurrentIndex(eds["lab"].findText("Chicago"))
eds["lab"].activated.emit(eds["lab"].currentIndex())
# rotated field
type_into(eds["rotated_field"], "Sideways OK")

# signature: draw strokes on the real canvas, export, place in the signature field
canvas = DrawCanvas()
canvas.resize(460, 170)
QTest.mousePress(canvas, Qt.LeftButton, pos=QPoint(40, 120))
for x in range(40, 400, 12):
    QTest.mouseMove(canvas, QPoint(x, 120 - int(30 * ((x % 60) / 60))))
QTest.mouseRelease(canvas, Qt.LeftButton, pos=QPoint(400, 100))
check(not canvas.is_empty(), "drawn signature captured")
sig_png = qimage_to_png(canvas.to_image())
sign_btn = next(it.widget() for it in view.pages[0].childItems()
                if hasattr(it, "widget") and type(it.widget()).__name__ == "SignFieldButton")
got = []
view.signFieldRequested.disconnect(win._sign_field)  # the real handler opens a modal dialog
view.signFieldRequested.connect(lambda pi, r: got.append((pi, r)))
QTest.mouseClick(sign_btn, Qt.LeftButton)
check(got and got[0][0] == 0, "clicking signature field requests a signature")
view.begin_place_image(sig_png, got[0])

# typed signature placed by clicking on page 3
typed = qimage_to_png(render_typed("N. Gozdor", "DejaVu Sans", QColor(16, 52, 166)))
view.begin_place_image(typed)
view.go_to_page(2)
QTest.qWait(50)
target = view.pages[2].mapToScene(QPointF(400, 300))
QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.mapFromScene(target))
check(view.mode == "normal", "placement mode ends after click")

# free text on page 3
view.begin_place_text()
tpt = view.pages[2].mapToScene(QPointF(72, 200))
QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.mapFromScene(tpt))
QTest.keyClicks(view.viewport(), "Verified 10/20/2026")
view.setFocus()
QTest.qWait(20)

# search
win._show_find()
win.find_edit.setText("thermometer")
win._find_next()
check(len(view.search_hits) == 2, "search finds 2 hits")

# screenshot before saving
view.go_to_page(0)
QTest.qWait(100)
win.grab().save(os.path.join(out, "screenshot_page1.png"))

# save as (stub the dialog), then verify the written file independently
dest = os.path.join(out, "form_filled.pdf")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (dest, "PDF files (*.pdf)"))
check(win.save_as(), "save as succeeds")
check(win.path == dest and not view.modified, "reopened saved copy, clean state")

d = pymupdf.open(dest)
vals = {}
for p in d:
    for w in p.widgets():
        vals.setdefault(w.field_name, []).append(w.field_value)
check(vals["name"] == ["Nate Gozdor"], f"name saved: {vals['name']}")
check(vals["notes"][0].replace("\r", "\n") == "Line one\nLine two", f"multiline saved: {vals['notes']!r}")
check(vals["urgent"] == ["Yes"], f"checkbox saved: {vals['urgent']}")
check(vals["priority"] == ["Off", "high"], f"radio saved: {vals['priority']}")
check(vals["lab"] == ["Chicago"], f"combo saved: {vals['lab']}")
check(vals["rotated_field"] == ["Sideways OK"], f"rotated field saved: {vals['rotated_field']}")
imgs0 = d[0].get_image_info()
imgs2 = d[2].get_image_info()
check(len(imgs0) == 1, "signature image on page 1")
if imgs0:
    b = pymupdf.Rect(imgs0[0]["bbox"])
    check(pymupdf.Rect(160, 350, 360, 400).contains(b), f"signature inside field box {b}")
check(len(imgs2) == 1, "typed signature on page 3")
if imgs2:
    b = pymupdf.Rect(imgs2[0]["bbox"])
    c = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
    check(abs(c[0] - 400) < 1 and abs(c[1] - 300) < 1, f"typed signature centered on click {c}")
txt = d[2].get_text()
check("Verified 10/20/2026" in txt, "free text written as real (searchable) text")
tb = d[2].search_for("Verified")
check(tb and abs(tb[0].x0 - 72) < 3, f"free text position {tb[0] if tb else None}")
d.close()

# in-place save path (incremental) on a second copy
copy = os.path.join(out, "inplace.pdf")
shutil.copy(src, copy)
win.open_path(copy)
QTest.qWait(50)
ed = next(e for e in view.editors if e.meta.field_name == "name")
type_into(ed, "In place")
size_before = os.path.getsize(copy)
check(win.save(), "save in place succeeds")
d = pymupdf.open(copy)
check([w.field_value for w in d[0].widgets() if w.field_name == "name"] == ["In place"], "in-place value saved")
check(os.path.getsize(copy) > size_before, "incremental save appended")
untouched = [w.field_value for w in d[0].widgets() if w.field_name == "lab"]
check(untouched == ["Detroit"], "untouched fields unchanged")
d.close()

# failed save must keep on-screen edits and leave a usable document
QMessageBox.critical = staticmethod(lambda *a, **k: None)
ed = next(e for e in view.editors if e.meta.field_name == "notes")
type_into(ed, "unsaved work")
orig_incr = pymupdf.Document.saveIncr
pymupdf.Document.saveIncr = lambda self: (_ for _ in ()).throw(OSError("simulated lock"))
check(not win.save(), "simulated save failure reported")
pymupdf.Document.saveIncr = orig_incr
check(view.modified and ed.toPlainText() == "unsaved work", "edits survive failed save")
check(win.doc is not None and not win.doc.is_closed and view.doc is win.doc, "document reattached after failure")
view._render_visible()
check(win.save(), "retry save succeeds")
d = pymupdf.open(copy)
check([w.field_value for w in d[0].widgets() if w.field_name == "notes"] == ["unsaved work"], "retry wrote value")
d.close()

# signature helpers
img = QImage(50, 20, QImage.Format_ARGB32)
img.fill(QColor(255, 255, 255))
img.setPixelColor(10, 10, QColor(0, 0, 0))
w = whiten_to_alpha(img)
check(w.pixelColor(0, 0).alpha() == 0 and w.pixelColor(10, 10).alpha() == 255, "white -> transparent")
t = trim_transparent(w, pad=0)
check(t.width() == 1 and t.height() == 1, "trim to content")

win.view.set_modified(False)
win.close()
print("\nFAILURES:" if failures else "\nALL PASSED", *failures, sep="\n  ")
sys.exit(1 if failures else 0)
