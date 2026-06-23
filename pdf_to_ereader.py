#!/usr/bin/env python3
"""
pdf_to_ereader.py

Convert a text-layer PDF (typically an Internet Archive scan with an OCR text
layer) into a clean, reflowable PDF sized for an e-reader.
"""

import argparse
import os
import re
import subprocess
import sys
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, PageBreak, Image, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT

import extract_positional
import fitz  # PyMuPDF, for rasterizing the cover page


def extract_layout_text(pdf_path):
    """Run pdftotext -layout and return the raw string."""
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr}")
    return result.stdout


PARA_INDENT_THRESHOLD = 2


def clean_raw_text(text):
    """Remove control characters and normalize obvious junk."""
    text = text.replace("\x0c", "\n<<<PAGEBREAK>>>\n")
    text = text.replace("\x0b", " ")
    text = text.replace("\u2014", "—").replace("\u2013", "–")
    text = text.replace("''", '"').replace("``", '"')
    text = text.replace("/'", '"').replace("/\"", '"')
    text = re.sub(r"([a-z])/(?=[\s.,;])", r'\1"', text)
    return text


def fix_inword_artifacts(line):
    """Fix stray spaces and obvious misreads inside a line of text."""
    line = re.sub(r"(\w)'\s+s\b", r"\1's", line)
    line = re.sub(r"(\w)\s+'(\w)", r"\1'\2", line)
    line = re.sub(r"[ \t]{2,}", " ", line)
    return line


def leading_spaces(s):
    """Count leading spaces on a raw (not yet stripped) line."""
    return len(s) - len(s.lstrip(" "))


def is_page_number(stripped):
    """A line that is just digits (a page number) we drop from body text."""
    return stripped.isdigit() and len(stripped) <= 4


def is_floating_dash(stripped):
    """A line containing only a dash is an extraction artifact -> drop it."""
    return stripped in {"—", "–", "-", "....", "...."}


def join_wrapped(prev, addition):
    """Join a continuation line onto the running paragraph."""
    if prev.endswith("-"):
        return prev[:-1] + addition
    if not prev:
        return addition
    return prev + " " + addition


def reflow_page(page_text):
    """Turn one page of wrapped/indented lines into a list of paragraph strings."""
    paragraphs = []
    current = ""

    for raw_line in page_text.split("\n"):
        indent = leading_spaces(raw_line)
        stripped = raw_line.strip()

        if not stripped:
            if current:
                paragraphs.append(current)
                current = ""
            continue

        if is_page_number(stripped) or is_floating_dash(stripped):
            continue

        stripped = fix_inword_artifacts(stripped)

        if indent >= PARA_INDENT_THRESHOLD:
            if current:
                paragraphs.append(current)
            current = stripped
        else:
            current = join_wrapped(current, stripped)

    if current:
        paragraphs.append(current)

    return paragraphs


_SENTENCE_END = re.compile(r"""[.?!]['"\u2019\u201d]?$|[:;]$""")


def _ends_a_sentence(text):
    return bool(_SENTENCE_END.search(text.rstrip()))


def _looks_like_continuation_start(text):
    """The next paragraph continues the previous if it starts lowercase."""
    stripped = text.lstrip()
    if not stripped:
        return False
    return stripped[0].islower()


def merge_broken_sentences(paragraphs):
    """Join paragraphs where the first ends mid-sentence and the second continues it."""
    if not paragraphs:
        return paragraphs

    merged = [paragraphs[0]]
    for nxt in paragraphs[1:]:
        prev = merged[-1]
        if prev == "<<<PAGEBREAK>>>" or nxt == "<<<PAGEBREAK>>>":
            merged.append(nxt)
            continue

        prev_open = not _ends_a_sentence(prev)
        nxt_continues = _looks_like_continuation_start(nxt)

        if prev_open and nxt_continues:
            merged[-1] = join_wrapped(prev, nxt.lstrip())
        else:
            merged.append(nxt)

    return merged


SUSPECT_PATTERNS = [
    (re.compile(r"[a-z]/[a-z]"), "slash inside word (e.g. v/ould)"),
    (re.compile(r"[^\x00-\x7f]{3,}"), "cluster of non-ascii junk"),
    (re.compile(r"[a-z]\d[a-z]"), "digit inside word"),
    (re.compile(r"['`]{2,}"), "doubled apostrophes (garbled quote)"),
]


def find_suspects(paragraphs):
    """Return a list of (paragraph_index, reason, snippet) for review."""
    suspects = []
    for i, para in enumerate(paragraphs):
        for pattern, reason in SUSPECT_PATTERNS:
            m = pattern.search(para)
            if m:
                start = max(0, m.start() - 20)
                end = min(len(para), m.end() + 20)
                snippet = para[start:end].replace("\n", " ")
                suspects.append((i, reason, snippet))
                break
    return suspects


