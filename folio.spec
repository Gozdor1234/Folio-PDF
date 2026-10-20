# PyInstaller spec: builds a folder-style (onedir) app. Onedir starts much faster
# than a single-file exe, which would unpack itself to %TEMP% on every launch.
import sys

block_cipher = None

QT_EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DAnimation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtGraphs", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtWebSockets", "PySide6.QtHttpServer", "PySide6.QtSvgWidgets", "PySide6.QtUiTools",
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/folio.ico", "assets"), ("assets/folio.png", "assets")],
    hiddenimports=[],
    excludes=QT_EXCLUDES + ["tkinter", "matplotlib", "PIL", "reportlab", "pytest", "cv2", "pandas", "scipy",
                             "onnxruntime", "lxml", "IPython", "fontTools"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FolioPDF",
    console=False,
    icon="assets/folio.ico",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="FolioPDF")
