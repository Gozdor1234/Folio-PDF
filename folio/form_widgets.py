"""In-place editors overlaid on PDF form fields.

Each editor sits exactly on top of its field (scene units = PDF points) and
records whether the user changed it, so only touched fields get rewritten on
save. Untouched fields keep their original appearance streams byte-for-byte.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QComboBox, QLineEdit, QPlainTextEdit, QWidget

FIELD_BG = "rgb(221, 232, 255)"
FIELD_BG_Q = QColor(221, 232, 255)
FIELD_BORDER_Q = QColor(120, 150, 220)

# PDF field flag bits (PDF 32000-1, tables 221/226/228/230)
FF_READ_ONLY = 1 << 0
FF_MULTILINE = 1 << 12
FF_PASSWORD = 1 << 13
FF_COMBO_EDIT = 1 << 18


@dataclass
class FieldMeta:
    page_index: int
    xref: int
    field_type: int
    field_name: str
    original: object = None
    dirty: bool = False
    choices: list = field(default_factory=list)  # (export, display)


def _font_for_rect(rect_h: float, declared: float, multiline: bool) -> QFont:
    f = QFont("Arial")
    if declared and declared > 0:
        size = declared
    elif multiline:
        size = 10
    else:
        size = max(6.0, min(rect_h * 0.7, 12.0))
    f.setPixelSize(max(1, int(round(size))))
    return f


class _EditorMixin:
    changed = None  # set by subclasses as Signal

    def init_meta(self, meta: FieldMeta):
        self.meta = meta

    def mark_dirty(self):
        self.meta.dirty = True
        self.changed.emit(self)


class TextFieldEditor(QLineEdit, _EditorMixin):
    changed = Signal(object)

    def __init__(self, meta: FieldMeta, value: str, rect_h: float, fontsize: float, flags: int):
        super().__init__()
        self.init_meta(meta)
        self.setFrame(False)
        self.setTextMargins(1, 0, 1, 0)
        self.setFont(_font_for_rect(rect_h, fontsize, False))
        self.setStyleSheet(f"QLineEdit {{ background: {FIELD_BG}; border: none; color: black; }}")
        self.setText(value or "")
        if flags & FF_PASSWORD:
            self.setEchoMode(QLineEdit.Password)
        if flags & FF_READ_ONLY:
            self.setReadOnly(True)
        self.textEdited.connect(lambda _t: self.mark_dirty())

    def value(self):
        return self.text()


class MultiLineEditor(QPlainTextEdit, _EditorMixin):
    changed = Signal(object)

    def __init__(self, meta: FieldMeta, value: str, rect_h: float, fontsize: float, flags: int):
        super().__init__()
        self.init_meta(meta)
        self.setFrameShape(QPlainTextEdit.NoFrame)
        self.document().setDocumentMargin(1)
        self.setFont(_font_for_rect(rect_h, fontsize, True))
        self.setStyleSheet(f"QPlainTextEdit {{ background: {FIELD_BG}; border: none; color: black; }}")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setPlainText(value or "")
        if flags & FF_READ_ONLY:
            self.setReadOnly(True)
        self.textChanged.connect(self._on_text)

    def _on_text(self):
        if self.toPlainText() != (self.meta.original or ""):
            self.mark_dirty()

    def value(self):
        return self.toPlainText()


class CheckEditor(QWidget, _EditorMixin):
    """Checkbox or radio button drawn to scale inside the field rectangle."""

    changed = Signal(object)
    toggled_on = Signal(object)

    def __init__(self, meta: FieldMeta, checked: bool, radio: bool, read_only: bool):
        super().__init__()
        self.init_meta(meta)
        self.radio = radio
        self.checked = checked
        self.read_only = read_only
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor if not read_only else Qt.ArrowCursor)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

    def set_checked(self, on: bool, user: bool = False):
        if on == self.checked:
            return
        self.checked = on
        self.update()
        if user:
            self.mark_dirty()

    def _activate(self):
        if self.read_only:
            return
        if self.radio:
            if not self.checked:
                self.set_checked(True, user=True)
                self.toggled_on.emit(self)
        else:
            self.set_checked(not self.checked, user=True)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._activate()
            e.accept()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._activate()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.fillRect(r, FIELD_BG_Q)
        side = min(r.width(), r.height())
        box = QRectF(0, 0, side * 0.86, side * 0.86)
        box.moveCenter(r.center())
        pen = QPen(FIELD_BORDER_Q if not self.hasFocus() else QColor(40, 90, 200))
        pen.setWidthF(max(0.6, side * 0.06))
        p.setPen(pen)
        p.setBrush(Qt.white)
        if self.radio:
            p.drawEllipse(box)
        else:
            p.drawRect(box)
        if self.checked:
            p.setBrush(Qt.black)
            p.setPen(Qt.NoPen)
            if self.radio:
                dot = QRectF(0, 0, box.width() * 0.5, box.height() * 0.5)
                dot.moveCenter(box.center())
                p.drawEllipse(dot)
            else:
                pen = QPen(Qt.black)
                pen.setWidthF(max(0.8, side * 0.13))
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                b = box.adjusted(box.width() * 0.2, box.height() * 0.2, -box.width() * 0.2, -box.height() * 0.2)
                p.drawPolyline([
                    b.topLeft() + (b.bottomLeft() - b.topLeft()) * 0.55,
                    b.bottomLeft() + (b.bottomRight() - b.bottomLeft()) * 0.38,
                    b.topRight(),
                ])
        p.end()

    def value(self):
        return self.checked


class ComboEditor(QComboBox, _EditorMixin):
    changed = Signal(object)

    def __init__(self, meta: FieldMeta, value: str, rect_h: float, fontsize: float, flags: int):
        super().__init__()
        self.init_meta(meta)
        self.setFont(_font_for_rect(rect_h, fontsize, False))
        self.setStyleSheet(
            f"QComboBox {{ background: {FIELD_BG}; border: none; color: black; padding: 0px 1px; }}"
            "QComboBox::drop-down { width: 10px; border: none; }"
        )
        for export, display in meta.choices:
            self.addItem(display, export)
        if flags & FF_COMBO_EDIT:
            self.setEditable(True)
            self.lineEdit().setFrame(False)
        idx = self.findData(value)
        if idx < 0:
            idx = self.findText(value or "")
        if idx >= 0:
            self.setCurrentIndex(idx)
        elif self.isEditable():
            self.setEditText(value or "")
        else:
            self.setCurrentIndex(-1)
        if flags & FF_READ_ONLY:
            self.setEnabled(False)
        self.activated.connect(lambda _i: self.mark_dirty())
        if self.isEditable():
            self.lineEdit().textEdited.connect(lambda _t: self.mark_dirty())

    def value(self):
        if self.isEditable():
            txt = self.currentText()
            idx = self.findText(txt)
            return self.itemData(idx) if idx >= 0 else txt
        data = self.currentData()
        return data if data is not None else self.currentText()


class SignFieldButton(QWidget):
    """Overlay for an (unsigned) signature field: click to place a signature."""

    clicked = Signal(object)

    def __init__(self, meta: FieldMeta):
        super().__init__()
        self.meta = meta
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to place a signature here")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self)

    def paintEvent(self, _e):
        p = QPainter(self)
        # outline + small corner tag only, so a placed signature on top stays readable
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.fillRect(r, QColor(255, 244, 200, 60))
        pen = QPen(QColor(200, 150, 0))
        pen.setStyle(Qt.DashLine)
        pen.setWidthF(0.6)
        p.setPen(pen)
        p.drawRect(r)
        f = QFont("Arial")
        f.setPixelSize(max(4, int(min(r.height() * 0.22, 7))))
        p.setFont(f)
        p.setPen(QColor(150, 110, 0))
        p.drawText(r.adjusted(2, 1, -2, -1), Qt.AlignLeft | Qt.AlignTop, "Click to sign")
        p.end()


def parse_choices(widget) -> list:
    out = []
    for c in widget.choice_values or []:
        if isinstance(c, (list, tuple)) and len(c) == 2:
            out.append((str(c[0]), str(c[1])))
        else:
            out.append((str(c), str(c)))
    return out


def is_on(value) -> bool:
    return value not in (None, False, "", "Off", "off", "OFF")


def make_editor(page_index: int, w, display_rect: pymupdf.Rect):
    """Build the editor for a PyMuPDF widget, or None for unsupported types."""
    ft = w.field_type
    flags = w.field_flags or 0
    meta = FieldMeta(page_index, w.xref, ft, w.field_name or "")
    h = display_rect.height
    fs = w.text_fontsize or 0

    if ft == pymupdf.PDF_WIDGET_TYPE_TEXT:
        val = w.field_value or ""
        meta.original = val
        if flags & FF_MULTILINE:
            return MultiLineEditor(meta, val, h, fs, flags)
        return TextFieldEditor(meta, val, h, fs, flags)
    if ft in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
        on = is_on(w.field_value)
        meta.original = on
        return CheckEditor(meta, on, ft == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, bool(flags & FF_READ_ONLY))
    if ft in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
        meta.choices = parse_choices(w)
        val = w.field_value
        if isinstance(val, (list, tuple)):
            val = val[0] if val else ""
        val = "" if val is None else str(val)
        meta.original = val
        return ComboEditor(meta, val, h, fs, flags)
    if ft == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
        return SignFieldButton(meta)
    return None
