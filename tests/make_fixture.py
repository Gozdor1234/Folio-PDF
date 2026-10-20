"""Build a test PDF with every supported field type, plus a rotated page.

Page 1's fields are made with ReportLab (which writes radio groups the way
Acrobat-style forms do, with named on-states). A signature field, a rotated
page with a field, and a searchable page are added with PyMuPDF.
"""
import io
import sys

import pymupdf
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def page_one() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    H = letter[1]
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, H - 60, "Calibration Request Form")
    c.setFont("Helvetica", 11)
    form = c.acroForm
    rows = {"Name:": 100, "Notes:": 140, "Urgent:": 230, "Priority:": 260, "Lab:": 300, "Signature:": 360}
    for t, y in rows.items():
        c.drawString(72, H - y - 14, t)
    form.textfield(name="name", x=160, y=H - 118, width=260, height=18, borderWidth=0, fontSize=11)
    form.textfield(name="notes", x=160, y=H - 210, width=260, height=70, borderWidth=0, fontSize=10,
                   fieldFlags="multiline")
    form.checkbox(name="urgent", x=160, y=H - 242, size=14, buttonStyle="check")
    form.radio(name="priority", value="low", x=160, y=H - 272, size=14, selected=True)
    c.drawString(178, H - 270, "Low")
    form.radio(name="priority", value="high", x=220, y=H - 272, size=14)
    c.drawString(238, H - 270, "High")
    form.choice(name="lab", x=160, y=H - 318, width=140, height=18, options=["Ann Arbor", "Detroit", "Chicago"],
                value="Detroit", fontSize=10)
    c.showPage()
    c.save()
    return buf.getvalue()


def build(path):
    doc = pymupdf.open("pdf", page_one())
    p = doc[0]
    w = pymupdf.Widget()
    w.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    w.field_name = "sig"
    w.rect = pymupdf.Rect(160, 350, 360, 400)
    p.add_widget(w)

    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 80), "Rotated page with a field", fontsize=14)
    w = pymupdf.Widget()
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.field_name = "rotated_field"
    w.rect = pymupdf.Rect(72, 100, 300, 120)
    p2.add_widget(w)
    p2.set_rotation(90)

    p3 = doc.new_page()
    p3.insert_text((72, 100), "Searchable thermometer serial TX-4471 appears here.", fontsize=12)
    p3.insert_text((72, 140), "Second thermometer line for search.", fontsize=12)
    doc.save(path)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "fixture.pdf")
