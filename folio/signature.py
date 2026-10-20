"""Signature creation (draw / type / image), saved-signature storage, and the
movable, resizable item used to place a signature or stamp on a page.

These are visual signatures: an image stamped onto the page. They are not
certificate-based digital signatures and provide no tamper evidence.
"""
from __future__ import annotations

import os
import uuid

import numpy as np

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, QStandardPaths, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QGraphicsItem, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                               QRadioButton, QSlider, QTabWidget, QVBoxLayout, QWidget)

INK_COLORS = {"Black": QColor(20, 20, 20), "Blue": QColor(16, 52, 166)}
SCRIPT_FONTS = ["Segoe Script", "Lucida Handwriting", "Ink Free", "Brush Script MT", "Segoe Print",
                "Freestyle Script", "Mistral", "Bradley Hand ITC", "Kristen ITC"]


# --------------------------------------------------------------------------- helpers
def qimage_to_png(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def _argb_array(img: QImage):
    img = img.convertToFormat(QImage.Format_ARGB32)
    arr = np.frombuffer(img.constBits(), dtype=np.uint8).reshape(img.height(), img.bytesPerLine())
    return img, arr[:, : img.width() * 4].reshape(img.height(), img.width(), 4)  # BGRA in memory


def trim_transparent(img: QImage, pad: int = 4) -> QImage:
    img, a = _argb_array(img)
    alpha = a[:, :, 3]
    ys, xs = np.nonzero(alpha)
    if len(xs) == 0:
        return img
    x0, y0 = max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad)
    x1, y1 = min(img.width(), int(xs.max()) + pad + 1), min(img.height(), int(ys.max()) + pad + 1)
    return img.copy(x0, y0, x1 - x0, y1 - y0)


def whiten_to_alpha(img: QImage, threshold: int = 225) -> QImage:
    img, a = _argb_array(img)
    out = a.copy()
    near_white = (a[:, :, 0] >= threshold) & (a[:, :, 1] >= threshold) & (a[:, :, 2] >= threshold)
    out[near_white] = 0
    h, w = out.shape[:2]
    res = QImage(out.tobytes(), w, h, w * 4, QImage.Format_ARGB32)
    return res.copy()


# --------------------------------------------------------------------------- storage
class SignatureStore:
    def __init__(self):
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        self.dir = os.path.join(base, "signatures")
        os.makedirs(self.dir, exist_ok=True)

    def list(self) -> list[str]:
        files = [os.path.join(self.dir, f) for f in os.listdir(self.dir) if f.lower().endswith(".png")]
        return sorted(files, key=os.path.getmtime)

    def add(self, png: bytes) -> str:
        path = os.path.join(self.dir, f"{uuid.uuid4().hex}.png")
        with open(path, "wb") as fh:
            fh.write(png)
        return path

    def remove(self, path: str):
        if os.path.dirname(os.path.abspath(path)) == os.path.abspath(self.dir):
            os.remove(path)


