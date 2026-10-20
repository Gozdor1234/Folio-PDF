"""Continuous-scroll PDF view.

Scene coordinates are PDF points. Zoom is a view transform, so form editors,
signatures and text boxes keep fixed page coordinates at every zoom level.
Pages are rendered lazily: only those near the viewport are rasterized, at the
exact device resolution, and far-away pages release their pixmaps.
"""
from __future__ import annotations

from dataclasses import dataclass

import pymupdf
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsProxyWidget, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsTextItem, QGraphicsView, QMenu)

from .form_widgets import CheckEditor, SignFieldButton, make_editor
from .signature import PlacedImageItem

PAGE_GAP = 12.0
PX_PER_PT = 96.0 / 72.0  # 100% zoom == physical size on a standard 96-dpi display
MIN_ZOOM, MAX_ZOOM = 0.1, 8.0
DEFAULT_TEXT_SIZE = 11.0


def pixmap_to_qimage(pix: pymupdf.Pixmap) -> QImage:
    fmt = QImage.Format_RGB888 if pix.n == 3 else QImage.Format_RGBA8888
    return QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()


class PageItem(QGraphicsItem):
    def __init__(self, index: int, w: float, h: float):
        super().__init__()
        self.index = index
        self.w, self.h = w, h
        self.pixmap: QPixmap | None = None
        self.rendered_scale = 0.0
        self.setZValue(0)

    def boundingRect(self):
        return QRectF(-1, -1, self.w + 4, self.h + 4)

    def paint(self, p, _opt, _w=None):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 45))
        p.drawRect(QRectF(1.5, 1.5, self.w, self.h))
        p.setBrush(Qt.white)
        p.drawRect(QRectF(0, 0, self.w, self.h))
        if self.pixmap is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawPixmap(QRectF(0, 0, self.w, self.h), self.pixmap, QRectF(self.pixmap.rect()))


class PlacedTextItem(QGraphicsTextItem):
    """Free text typed onto the page (for forms with no fillable fields)."""

    def __init__(self, size: float, on_change):
        super().__init__()
        f = QFont("Arial")
        f.setPixelSize(int(round(size)))
        self.setFont(f)
        self.size = size
        self.setDefaultTextColor(Qt.black)
        self.document().setDocumentMargin(0)
        self.on_change = on_change
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemIsFocusable | QGraphicsItem.ItemSendsGeometryChanges)
        self.setZValue(50)
        self.document().contentsChanged.connect(self.on_change)

    def start_editing(self):
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFocus(Qt.MouseFocusReason)
        self.setCursor(Qt.IBeamCursor)

    def mouseDoubleClickEvent(self, e):
        self.start_editing()
        super().mouseDoubleClickEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setCursor(Qt.SizeAllCursor)
        c = self.textCursor()
        c.clearSelection()
        self.setTextCursor(c)
        if not self.toPlainText().strip():
            self.remove()

    def keyPressEvent(self, e):
        editing = bool(self.textInteractionFlags() & Qt.TextEditable)
        if not editing and e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove()
            return
        if editing and e.key() == Qt.Key_Escape:
            self.clearFocus()
            return
        super().keyPressEvent(e)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.on_change()
        return super().itemChange(change, value)

    def paint(self, p, opt, w=None):
        r = self.boundingRect()
        editing = bool(self.textInteractionFlags() & Qt.TextEditable)
        if editing or self.isSelected():
            p.setPen(QPen(QColor(40, 110, 230), 0, Qt.DashLine))
            p.setBrush(QColor(221, 232, 255, 90))
            p.drawRect(r)
        super().paint(p, opt, w)

    def contextMenuEvent(self, e):
        m = QMenu()
        edit = m.addAction("Edit text")
        delete = m.addAction("Delete")
        act = m.exec(e.screenPos())
        if act == delete:
            self.remove()
        elif act == edit:
            self.start_editing()

    def remove(self):
        sc = self.scene()
        if sc:
            sc.removeItem(self)
            self.on_change()


@dataclass
class ImageEdit:
    page: int
    rect: tuple  # x0, y0, x1, y1 in displayed (rotated) page coords
    png: bytes


@dataclass
class TextEdit:
    page: int
    origin: tuple  # top-left in displayed page coords
    text: str
    size: float
    baseline: float  # distance from top of box to first baseline
    line_height: float  # line spacing as a multiple of size


