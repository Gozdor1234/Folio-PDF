from __future__ import annotations

import os

import pymupdf
from PySide6.QtCore import QRectF, QSettings, QSize, Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (QDoubleSpinBox, QFileDialog, QInputDialog, QLabel, QLineEdit, QMainWindow,
                               QMenu, QMessageBox, QSpinBox, QStackedWidget, QToolBar, QToolButton, QWidget,
                               QSizePolicy, QProgressDialog, QApplication)

from . import APP_NAME
from .document_view import PdfView, pixmap_to_qimage
from .saver import SaveError, has_signed_fields, save_as, save_in_place
from .signature import SignatureDialog, SignatureStore


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings()
        self.path: str | None = None
        self.password: str | None = None
        self.doc: pymupdf.Document | None = None
        self.store = SignatureStore()
        self._warned_signed = False

        self.view = PdfView()
        self.welcome = QLabel("Open a PDF  (Ctrl+O)\nor drag one here")
        self.welcome.setAlignment(Qt.AlignCenter)
        self.welcome.setStyleSheet("color: #ddd; font-size: 18px; background: #525659;")
        self.stack = QStackedWidget()
        self.stack.addWidget(self.welcome)
        self.stack.addWidget(self.view)
        self.setCentralWidget(self.stack)
        self.setAcceptDrops(True)

        self._build_actions()
        self._build_toolbar()
        self._build_findbar()
        self.status_page = QLabel()
        self.status_mode = QLabel()
        self.statusBar().addWidget(self.status_mode, 1)
        self.statusBar().addPermanentWidget(self.status_page)

        self.view.pageChanged.connect(self._on_page_changed)
        self.view.zoomChanged.connect(lambda z: self.zoom_label.setText(f"{z * 100:.0f}%"))
        self.view.modifiedChanged.connect(lambda _m: self._update_title())
        self.view.signFieldRequested.connect(self._sign_field)
        self.view.modeChanged.connect(self._on_mode)

        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1100, 850)
        self._update_title()
        self._update_enabled()

    # ------------------------------------------------------------------ UI construction
    def _act(self, text, slot, shortcut=None, tip=None):
        a = QAction(text, self)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcuts(shortcut if isinstance(shortcut, list) else [shortcut])
        if tip:
            a.setToolTip(tip)
        self.addAction(a)
        return a

    def _build_actions(self):
        S = QKeySequence
        self.a_open = self._act("Open", self.open_dialog, S.Open, "Open a PDF (Ctrl+O)")
        self.a_save = self._act("Save", self.save, S.Save, "Save (Ctrl+S)")
        self.a_saveas = self._act("Save As…", self.save_as, S("Ctrl+Shift+S"), "Save a copy (Ctrl+Shift+S)")
        self.a_print = self._act("Print", self.print_doc, S.Print, "Print (Ctrl+P)")
        self.a_zoom_in = self._act("+", lambda: self.view.zoom_by(1.2), [S("Ctrl+="), S("Ctrl++")], "Zoom in")
        self.a_zoom_out = self._act("−", lambda: self.view.zoom_by(1 / 1.2), S("Ctrl+-"), "Zoom out")
        self.a_fit_w = self._act("Fit width", self.view.fit_width, S("Ctrl+0"), "Fit width (Ctrl+0)")
        self.a_fit_p = self._act("Fit page", self.view.fit_page, S("Ctrl+9"), "Fit page (Ctrl+9)")
        self.a_text = self._act("Add text", self.view.begin_place_text, S("Ctrl+T"),
                                "Click on the page to type text (Ctrl+T). For forms without fillable fields.")
        self.a_find = self._act("Find", self._show_find, S.Find)
        self.a_home = self._act("First page", lambda: self.view.go_to_page(0), S("Ctrl+Home"))
        self.a_end = self._act("Last page", lambda: self.view.go_to_page(10 ** 9), S("Ctrl+End"))

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        for a in (self.a_open, self.a_save, self.a_saveas, self.a_print):
            tb.addAction(a)
        tb.addSeparator()

        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(lambda v: self.view.go_to_page(v - 1))
        self.page_total = QLabel(" / 0 ")
        tb.addWidget(QLabel(" Page "))
        tb.addWidget(self.page_spin)
        tb.addWidget(self.page_total)
        tb.addSeparator()

        tb.addAction(self.a_zoom_out)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        tb.addWidget(self.zoom_label)
        tb.addAction(self.a_zoom_in)
        tb.addAction(self.a_fit_w)
        tb.addAction(self.a_fit_p)
        tb.addSeparator()

        self.sign_btn = QToolButton()
        self.sign_btn.setText("Sign ▾")
        self.sign_btn.setToolTip("Place a signature or initials")
        self.sign_btn.setPopupMode(QToolButton.InstantPopup)
        self.sign_menu = QMenu(self)
        self.sign_menu.aboutToShow.connect(self._fill_sign_menu)
        self.sign_btn.setMenu(self.sign_menu)
        tb.addWidget(self.sign_btn)
        tb.addAction(self.a_text)
        self.text_size = QDoubleSpinBox()
        self.text_size.setRange(6, 48)
        self.text_size.setValue(float(self.settings.value("textSize", 11)))
        self.text_size.setSuffix(" pt")
        self.text_size.setToolTip("Font size for new text")
        self.text_size.valueChanged.connect(self._text_size_changed)
        self.view.text_size = self.text_size.value()
        tb.addWidget(self.text_size)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.a_find)

    def _build_findbar(self):
        fb = QToolBar("Find")
        fb.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(fb)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find in document")
        self.find_edit.setMaximumWidth(280)
        self.find_edit.returnPressed.connect(self._find_next)
        self.find_edit.textChanged.connect(lambda _t: setattr(self, "_find_dirty", True))
        self.find_status = QLabel()
        fb.addWidget(self.find_edit)
        fb.addAction("Previous", lambda: self._find_next(backwards=True))
        fb.addAction("Next", self._find_next)
        fb.addWidget(self.find_status)
        close = QAction("✕", self)
        close.triggered.connect(self._hide_find)
        fb.addAction(close)
        esc = QAction(self.find_edit)
        esc.setShortcut(QKeySequence("Escape"))
        esc.setShortcutContext(Qt.WidgetShortcut)
        esc.triggered.connect(self._hide_find)
        self.find_edit.addAction(esc)
        self.findbar = fb
        self._find_dirty = True
        fb.hide()

    # ------------------------------------------------------------------ state helpers
    def _update_title(self):
        name = os.path.basename(self.path) if self.path else ""
        mod = " •" if self.view.modified else ""
        self.setWindowTitle(f"{name}{mod} — {APP_NAME}" if name else APP_NAME)

    def _update_enabled(self):
        has = self.doc is not None
        for a in (self.a_save, self.a_saveas, self.a_print, self.a_zoom_in, self.a_zoom_out, self.a_fit_w,
                  self.a_fit_p, self.a_text, self.a_find, self.a_home, self.a_end):
            a.setEnabled(has)
        self.sign_btn.setEnabled(has)
        self.page_spin.setEnabled(has)
        self.stack.setCurrentWidget(self.view if has else self.welcome)

    def _on_page_changed(self, i, n):
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, n))
        self.page_spin.setValue(i + 1)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f" / {n} ")
        self.status_page.setText(f"Page {i + 1} of {n}")

    def _on_mode(self, mode):
        msg = {"place_image": "Click where the signature should go  (Esc to cancel)",
               "place_text": "Click where the text should start  (Esc to cancel)"}.get(mode, "")
        self.status_mode.setText(msg)

    def _text_size_changed(self, v):
        self.view.text_size = v
        self.settings.setValue("textSize", v)

    # ------------------------------------------------------------------ open
    def open_dialog(self):
        if not self._confirm_discard():
            return
        start = self.settings.value("lastDir", os.path.expanduser("~"))
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", start, "PDF files (*.pdf);;All files (*)")
        if path:
            self.open_path(path)

    def _open_doc(self, path: str, password: str | None) -> pymupdf.Document:
        doc = pymupdf.open(path)
        if doc.needs_pass and not doc.authenticate(password or ""):
            doc.close()
            raise PermissionError("password")
        return doc

    def open_path(self, path: str, keep_view: bool = False) -> bool:
        path = os.path.abspath(path)
        password = self.password if keep_view else None
        try:
            doc = pymupdf.open(path)
        except Exception as e:  # noqa: BLE001 - surface any parse error to the user
            QMessageBox.critical(self, APP_NAME, f"Could not open the file:\n{path}\n\n{e}")
            return False
        if doc.needs_pass:
            while not doc.authenticate(password or ""):
                password, ok = QInputDialog.getText(self, APP_NAME, "This PDF is password protected:",
                                                    QLineEdit.Password)
                if not ok:
                    doc.close()
                    return False
        if self.doc is not None and not self.doc.is_closed:
            self.doc.close()
        self.doc, self.path, self.password = doc, path, password
        self._warned_signed = False
        self.settings.setValue("lastDir", os.path.dirname(path))
        self._update_enabled()
        QApplication.processEvents()  # let the view get its real size before fitting
        self.view.load(doc, keep_view=keep_view)
        self.view.clear_search()
        self._find_dirty = True
        self._update_title()
        return True

    # ------------------------------------------------------------------ save
    def _confirm_signed_doc(self) -> bool:
        if self._warned_signed or not has_signed_fields(self.doc):
            return True
        r = QMessageBox.warning(
            self, APP_NAME,
            "This PDF contains a digital signature.\n\nSaving changes will be recorded after the signature, "
            "and PDF viewers will report that the document was modified after signing.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if r == QMessageBox.Yes:
            self._warned_signed = True
            return True
        return False

    def save(self) -> bool:
        if self.doc is None or self.doc.is_closed:
            return False
        if not self.view.modified:
            return True
        if not self._confirm_signed_doc():
            return False
        edits = self.view.collect_edits()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            save_in_place(self.doc, self.path, edits, self.password,
                          reopen=lambda p: self._reopen_after_save(p))
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            err = e
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            # the display document may have been closed during the attempt: reattach without
            # rebuilding the scene, so the user's unsaved edits stay on screen
            if self.doc is None or self.doc.is_closed:
                try:
                    self.doc = self._open_doc(self.path, self.password)
                    self.view.doc = self.doc
                except Exception:  # noqa: BLE001
                    pass
            QMessageBox.critical(self, APP_NAME, f"Save failed.\n\n{err}")
            return False
        self.statusBar().showMessage("Saved", 3000)
        return True

    def _reopen_after_save(self, path):
        self.doc = None
        self.open_path(path, keep_view=True)
        return self.doc

    def save_as(self) -> bool:
        if self.doc is None or self.doc.is_closed:
            return False
        if not self._confirm_signed_doc():
            return False
        base, _ = os.path.splitext(self.path)
        suggested = base + ("_signed.pdf" if self.view.modified else "_copy.pdf")
        new_path, _ = QFileDialog.getSaveFileName(self, "Save As", suggested, "PDF files (*.pdf)")
        if not new_path:
            return False
        if not new_path.lower().endswith(".pdf"):
            new_path += ".pdf"
        edits = self.view.collect_edits()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            save_as(self.path, new_path, edits, self.password)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, f"Save failed.\n\n{e}")
            return False
        QApplication.restoreOverrideCursor()
        pw = self.password
        self.view.set_modified(False)
        self.password = pw
        self._open_saved_as(new_path, pw)
        return True

    def _open_saved_as(self, path, pw):
        self.password = pw
        if self.doc is not None and not self.doc.is_closed:
            self.doc.close()
        self.doc = None
        self.path = path
        self.open_path(path, keep_view=True)

    def _confirm_discard(self) -> bool:
        if not self.view.modified:
            return True
        r = QMessageBox.question(self, APP_NAME, "Save changes to this document?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    # ------------------------------------------------------------------ signatures
    def _fill_sign_menu(self):
        m = self.sign_menu
        m.clear()
        saved = self.store.list()
        for path in saved:
            pm = QPixmap(path)
            act = m.addAction(QIcon(pm.scaled(160, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)), "")
            act.triggered.connect(lambda _c=False, p=path: self._place_saved(p))
        if saved:
            m.addSeparator()
        m.addAction("New signature…", self.new_signature)
        if saved:
            rm = m.addMenu("Delete saved signature")
            for path in saved:
                pm = QPixmap(path)
                a = rm.addAction(QIcon(pm.scaled(160, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)), "")
                a.triggered.connect(lambda _c=False, p=path: self._delete_saved(p))
        m.setStyleSheet("QMenu { icon-size: 120px; }")

    def _place_saved(self, path, fit=None):
        with open(path, "rb") as fh:
            self.view.begin_place_image(fh.read(), fit)

    def _delete_saved(self, path):
        if QMessageBox.question(self, APP_NAME, "Delete this saved signature?") == QMessageBox.Yes:
            self.store.remove(path)

    def new_signature(self, fit=None):
        dlg = SignatureDialog(self)
        if dlg.exec() and dlg.png:
            if dlg.remember.isChecked():
                self.store.add(dlg.png)
            self.view.begin_place_image(dlg.png, fit)

    def _sign_field(self, page_index, rect: QRectF):
        saved = self.store.list()
        fit = (page_index, rect)
        if not saved:
            self.new_signature(fit)
            return
        m = QMenu(self)
        for path in saved:
            pm = QPixmap(path)
            a = m.addAction(QIcon(pm.scaled(160, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)), "")
            a.triggered.connect(lambda _c=False, p=path: self._place_saved(p, fit))
        m.addSeparator()
        m.addAction("New signature…", lambda: self.new_signature(fit))
        m.setStyleSheet("QMenu { icon-size: 120px; }")
        m.exec(self.cursor().pos())

    # ------------------------------------------------------------------ find
    def _show_find(self):
        self.findbar.show()
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def _hide_find(self):
        self.findbar.hide()
        self.view.clear_search()
        self._find_dirty = True
        self.find_status.clear()
        self.view.setFocus()

    def _find_next(self, backwards=False):
        text = self.find_edit.text()
        if self._find_dirty:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            n = self.view.search(text)
            QApplication.restoreOverrideCursor()
            self._find_dirty = False
            if n == 0:
                self.find_status.setText("  No matches" if text else "")
                return
        pos = self.view.next_hit(backwards)
        if pos >= 0:
            self.find_status.setText(f"  {pos + 1} of {len(self.view.search_hits)}")

    # ------------------------------------------------------------------ print
    def print_doc(self):
        if self.doc is None or self.doc.is_closed:
            return
        if self.view.modified:
            r = QMessageBox.question(self, APP_NAME, "Unsaved changes will not print. Save first?",
                                     QMessageBox.Save | QMessageBox.Ignore | QMessageBox.Cancel, QMessageBox.Save)
            if r == QMessageBox.Cancel or (r == QMessageBox.Save and not self.save()):
                return
        printer = QPrinter(QPrinter.HighResolution)
        dlg = QPrintDialog(printer, self)
        dlg.setMinMax(1, len(self.doc))
        dlg.setOption(QPrintDialog.PrintPageRange, True)
        if dlg.exec() != QPrintDialog.Accepted:
            return
        first, last = 0, len(self.doc) - 1
        if printer.printRange() == QPrinter.PageRange:
            first, last = printer.fromPage() - 1, printer.toPage() - 1
        elif printer.printRange() == QPrinter.CurrentPage:
            first = last = self.view.current_page()
        dpi = min(300, printer.resolution())
        prog = QProgressDialog("Printing…", "Cancel", 0, last - first + 1, self)
        prog.setWindowModality(Qt.WindowModal)
        painter = QPainter()
        if not painter.begin(printer):
            QMessageBox.critical(self, APP_NAME, "Could not start the print job.")
            return
        try:
            for n, i in enumerate(range(first, last + 1)):
                if prog.wasCanceled():
                    printer.abort()
                    break
                prog.setValue(n)
                if n:
                    printer.newPage()
                page = self.doc[i]
                pix = page.get_pixmap(dpi=dpi, alpha=False, annots=True)
                img = pixmap_to_qimage(pix)
                area = QRectF(painter.viewport())
                s = min(area.width() / img.width(), area.height() / img.height())
                w, h = img.width() * s, img.height() * s
                painter.drawImage(QRectF(area.x() + (area.width() - w) / 2, area.y() + (area.height() - h) / 2, w, h),
                                  img)
        finally:
            painter.end()
            prog.close()

    # ------------------------------------------------------------------ window events
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith(".pdf") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith(".pdf"):
                if self._confirm_discard():
                    self.open_path(p)
                break

    def closeEvent(self, e):
        if not self._confirm_discard():
            e.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        if self.doc is not None and not self.doc.is_closed:
            self.doc.close()
        e.accept()
