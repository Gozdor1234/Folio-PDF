# Folio PDF

A fast, lightweight PDF viewer for Windows focused on filling forms and signing.

Rendering uses MuPDF (via PyMuPDF), so pages appear in milliseconds; only the pages near the screen are rasterized, and pages far away release their memory.

## Features

- Continuous scroll, Ctrl+wheel zoom, fit width / fit page, page jump, find (Ctrl+F), print, drag and drop
- **Form filling:** text fields, multi-line fields, checkboxes, radio groups, dropdowns. Only fields you change are rewritten; untouched fields keep their original appearance.
- **Add text** (Ctrl+T): click anywhere and type, for forms that have no fillable fields. Saved as real, searchable PDF text.
- **Signatures:** draw, type, or import an image (with automatic white-background removal). Saved signatures are reusable from the Sign menu. Click an empty signature field to drop a signature into it, or place one anywhere and drag/resize it. Delete key removes a selected item.
- **Safe saving:** Save uses an incremental save when possible (appends changes, original bytes untouched). Edits are written to a separate copy first, so a failed save (for example the file is open in another program) never loses your on-screen work.

## Important limitations

- Signatures here are **visual** (an image stamped on the page). They are not certificate-based digital signatures and provide no tamper evidence. Don't use them where a regulated electronic signature is required (e.g. 21 CFR Part 11 records).
- If a PDF is already digitally signed, the app warns before saving, because viewers will flag the document as modified after signing.
- Added text uses Helvetica, which covers Western European characters only.
- Editing existing text in the document is not supported.

## Running the Windows build

Download `FolioPDF-windows.zip` from the latest GitHub Actions run (or Release), unzip it anywhere you can write (e.g. `Documents\FolioPDF`), and run `FolioPDF.exe`. No installer or admin rights needed.

To make it your default PDF app: right-click any PDF > Open with > Choose another app > browse to `FolioPDF.exe` > "Always". Windows may restrict this on managed PCs.

Windows SmartScreen will warn the first time because the exe is not code-signed ("More info" > "Run anyway"). On a work PC, application-control policies may block it entirely; check with IT.

Saved signatures are stored in `%APPDATA%\Folio\Folio PDF\signatures`.

## Building / developing

```
pip install -r requirements-dev.txt
python run.py [file.pdf]                         # run from source
QT_QPA_PLATFORM=offscreen python tests/test_gui.py out   # end-to-end test
pyinstaller folio.spec --noconfirm               # build dist/FolioPDF
```

Pushing to `main` builds the Windows zip in GitHub Actions; pushing a tag like `v0.1.0` also attaches it to a Release.

## Licensing

PyMuPDF is AGPL-3.0 (or commercial). That is fine for personal use; distributing this app to others means distributing its source under AGPL terms too. PySide6 is LGPL-3.0.