@dataclass
class Edits:
    fields: list
    images: list
    texts: list

    def empty(self):
        return not (self.fields or self.images or self.texts)


class PdfView(QGraphicsView):
    pageChanged = Signal(int, int)
    zoomChanged = Signal(float)
    modifiedChanged = Signal(bool)
    signFieldRequested = Signal(int, object)  # page index, QRectF (page coords)
    modeChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor(82, 86, 89))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.doc: pymupdf.Document | None = None
        self.pages: list[PageItem] = []
        self.editors: list = []
        self.radio_groups: dict[str, list[CheckEditor]] = {}
        self.search_items: list[QGraphicsRectItem] = []
        self.search_hits: list[tuple[int, QRectF]] = []
        self.search_pos = -1
        self.zoom = 1.0
        self._modified = False
        self.mode = "normal"
        self._pending_png: bytes | None = None
        self._pending_fit: tuple[int, QRectF] | None = None
        self.text_size = DEFAULT_TEXT_SIZE
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_visible)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.horizontalScrollBar().valueChanged.connect(lambda _v: self.schedule_render())

    # ------------------------------------------------------------------ loading
    def clear(self):
        self.scene().clear()
        self.pages.clear()
        self.editors.clear()
        self.radio_groups.clear()
        self.search_items.clear()
        self.search_hits.clear()
        self.doc = None
        self.set_modified(False)

    def load(self, doc: pymupdf.Document, keep_view: bool = False):
        old_zoom = self.zoom
        old_center = self.mapToScene(self.viewport().rect().center()) if keep_view else None
        self.clear()
        self.doc = doc
        y = 0.0
        sizes = [(p.rect.width, p.rect.height) for p in doc]
        maxw = max((w for w, _ in sizes), default=612)
        for i, (w, h) in enumerate(sizes):
            item = PageItem(i, w, h)
            item.setPos((maxw - w) / 2, y)
            self.scene().addItem(item)
            self.pages.append(item)
            y += h + PAGE_GAP
        self.scene().setSceneRect(QRectF(-PAGE_GAP, -PAGE_GAP, maxw + 2 * PAGE_GAP, y + PAGE_GAP))
        if doc.is_form_pdf:
            self._build_form_editors()
        if keep_view:
            self.set_zoom(old_zoom)
            self.centerOn(old_center)
        else:
            self.fit_width()
            self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
        self.schedule_render()
        self._on_scroll()

    def _build_form_editors(self):
        for pi, page in enumerate(self.doc):
            rot = page.rotation_matrix
            for w in page.widgets():
                if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE and w.field_value:
                    continue  # already digitally signed: leave untouched
                r = w.rect * rot
                ed = make_editor(pi, w, r)
                if ed is None:
                    continue
                proxy = QGraphicsProxyWidget(self.pages[pi])
                proxy.setWidget(ed)
                proxy.setPos(r.x0, r.y0)
                ed.setFixedSize(max(1, int(round(r.width))), max(1, int(round(r.height))))
                proxy.resize(r.width, r.height)
                proxy.setZValue(10)
                if isinstance(ed, SignFieldButton):
                    ed.clicked.connect(self._on_sign_field)
                    continue
                ed.changed.connect(lambda _e: self.set_modified(True))
                self.editors.append(ed)
                if isinstance(ed, CheckEditor) and ed.radio:
                    self.radio_groups.setdefault(ed.meta.field_name, []).append(ed)
                    ed.toggled_on.connect(self._on_radio)

    def _on_radio(self, chosen: CheckEditor):
        for other in self.radio_groups.get(chosen.meta.field_name, []):
            if other is not chosen and other.checked:
                other.set_checked(False, user=True)

    def _on_sign_field(self, btn: SignFieldButton):
        proxy = btn.graphicsProxyWidget()
        rect = QRectF(proxy.pos(), proxy.size())
        self.signFieldRequested.emit(btn.meta.page_index, rect)

    # ------------------------------------------------------------------ state
    def doc_ok(self) -> bool:
        # never use `not self.doc`: a pymupdf Document's truthiness is its page count,
        # and evaluating it on a closed document raises
        return self.doc is not None and not self.doc.is_closed

    def set_modified(self, on: bool):
        if on != self._modified:
            self._modified = on
            self.modifiedChanged.emit(on)

    @property
    def modified(self):
        return self._modified

    def _item_changed(self):
        self.set_modified(True)

    # ------------------------------------------------------------------ zoom / navigation
    def set_zoom(self, z: float):
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        self.zoom = z
        s = z * PX_PER_PT
        self.setTransform(QTransform.fromScale(s, s))
        self.zoomChanged.emit(z)
        self.schedule_render(60)

    def zoom_by(self, factor: float):
        anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        self.set_zoom(self.zoom * factor)
        self.setTransformationAnchor(anchor)

    def fit_width(self):
        if not self.pages:
            return
        maxw = max(p.w for p in self.pages) + 2 * PAGE_GAP
        avail = self.viewport().width() - 4
        self.set_zoom(avail / (maxw * PX_PER_PT))

    def fit_page(self):
        if not self.pages:
            return
        cur = self.current_page()
        p = self.pages[cur]
        zw = (self.viewport().width() - 8) / ((p.w + 2 * PAGE_GAP) * PX_PER_PT)
        zh = (self.viewport().height() - 8) / ((p.h + 2 * PAGE_GAP) * PX_PER_PT)
        self.set_zoom(min(zw, zh))
        self.go_to_page(cur)

    def go_to_page(self, i: int):
        if not self.pages:
            return
        i = max(0, min(len(self.pages) - 1, i))
        p = self.pages[i]
        top = self.mapFromScene(QPointF(p.x(), p.y() - PAGE_GAP / 2)).y()
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + top)

    def current_page(self) -> int:
        if not self.pages:
            return 0
        c = self.mapToScene(self.viewport().rect().center()).y()
        for p in self.pages:
            if p.y() + p.h + PAGE_GAP / 2 >= c:
                return p.index
        return len(self.pages) - 1

    def _on_scroll(self, *_):
        if self.pages:
            self.pageChanged.emit(self.current_page(), len(self.pages))
        self.schedule_render()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            factor = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
            self.set_zoom(self.zoom * factor)
            e.accept()
            return
        super().wheelEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.schedule_render()

    # ------------------------------------------------------------------ rendering
    def schedule_render(self, delay: int = 15):
        self._render_timer.start(delay)

    def _render_visible(self):
        if not self.doc_ok():
            return
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        margin = vis.height() * 0.75
        near = vis.adjusted(0, -margin, 0, margin)
        keep = vis.adjusted(0, -vis.height() * 4, 0, vis.height() * 4)
        target = self.zoom * PX_PER_PT * self.viewport().devicePixelRatioF()
        # render visible pages first, then the prefetch band
        order = sorted(self.pages, key=lambda p: 0 if p.sceneBoundingRect().intersects(vis) else 1)
        for p in order:
            br = p.sceneBoundingRect()
            if br.intersects(near):
                if abs(p.rendered_scale - target) > 1e-3:
                    self._render_page(p, target)
            elif p.pixmap is not None and not br.intersects(keep):
                p.pixmap = None
                p.rendered_scale = 0.0

    def _render_page(self, item: PageItem, scale: float):
        page = self.doc[item.index]
        # cap a single page bitmap at ~40 MP so extreme zoom can't exhaust memory
        max_px = 40_000_000
        eff = scale
        if item.w * item.h * eff * eff > max_px:
            eff = (max_px / (item.w * item.h)) ** 0.5
        pix = page.get_pixmap(matrix=pymupdf.Matrix(eff, eff), alpha=False, annots=True)
        item.pixmap = QPixmap.fromImage(pixmap_to_qimage(pix))
        item.rendered_scale = scale  # the requested scale, so a capped page isn't re-rendered every scroll
        item.update()

    # ------------------------------------------------------------------ placing signatures and text
    def begin_place_image(self, png: bytes, fit: tuple[int, QRectF] | None = None):
        if fit is not None:
            self._place_image(png, fit[0], fit[1], fit_inside=True)
            return
        self._pending_png = png
        self._set_mode("place_image")

    def begin_place_text(self):
        self._set_mode("place_text")

    def cancel_mode(self):
        self._pending_png = None
        self._set_mode("normal")

    def _set_mode(self, mode: str):
        self.mode = mode
        self.viewport().setCursor(Qt.CrossCursor if mode != "normal" else Qt.ArrowCursor)
        self.modeChanged.emit(mode)

    def page_at(self, scene_pt: QPointF) -> PageItem | None:
        for p in self.pages:
            if p.sceneBoundingRect().contains(scene_pt):
                return p
        return None

    def _place_image(self, png: bytes, page_index: int, at, fit_inside=False):
        page = self.pages[page_index]
        item = PlacedImageItem(png, 150.0, self._item_changed)
        if fit_inside:
            rect: QRectF = at
            w = min(rect.width(), rect.height() * item.aspect)
            item.prepareGeometryChange()
            item.w, item.h = w, w / item.aspect
            local = QPointF(rect.x() + (rect.width() - item.w) / 2, rect.y() + (rect.height() - item.h) / 2)
        else:
            w = min(150.0, page.w * 0.6)
            item.prepareGeometryChange()
            item.w, item.h = w, w / item.aspect
            local = QPointF(at.x() - item.w / 2, at.y() - item.h / 2)
        self.scene().addItem(item)
        item.setPos(page.mapToScene(local))
        self.scene().clearSelection()
        item.setSelected(True)
        item.setFocus()
        self.set_modified(True)
        return item

    def mousePressEvent(self, e):
        if self.mode != "normal" and e.button() == Qt.LeftButton:
            sp = self.mapToScene(e.position().toPoint())
            page = self.page_at(sp)
            if page is None:
                return
            local = page.mapFromScene(sp)
            if self.mode == "place_image" and self._pending_png:
                self._place_image(self._pending_png, page.index, local)
            elif self.mode == "place_text":
                t = PlacedTextItem(self.text_size, self._item_changed)
                self.scene().addItem(t)
                t.setPos(sp - QPointF(0, self.text_size * 0.6))
                self.scene().clearSelection()
                t.setSelected(True)
                t.start_editing()
                self.set_modified(True)
            self.cancel_mode()
            return
        if e.button() == Qt.RightButton and self.mode != "normal":
            self.cancel_mode()
            return
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.mode != "normal":
            self.cancel_mode()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------------ collecting edits
    def collect_edits(self) -> Edits:
        fields = [(ed.meta, ed.value()) for ed in self.editors if ed.meta.dirty]
        images, texts = [], []
        for it in self.scene().items():
            if isinstance(it, PlacedImageItem):
                center = it.mapToScene(it.content_rect().center())
                page = self.page_at(center)
                if page is None:
                    continue
                tl = page.mapFromScene(it.mapToScene(QPointF(0, 0)))
                images.append(ImageEdit(page.index, (tl.x(), tl.y(), tl.x() + it.w, tl.y() + it.h), it.png))
            elif isinstance(it, PlacedTextItem):
                text = it.toPlainText().rstrip()
                if not text.strip():
                    continue
                page = self.page_at(it.sceneBoundingRect().center())
                if page is None:
                    continue
                tl = page.mapFromScene(it.scenePos())
                fm = QFontMetricsF(it.font())
                texts.append(TextEdit(page.index, (tl.x(), tl.y()), text, it.size,
                                      fm.ascent(), fm.lineSpacing() / it.size))
        return Edits(fields, images, texts)

    # ------------------------------------------------------------------ search
    def clear_search(self):
        for it in self.search_items:
            if it.scene():
                self.scene().removeItem(it)
        self.search_items.clear()
        self.search_hits.clear()
        self.search_pos = -1

    def search(self, text: str) -> int:
        self.clear_search()
        if not self.doc_ok() or not text:
            return 0
        for pi, page in enumerate(self.doc):
            rot = page.rotation_matrix
            for r in page.search_for(text):
                rr = r * rot
                qr = QRectF(rr.x0, rr.y0, rr.width, rr.height)
                hl = QGraphicsRectItem(qr, self.pages[pi])
                hl.setBrush(QColor(255, 210, 0, 90))
                hl.setPen(Qt.NoPen)
                hl.setZValue(5)
                self.search_items.append(hl)
                self.search_hits.append((pi, qr))
        return len(self.search_hits)

    def next_hit(self, backwards=False) -> int:
        if not self.search_hits:
            return -1
        n = len(self.search_hits)
        self.search_pos = (self.search_pos + (-1 if backwards else 1)) % n
        for i, it in enumerate(self.search_items):
            it.setBrush(QColor(255, 140, 0, 140) if i == self.search_pos else QColor(255, 210, 0, 90))
        pi, r = self.search_hits[self.search_pos]
        self.centerOn(self.pages[pi].mapToScene(r.center()))
        return self.search_pos
