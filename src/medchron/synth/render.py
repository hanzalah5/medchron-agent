"""Render laid-out pages to a PDF.

Text pages get a real text layer. "Scanned" pages are rasterized to a slightly
rotated, speckled grayscale image with no text layer, so the ingestion
pipeline has to OCR them. Output is byte-for-byte deterministic for a given
seed and library version.
"""

from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from medchron.synth.layout import LaidPage

FONT = "Courier"
FONT_SIZE = 9
LEADING = 11
MARGIN_X = 54
MARGIN_TOP = 40
SCAN_DPI = 100


def _scan_image(page: LaidPage, rng: random.Random) -> Image.Image:
    width, height = int(8.5 * SCAN_DPI), int(11 * SCAN_DPI)
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=12)
    scale = SCAN_DPI / 72
    y = MARGIN_TOP * scale
    for line in page.text.splitlines():
        draw.text((MARGIN_X * scale, y), line, fill=rng.randint(0, 60), font=font)
        y += LEADING * scale
    for _ in range(900):  # scanner speckle
        x, yy = rng.randrange(width), rng.randrange(height)
        draw.point((x, yy), fill=rng.randint(0, 140))
    angle = rng.uniform(-1.2, 1.2)
    return img.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=255)


def render_pdf(pages: list[LaidPage], seed: int) -> bytes:
    rng = random.Random(seed ^ 0x5CA9)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1, pageCompression=1)
    c.setTitle("Synthetic medical record production (not a real patient)")
    c.setAuthor("medchron synthetic generator")
    page_w, page_h = letter
    for page in pages:
        if page.text_layer:
            c.setFont(FONT, FONT_SIZE)
            y = page_h - MARGIN_TOP
            for line in page.text.splitlines():
                c.drawString(MARGIN_X, y, line)
                y -= LEADING
        else:
            png = io.BytesIO()
            _scan_image(page, rng).save(png, format="PNG", optimize=True)
            png.seek(0)
            c.drawImage(ImageReader(png), 0, 0, width=page_w, height=page_h)
        c.showPage()
    c.save()
    return buf.getvalue()
