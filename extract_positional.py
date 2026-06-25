#!/usr/bin/env python3
"""
extract_positional.py

Coordinate-aware text extraction using PyMuPDF.

Why this exists: pdftotext (and PyMuPDF's own default get_text()) emit words in
"block" order, which on some scanned pages does not match human reading order.
The result is scrambled sentences like:

    "...for herself and her As she children. struggles against..."

But the underlying word coordinates are correct. Every word in that run sits on
the same y (~352) with left-to-right increasing x. So if we ignore the block
numbering and instead sort words purely by geometry -- group into lines by
y-position, then order each line by x-position -- we recover true reading order.

This module returns text page-by-page with words in geometric reading order,
which the reflow logic in pdf_to_ereader.py can then turn into paragraphs.
"""

import fitz  # PyMuPDF


# Two words are considered on the same visual line if their vertical centers are
# within this many points of each other. Body text here is ~12pt, so half the
# line height is a safe tolerance for "same row".
LINE_Y_TOLERANCE = 6.0


def _vertical_mid(w):
    """Vertical center of a word box."""
    return (w[1] + w[3]) / 2.0


def _group_words_into_lines(words):
    """Group raw PyMuPDF words into visual lines by vertical overlap.

    words: list of tuples (x0, y0, x1, y1, text, block, line, word_no)
    Returns: list of lines, each a list of words sorted left-to-right.

    Robustness note: individual words can sit a few points above or below their
    neighbors on the same visual line (drop letters, italics, baseline jitter).
    A naive "is this word's y close to the previous word's y" walk breaks when a
    jittered word arrives out of order -- it can land on the wrong line and then
    get sorted into the wrong position. To avoid that we assign each word to a
    line by comparing its vertical *center* against each established line's
    running center, which is stable regardless of arrival order.
    """
    if not words:
        return []

    # Sort top-to-bottom by vertical center, then left-to-right. This makes the
    # grouping pass see words in true visual order before we bucket them.
    words = sorted(words, key=lambda w: (_vertical_mid(w), w[0]))

    lines = []  # each: {"mid": float, "words": [...]}
    for w in words:
        mid = _vertical_mid(w)
        placed = False
        # Try to place into the most recent line whose center is within tolerance.
        for line in reversed(lines):
            if abs(mid - line["mid"]) <= LINE_Y_TOLERANCE:
                line["words"].append(w)
                # Update running center as a simple average so a tall drop-cap
                # doesn't drag the whole line's reference point.
                n = len(line["words"])
                line["mid"] = (line["mid"] * (n - 1) + mid) / n
                placed = True
                break
        if not placed:
            lines.append({"mid": mid, "words": [w]})

    # Order lines top-to-bottom, and each line's words left-to-right.
    lines.sort(key=lambda ln: ln["mid"])
    result = []
    for line in lines:
        line["words"].sort(key=lambda w: w[0])
        result.append(line["words"])
    return result


def _line_indent(line, page_left_x):
    """Approximate leading-space count for a line, from its left x-position.

    The reflow logic keys paragraph starts off leading whitespace. We synthesize
    that by measuring how far the line's first word sits from the page's left
    text margin and converting points to an approximate space count.
    """
    if not line:
        return 0
    first_x = line[0][0]
    gap = first_x - page_left_x
    # Roughly 3pt per space at this font size; clamp negatives to 0.
    return max(0, int(gap / 3))


def extract_page_text(page, crop_top=0, crop_bottom=0):
    """Return one page's text with words in geometric reading order.

    Leading indentation is reconstructed as real leading spaces so the existing
    reflow logic (which detects paragraph starts by indent) keeps working.

    crop_top: if > 0 and < 1, treated as a fraction of page height (e.g. 0.06
              = top 6%). If >= 1, treated as absolute points.
    crop_bottom: same logic, applied to the bottom of the page.
    """
    words = page.get_text("words")
    if not words:
        return ""

    # Crop by y-position: drop headers at top and footnotes at bottom.
    if crop_top or crop_bottom:
        page_height = page.rect.height
        # Convert fractions to absolute points.
        top_px = crop_top * page_height if 0 < crop_top < 1 else crop_top
        bot_px = crop_bottom * page_height if 0 < crop_bottom < 1 else crop_bottom
        words = [w for w in words
                 if w[1] >= top_px and w[3] <= (page_height - bot_px)]

    if not words:
        return ""

    # The page's left text margin = smallest x0 among all words on the page.
    page_left_x = min(w[0] for w in words)

    lines = _group_words_into_lines(words)

    out_lines = []
    for line in lines:
        indent = _line_indent(line, page_left_x)
        text = " ".join(w[4] for w in line)
        out_lines.append(" " * indent + text)

    return "\n".join(out_lines)


def extract_document(pdf_path, crop_top=0, crop_bottom=0):
    """Yield (page_number, page_text) for every page, in reading order."""
    doc = fitz.open(pdf_path)
    for pno in range(len(doc)):
        yield pno, extract_page_text(doc[pno], crop_top=crop_top, crop_bottom=crop_bottom)
    doc.close()


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    # Quick smoke test: dump the first few pages.
    for pno, text in extract_document(path):
        if pno > 3:
            break
        print(f"===== PAGE {pno} =====")
        print(text[:1200])
        print()