# --------------------------------------------------------------------------- dialog
class DrawCanvas(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(460, 170)
        self.strokes: list[list[QPointF]] = []
        self.pen_width = 3.0
        self.color = INK_COLORS["Black"]
        self.setCursor(Qt.CrossCursor)

    def clear(self):
        self.strokes.clear()
        self.update()

    def is_empty(self):
        return not any(len(s) > 1 for s in self.strokes)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.strokes.append([e.position()])
            self.update()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton and self.strokes:
            self.strokes[-1].append(e.position())
            self.update()

    @staticmethod
    def _path(stroke):
        path = QPainterPath(stroke[0])
        if len(stroke) < 3:
            for pt in stroke[1:]:
                path.lineTo(pt)
            return path
        for i in range(1, len(stroke) - 1):
            mid = (stroke[i] + stroke[i + 1]) / 2
            path.quadTo(stroke[i], mid)
        path.lineTo(stroke[-1])
        return path

    def _draw(self, p: QPainter, scale: float = 1.0, offset=QPointF(0, 0)):
        pen = QPen(self.color, self.pen_width * scale, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        for s in self.strokes:
            if len(s) == 1:
                p.drawPoint((s[0] - offset) * scale)
            else:
                p.drawPath(self._path([(pt - offset) * scale for pt in s]))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), Qt.white)
        p.setPen(QPen(QColor(200, 200, 200), 1, Qt.DashLine))
        y = self.height() * 0.72
        p.drawLine(QPointF(20, y), QPointF(self.width() - 20, y))
        p.setPen(QColor(170, 170, 170))
        p.drawText(QPointF(20, y + 18), "Sign above the line")
        self._draw(p)
        p.end()

    def to_image(self, scale: float = 3.0) -> QImage:
        pts = [pt for s in self.strokes for pt in s]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        pad = self.pen_width * 2
        x0, y0 = min(xs) - pad, min(ys) - pad
        w, h = max(xs) - x0 + pad, max(ys) - y0 + pad
        img = QImage(int(w * scale) + 1, int(h * scale) + 1, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        self._draw(p, scale, QPointF(x0, y0))
        p.end()
        return img


def available_script_fonts() -> list[str]:
    fams = set(QFontDatabase.families())
    found = [f for f in SCRIPT_FONTS if f in fams]
    return found or sorted(fams)


def render_typed(text: str, family: str, color: QColor) -> QImage:
    f = QFont(family)
    f.setPixelSize(110)
    fm = QFontMetricsF(f)
    br = fm.boundingRect(text)
    pad = 20
    img = QImage(int(br.width() + pad * 2 + fm.averageCharWidth()), int(fm.height() + pad * 2), QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setFont(f)
    p.setPen(color)
    p.drawText(QPointF(pad - br.left(), pad + fm.ascent()), text)
    p.end()
    return trim_transparent(img)


class SignatureDialog(QDialog):
    """Returns PNG bytes via .png and whether to remember it via .remember."""

    def __init__(self, parent=None, title="Create signature"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.png: bytes | None = None
        self.loaded_image: QImage | None = None

        self.tabs = QTabWidget()
        # draw
        draw = QWidget()
        dl = QVBoxLayout(draw)
        self.canvas = DrawCanvas()
        dl.addWidget(self.canvas)
        row = QHBoxLayout()
        row.addWidget(QLabel("Pen:"))
        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setRange(1, 8)
        self.width_slider.setValue(3)
        self.width_slider.setMaximumWidth(140)
        self.width_slider.valueChanged.connect(self._pen_changed)
        row.addWidget(self.width_slider)
        row.addStretch()
        clear = QPushButton("Clear")
        clear.clicked.connect(self.canvas.clear)
        row.addWidget(clear)
        dl.addLayout(row)
        self.tabs.addTab(draw, "Draw")

        # type
        typ = QWidget()
        tl = QVBoxLayout(typ)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Type your name")
        self.font_combo = QComboBox()
        self.font_combo.addItems(available_script_fonts())
        self.preview = QLabel()
        self.preview.setMinimumHeight(110)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background: white; border: 1px solid #ccc;")
        tl.addWidget(self.name_edit)
        tl.addWidget(self.font_combo)
        tl.addWidget(self.preview, 1)
        self.name_edit.textChanged.connect(self._update_preview)
        self.font_combo.currentIndexChanged.connect(self._update_preview)
        self.tabs.addTab(typ, "Type")

        # image
        im = QWidget()
        il = QVBoxLayout(im)
        pick = QPushButton("Choose image (PNG / JPG)…")
        pick.clicked.connect(self._pick_image)
        self.whiten = QCheckBox("Make white background transparent")
        self.whiten.setChecked(True)
        self.whiten.toggled.connect(self._update_image_preview)
        self.image_preview = QLabel("No image selected")
        self.image_preview.setMinimumHeight(110)
        self.image_preview.setAlignment(Qt.AlignCenter)
        self.image_preview.setStyleSheet("background: white; border: 1px solid #ccc;")
        il.addWidget(pick)
        il.addWidget(self.whiten)
        il.addWidget(self.image_preview, 1)
        self.tabs.addTab(im, "Image")

        # ink + remember
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Ink:"))
        self.ink_group = QButtonGroup(self)
        for i, name in enumerate(INK_COLORS):
            rb = QRadioButton(name)
            rb.setChecked(i == 0)
            self.ink_group.addButton(rb)
            bottom.addWidget(rb)
        self.ink_group.buttonToggled.connect(self._ink_changed)
        bottom.addStretch()
        self.remember = QCheckBox("Save for reuse")
        self.remember.setChecked(True)
        bottom.addWidget(self.remember)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Place")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        lay.addLayout(bottom)
        lay.addWidget(buttons)
        self.resize(540, 360)

    def _color(self) -> QColor:
        btn = self.ink_group.checkedButton()
        return INK_COLORS[btn.text()] if btn else INK_COLORS["Black"]

    def _pen_changed(self, v):
        self.canvas.pen_width = float(v)
        self.canvas.update()

    def _ink_changed(self, *_):
        self.canvas.color = self._color()
        self.canvas.update()
        self._update_preview()

    def _update_preview(self):
        text = self.name_edit.text().strip()
        if not text:
            self.preview.clear()
            return
        img = render_typed(text, self.font_combo.currentText(), self._color())
        self.preview.setPixmap(QPixmap.fromImage(img).scaled(
            self.preview.width() - 10, self.preview.height() - 10, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _pick_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose signature image", "",
                                              "Images (*.png *.jpg *.jpeg *.bmp)")
        if path:
            img = QImage(path)
            if img.isNull():
                self.image_preview.setText("Could not read that image")
                self.loaded_image = None
                return
            # keep processing fast: cap to 1600px wide
            if img.width() > 1600:
                img = img.scaledToWidth(1600, Qt.SmoothTransformation)
            self.loaded_image = img
            self._update_image_preview()

    def _processed_image(self) -> QImage | None:
        if self.loaded_image is None:
            return None
        img = self.loaded_image
        if self.whiten.isChecked():
            img = whiten_to_alpha(img)
        return trim_transparent(img)

    def _update_image_preview(self):
        img = self._processed_image()
        if img is not None:
            self.image_preview.setPixmap(QPixmap.fromImage(img).scaled(
                self.image_preview.width() - 10, self.image_preview.height() - 10,
                Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _accept(self):
        tab = self.tabs.currentIndex()
        img = None
        if tab == 0 and not self.canvas.is_empty():
            img = self.canvas.to_image()
        elif tab == 1 and self.name_edit.text().strip():
            img = render_typed(self.name_edit.text().strip(), self.font_combo.currentText(), self._color())
        elif tab == 2:
            img = self._processed_image()
        if img is None or img.isNull():
            return
        self.png = qimage_to_png(img)
        self.accept()


# --------------------------------------------------------------------------- placed item
class PlacedImageItem(QGraphicsItem):
    """A signature/stamp image the user can drag and resize (aspect locked)."""

    HANDLE = 7.0

    def __init__(self, png: bytes, width: float, on_change):
        super().__init__()
        self.png = png
        self.pixmap = QPixmap()
        self.pixmap.loadFromData(png, "PNG")
        self.aspect = self.pixmap.width() / max(1, self.pixmap.height())
        self.w = width
        self.h = width / self.aspect
        self.on_change = on_change
        self._resizing = False
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemIsFocusable | QGraphicsItem.ItemSendsGeometryChanges)
        self.setZValue(50)
        self.setCursor(Qt.SizeAllCursor)
        self.setAcceptHoverEvents(True)

    def content_rect(self) -> QRectF:
        return QRectF(0, 0, self.w, self.h)

    def _handle_rect(self) -> QRectF:
        s = self.HANDLE
        return QRectF(self.w - s / 2, self.h - s / 2, s, s)

    def boundingRect(self):
        return QRectF(-1, -1, self.w + self.HANDLE, self.h + self.HANDLE)

    def paint(self, p, _opt, _w=None):
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(self.content_rect(), self.pixmap, QRectF(self.pixmap.rect()))
        if self.isSelected():
            pen = QPen(QColor(40, 110, 230), 0)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.content_rect())
            p.setPen(QPen(QColor(40, 110, 230), 0))
            p.setBrush(Qt.white)
            p.drawRect(self._handle_rect())

    def hoverMoveEvent(self, e):
        self.setCursor(Qt.SizeFDiagCursor if self.isSelected() and self._handle_rect().contains(e.pos())
                       else Qt.SizeAllCursor)

    def mousePressEvent(self, e):
        if self.isSelected() and self._handle_rect().contains(e.pos()):
            self._resizing = True
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._resizing:
            self.prepareGeometryChange()
            nw = max(12.0, e.pos().x(), e.pos().y() * self.aspect)
            self.w, self.h = nw, nw / self.aspect
            self.update()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._resizing:
            self._resizing = False
            self.on_change()
            return
        super().mouseReleaseEvent(e)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.on_change()
        return super().itemChange(change, value)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove()
        else:
            super().keyPressEvent(e)

    def contextMenuEvent(self, e):
        m = QMenu()
        act = m.addAction("Delete")
        if m.exec(e.screenPos()) == act:
            self.remove()

    def remove(self):
        sc = self.scene()
        if sc:
            sc.removeItem(self)
            self.on_change()