def render_cover_image(pdf_path, page_index, out_png, zoom=2.0):
    """Rasterize one PDF page to a PNG to use as a cover."""
    doc = fitz.open(pdf_path)
    if page_index >= len(doc):
        doc.close()
        return None
    page = doc[page_index]
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix)
    pix.save(out_png)
    doc.close()
    return out_png


def build_pdf(paragraphs, output_path, font_size=12, cover_png=None):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=font_size,
        leading=font_size + 4,
        textColor="#000000",
        alignment=TA_LEFT,
        spaceAfter=10,
        firstLineIndent=0,
    )

    story = []

    if cover_png and os.path.exists(cover_png):
        from PIL import Image as PILImage
        avail_w = letter[0] - 1.5 * inch - 6
        avail_h = letter[1] - 1.5 * inch - 18
        with PILImage.open(cover_png) as im:
            iw, ih = im.size
        scale = min(avail_w / iw, avail_h / ih)
        story.append(Image(cover_png, width=iw * scale, height=ih * scale))
        story.append(PageBreak())

    for para in paragraphs:
        if para == "<<<PAGEBREAK>>>":
            story.append(PageBreak())
            continue
        story.append(Paragraph(escape(para), body))

    doc.build(story)


def convert(pdf_path, output_path, font_size=12, report=False, backend="positional",
            skip_pages=0, cover_page=None):
    print(f"Extracting text from {pdf_path} (backend: {backend}) ...")
    if skip_pages:
        print(f"Skipping the first {skip_pages} page(s) of front matter.")

    cover_png = None
    if cover_page is not None:
        cover_png = render_cover_image(pdf_path, cover_page, "/tmp/_cover.png")
        if cover_png:
            print(f"Rendered cover from page {cover_page}.")
        else:
            print(f"Could not render cover page {cover_page}; skipping cover.")

    all_paragraphs = []

    if backend == "positional":
        page_count = 0
        for pno, page_text in extract_positional.extract_document(pdf_path):
            if pno < skip_pages:
                continue
            page_count += 1
            page_text = clean_raw_text(page_text)
            page_text = page_text.replace("<<<PAGEBREAK>>>", "")
            if page_text.strip():
                all_paragraphs.extend(reflow_page(page_text))
        print(f"Processed {page_count} pages.")
    else:
        raw = extract_layout_text(pdf_path)
        raw = clean_raw_text(raw)
        pages = raw.split("<<<PAGEBREAK>>>")
        pages = pages[skip_pages:]
        print(f"Found {len(pages)} pages. Reflowing ...")
        for page in pages:
            if page.strip():
                all_paragraphs.extend(reflow_page(page))

    print(f"Reconstructed {len(all_paragraphs)} paragraphs.")

    before = len(all_paragraphs)
    all_paragraphs = merge_broken_sentences(all_paragraphs)
    print(f"Merged {before - len(all_paragraphs)} mid-sentence breaks "
          f"-> {len(all_paragraphs)} paragraphs.")

    if report:
        suspects = find_suspects(all_paragraphs)
        print(f"\n{len(suspects)} paragraphs flagged for review:")
        for idx, reason, snippet in suspects[:40]:
            print(f"  [#{idx}] {reason}: ...{snippet}...")
        if len(suspects) > 40:
            print(f"  ... and {len(suspects) - 40} more")

    print(f"\nBuilding PDF -> {output_path}")
    build_pdf(all_paragraphs, output_path, font_size=font_size, cover_png=cover_png)
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Clean a scanned-PDF text layer into a reflowable e-reader PDF.")
    parser.add_argument("input", help="input PDF path")
    parser.add_argument("output", help="output PDF path")
    parser.add_argument("--font-size", type=int, default=12, help="body font size in points (default 12)")
    parser.add_argument("--report", action="store_true", help="print paragraphs flagged for manual review")
    parser.add_argument("--backend", choices=["positional", "pdftotext"], default="positional",
                        help="extraction backend: 'positional' (coordinate-aware) or 'pdftotext' (legacy)")
    parser.add_argument("--skip-pages", type=int, default=0,
                        help="number of leading pages to drop (book jacket, title, copyright)")
    parser.add_argument("--cover-page", type=int, default=None,
                        help="page index (0-based) to render as a cover image and keep at the front")
    args = parser.parse_args()

    convert(args.input, args.output, font_size=args.font_size, report=args.report,
            backend=args.backend, skip_pages=args.skip_pages, cover_page=args.cover_page)


if __name__ == "__main__":
    main()