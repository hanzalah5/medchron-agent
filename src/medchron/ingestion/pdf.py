"""Extract page text with word positions from PDFs, falling back to OCR.

Scanned pages are often rotated (fed sideways or upside down) or skewed. Before
OCR, the page image is turned upright using Tesseract's orientation detection
and straightened with a projection-profile deskew. Word boxes are then mapped
back to the original page, so highlights line up with what the reviewer sees.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
from dataclasses import dataclass

import numpy as np
import pymupdf
from PIL import Image

from medchron.ingestion.models import BBox, Page, TextSource, Word

log = logging.getLogger(__name__)

MIN_TEXT_WORDS = 5  # fewer words than this on a page means "probably a scan"
OCR_DPI = 200
MAX_SKEW_DEG = 6.0
MIN_SKEW_DEG = 0.5  # straighter than this is left alone


@dataclass(frozen=True)
class RawWord:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line_key: tuple[int, int]
    conf: float | None = None


def normalized_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()


def assemble_text(words: list[RawWord]) -> tuple[str, list[Word]]:
    """Join words into page text: spaces within a line, newlines between lines.

    Building the text ourselves (instead of using the PDF library's text
    output) guarantees every word's character offsets match its bounding box.
    """
    parts: list[str] = []
    out: list[Word] = []
    pos = 0
    prev_line: tuple[int, int] | None = None
    prev_y1 = 0.0
    for w in words:
        if prev_line is not None:
            if w.line_key == prev_line:
                sep = " "
            else:
                # A vertical jump of more than ~1.5 line heights marks a paragraph break.
                gap = w.y0 - prev_y1
                sep = "\n\n" if gap > 1.2 * (w.y1 - w.y0) else "\n"
            parts.append(sep)
            pos += len(sep)
        parts.append(w.text)
        out.append(
            Word(
                text=w.text,
                bbox=BBox(x0=w.x0, y0=w.y0, x1=w.x1, y1=w.y1),
                char_start=pos,
                char_end=pos + len(w.text),
            )
        )
        pos += len(w.text)
        prev_line, prev_y1 = w.line_key, w.y1
    return "".join(parts), out


def _text_layer_words(page: pymupdf.Page) -> list[RawWord]:
    rows = page.get_text("words", sort=True)
    return [
        RawWord(str(r[4]), float(r[0]), float(r[1]), float(r[2]), float(r[3]), (r[5], r[6]))
        for r in rows
    ]


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def detect_rotation(img: Image.Image) -> int:
    """Counter-clockwise degrees (0, 90, 180, 270) the text is rotated by.

    Uses Tesseract orientation detection. Pages with too little text for it
    to decide are treated as upright.
    """
    import pytesseract

    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:
        return 0
    rotate = int(osd.get("rotate", 0)) % 360
    return rotate if rotate in (90, 180, 270) else 0


def _profile_score(img: Image.Image, angle: float) -> float:
    rotated = img.rotate(angle, resample=Image.Resampling.NEAREST, fillcolor=255)
    profile = (np.asarray(rotated) < 128).sum(axis=1).astype(float)
    return float(np.var(profile))


def estimate_skew(img: Image.Image, max_angle: float = MAX_SKEW_DEG) -> float:
    """Angle (degrees, counter-clockwise) that straightens the text lines.

    Projection profile: rows of text give the sharpest row-sum histogram when
    lines are horizontal. Coarse 0.25-degree search, then 0.05-degree refinement.
    """
    small = img.copy()
    small.thumbnail((600, 600))
    steps = int(max_angle / 0.25)
    coarse = max(
        (i * 0.25 for i in range(-steps, steps + 1)), key=lambda a: _profile_score(small, a)
    )
    return max((coarse + i * 0.05 for i in range(-5, 6)), key=lambda a: _profile_score(small, a))


def _rotate_point(x: float, y: float, cx: float, cy: float, deg: float) -> tuple[float, float]:
    """Where the point (x, y) of an image ends up after ``img.rotate(deg)`` (no expand)."""
    t = np.radians(deg)
    dx, dy = x - cx, y - cy
    return cx + dx * np.cos(t) + dy * np.sin(t), cy - dx * np.sin(t) + dy * np.cos(t)


def _unrotate_quarter(x: float, y: float, w: float, h: float, rot: int) -> tuple[float, float]:
    """Map a point of the upright image back to the original, rotated scan.

    ``rot`` is the counter-clockwise rotation of the text in the original (w x h).
    The upright image was made with ``original.rotate(-rot, expand=True)``.
    """
    if rot == 90:  # upright is h x w; original text was turned CCW
        return w - y, x
    if rot == 180:
        return w - x, h - y
    if rot == 270:
        return y, h - x
    return x, y


def _ocr_words(page: pymupdf.Page) -> list[RawWord]:
    import pytesseract

    pix = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY)
    original = Image.open(io.BytesIO(pix.tobytes("png")))
    w, h = original.size
    rot = detect_rotation(original)
    upright = original.rotate(-rot, expand=True, fillcolor=255) if rot else original
    skew = estimate_skew(upright)
    if abs(skew) < MIN_SKEW_DEG:
        skew = 0.0
    straight = (
        upright.rotate(skew, resample=Image.Resampling.BILINEAR, fillcolor=255) if skew else upright
    )
    cx, cy = upright.size[0] / 2, upright.size[1] / 2
    data = pytesseract.image_to_data(
        straight, output_type=pytesseract.Output.DICT, config="--psm 4"
    )
    scale = 72.0 / OCR_DPI  # pixels -> PDF points
    words: list[RawWord] = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        left, top = float(data["left"][i]), float(data["top"][i])
        right, bottom = left + float(data["width"][i]), top + float(data["height"][i])
        corners = []
        for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):
            if skew:  # undo the deskew rotation
                x, y = _rotate_point(x, y, cx, cy, -skew)
            corners.append(_unrotate_quarter(x, y, w, h, rot))
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        key = (data["block_num"][i] * 1000 + data["par_num"][i], data["line_num"][i])
        words.append(
            RawWord(
                text,
                max(0.0, min(xs)) * scale,
                max(0.0, min(ys)) * scale,
                min(float(w), max(xs)) * scale,
                min(float(h), max(ys)) * scale,
                key,
                conf,
            )
        )
    return words


def extract_pages(
    pdf_bytes: bytes, *, ocr: bool = True, first_page_no: int = 1, production: int = 1
) -> list[Page]:
    """Pages of one PDF. ``first_page_no`` continues numbering across productions."""
    pages: list[Page] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for i, page in enumerate(doc, start=first_page_no):
            words = _text_layer_words(page)
            source = TextSource.TEXT_LAYER
            conf: float | None = None
            if len(words) < MIN_TEXT_WORDS:
                if ocr and ocr_available():
                    words = _ocr_words(page)
                    source = TextSource.OCR if words else TextSource.NONE
                    confs = [w.conf for w in words if w.conf is not None]
                    conf = round(sum(confs) / len(confs), 1) if confs else None
                else:
                    if ocr:
                        log.warning("page %d needs OCR but tesseract is not installed", i)
                    source = TextSource.NONE if not words else source
            text, positioned = assemble_text(words)
            pages.append(
                Page(
                    page_no=i,
                    text=text,
                    words=positioned,
                    source=source,
                    ocr_confidence=conf,
                    width=page.rect.width,
                    height=page.rect.height,
                    content_hash=normalized_hash(text),
                    production=production,
                )
            )
    return pages
