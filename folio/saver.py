"""Write the user's edits into a PDF.

Edits are applied to a separate working copy opened from disk, never to the
document being displayed. If anything fails, the on-screen edits are intact
and the user can retry or Save As elsewhere.
"""
from __future__ import annotations

import os
import tempfile

import pymupdf

from .document_view import Edits


class SaveError(Exception):
    pass


def _set_field(w, ftype, value):
    if ftype in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
        w.field_value = w.on_state() if value else "Off"
    else:
        w.field_value = "" if value is None else str(value)
    w.update()


def apply_edits(doc: pymupdf.Document, edits: Edits):
    # 1. form fields, grouped by page so each page's widget list is walked once
    by_page: dict[int, dict[int, tuple]] = {}
    for meta, value in edits.fields:
        by_page.setdefault(meta.page_index, {})[meta.xref] = (meta.field_type, value)
    selected_radios = []  # set after everything else so "Off" siblings can't override them
    for pi, wanted in by_page.items():
        page = doc[pi]
        for w in page.widgets():
            if w.xref not in wanted:
                continue
            ftype, value = wanted.pop(w.xref)
            if ftype == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON and value:
                selected_radios.append((pi, w.xref))
            else:
                _set_field(w, ftype, value)
        if wanted:
            raise SaveError(f"Could not find {len(wanted)} form field(s) on page {pi + 1}.")
    for pi, xref in selected_radios:
        for w in doc[pi].widgets():
            if w.xref == xref:
                _set_field(w, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, True)
                break

    # 2. signatures / images (display coords -> unrotated page coords)
    for img in edits.images:
        page = doc[img.page]
        r = pymupdf.Rect(*img.rect) * page.derotation_matrix
        page.insert_image(r, stream=img.png, keep_proportion=True, overlay=True, rotate=page.rotation)

    # 3. free text
    for t in edits.texts:
        page = doc[t.page]
        origin = pymupdf.Point(t.origin[0], t.origin[1] + t.baseline) * page.derotation_matrix
        page.insert_text(origin, t.text, fontsize=t.size, fontname="helv", color=(0, 0, 0),
                         rotate=page.rotation, lineheight=t.line_height)


def _open_copy(path: str, password: str | None) -> pymupdf.Document:
    work = pymupdf.open(path)
    if work.needs_pass and not work.authenticate(password or ""):
        work.close()
        raise SaveError("Could not unlock the file for saving.")
    return work


def has_signed_fields(doc: pymupdf.Document) -> bool:
    if not doc.is_form_pdf:
        return False
    for page in doc:
        for w in page.widgets(types=[pymupdf.PDF_WIDGET_TYPE_SIGNATURE]):
            if w.field_value:
                return True
    return False


def save_in_place(display_doc: pymupdf.Document, path: str, edits: Edits, password: str | None,
                  reopen) -> pymupdf.Document:
    """Save edits back into `path`. Returns the freshly opened document.

    Uses an incremental save when the file allows it (appends changes, leaving
    the original bytes, including any existing digital signatures, untouched).
    Otherwise writes a full copy to a temp file and swaps it in.
    """
    work = _open_copy(path, password)
    try:
        apply_edits(work, edits)
        if work.can_save_incrementally():
            display_doc.close()
            try:
                work.saveIncr()
            finally:
                work.close()
            return reopen(path)
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(os.path.abspath(path)))
        os.close(fd)
        work.save(tmp, garbage=3, deflate=True)
    finally:
        if not work.is_closed:
            work.close()
    display_doc.close()
    try:
        os.replace(tmp, path)
    except OSError as e:
        raise SaveError(
            f"The file is locked by another program (is it open in Acrobat or Explorer preview?).\n"
            f"Your changes were saved to:\n{tmp}\n\n({e})", ) from e
    return reopen(path)


def save_as(path: str, new_path: str, edits: Edits, password: str | None):
    if os.path.abspath(path) == os.path.abspath(new_path):
        raise SaveError("Use Save to overwrite the current file.")
    work = _open_copy(path, password)
    try:
        apply_edits(work, edits)
        work.save(new_path, garbage=3, deflate=True)
    finally:
        work.close()
