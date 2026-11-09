"""Extract page text with word positions from PDFs, falling back to OCR."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
from dataclasses import dataclass

import pymupdf

from medchron.ingestion.models import BBox, Page, TextSource, Word

log = logging.getLogger(__name__)

MIN_TEXT_WORDS = 5  # fewer words than this on a page means "probably a scan"
OCR_DPI = 200


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


def _ocr_words(page: pymupdf.Page) -> list[RawWord]:
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--psm 4")
    scale = 72.0 / OCR_DPI  # pixels -> PDF points
    words: list[RawWord] = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        x, y, w, h = (data[k][i] * scale for k in ("left", "top", "width", "height"))
        key = (data["block_num"][i] * 1000 + data["par_num"][i], data["line_num"][i])
        words.append(RawWord(text, x, y, x + w, y + h, key, conf))
    return words


def extract_pages(pdf_bytes: bytes, *, ocr: bool = True) -> list[Page]:
    pages: list[Page] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for i, page in enumerate(doc, start=1):
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
                )
            )
    return pages
