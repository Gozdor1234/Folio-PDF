import os
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import APP_NAME, APP_VERSION
from .main_window import MainWindow


def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", name)


def main():
    QCoreApplication.setOrganizationName("Folio")
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(APP_VERSION)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    icon = resource("folio.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    win = MainWindow()
    win.show()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        win.open_path(args[0])
    sys.exit(app.exec())
