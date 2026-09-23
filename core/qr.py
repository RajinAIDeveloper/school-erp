"""
QR codes for printed documents, from ReportLab's own encoder so no extra dependency is needed.

A report card or certificate carries a QR code pointing at its verification page. The same
matrix is drawn two ways: as a ReportLab drawing for PDFs, and as inline SVG for the pages a
school prints from the browser (which is how Bangla documents are printed, because the
browser joins Bangla letters correctly and the PDF engine does not).
"""

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm


def _matrix(text):
    widget = QrCodeWidget(text)
    widget.getBounds()  # forces the encoder to choose a version and build the matrix
    qr = widget.qr
    size = qr.getModuleCount()
    return [[qr.isDark(row, col) for col in range(size)] for row in range(size)]


def qr_svg(text, size=96, label="Scan to verify"):
    """An inline SVG QR code, `size` pixels square, with a quiet zone of four modules."""
    matrix = _matrix(text)
    count = len(matrix)
    quiet = 4
    total = count + 2 * quiet
    cells = []
    for y, row in enumerate(matrix):
        run_start = None
        for x, dark in enumerate(row + [False]):
            if dark and run_start is None:
                run_start = x
            elif not dark and run_start is not None:
                cells.append(f"M{run_start + quiet} {y + quiet}h{x - run_start}v1h-{x - run_start}z")
                run_start = None
    path = "".join(cells)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {total} {total}" '
        f'role="img" aria-label="{label}" shape-rendering="crispEdges">'
        f'<rect width="{total}" height="{total}" fill="#fff"/><path d="{path}" fill="#000"/></svg>'
    )


def qr_drawing(text, size_mm=24):
    """A ReportLab drawing of the QR code, `size_mm` square."""
    widget = QrCodeWidget(text)
    left, bottom, right, top = widget.getBounds()
    size = size_mm * mm
    drawing = Drawing(size, size, transform=[size / (right - left), 0, 0, size / (top - bottom), 0, 0])
    drawing.add(widget)
    return drawing
