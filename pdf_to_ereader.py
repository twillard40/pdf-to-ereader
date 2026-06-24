#!/usr/bin/env python3
"""
pdf_to_ereader.py

Convert a text-layer PDF (typically an Internet Archive scan with an OCR text
layer) into a clean, reflowable PDF sized for an e-reader.

The hard part of this is NOT extraction -- pdftotext already pulls the words out.
The hard part is reconstructing paragraphs and lines, because the raw extraction:

  1. Wraps every visual line with a newline, even mid-sentence.
  2. Hyphenates words across line breaks ("judg-\nments" -> "judgments").
  3. Marks paragraph starts only with leading indentation (in -layout mode).
  4. Scatters junk: mangled em-dashes, form-feed control chars, stray spaces
     inside words ("Maximiliane' s"), and OCR misreads ("v/ould").

This script targets problems 1-3 reliably and takes a conservative pass at 4.
It will not be perfect on every book. It is meant to get you to "comfortably
readable" and to flag what it could not confidently fix.

Usage:
    python pdf_to_ereader.py input.pdf output.pdf
    python pdf_to_ereader.py input.pdf output.pdf --font-size 13 --report
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


# ---------------------------------------------------------------------------
# Step 1: get the raw text out of the PDF, preserving layout (indentation).
# ---------------------------------------------------------------------------

def extract_layout_text(pdf_path):
    """Run pdftotext -layout and return the raw string.

    -layout keeps leading indentation, which is our single most reliable
    signal for where paragraphs begin.
    """
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr}")
    return result.stdout


# ---------------------------------------------------------------------------
# Step 2: scrub junk characters that the OCR layer leaves behind.
# ---------------------------------------------------------------------------

# How indented a line must be (in leading spaces) to count as a paragraph start.
# From inspecting this book, real paragraph starts had 4-7 leading spaces while
# wrapped continuation lines had 0. We use a threshold of 2 to be safe.
PARA_INDENT_THRESHOLD = 2

# Junk fixes applied to the *whole text* before line processing.
def clean_raw_text(text):
    """Remove control characters and normalize obvious junk."""
    # Form feed (page break marker) -> we handle pages separately, so turn the
    # raw \f into a sentinel we can split on, then strip stray ^L bytes.
    text = text.replace("\x0c", "\n<<<PAGEBREAK>>>\n")
    # Lone form-feed / vertical-tab style control chars sometimes survive.
    text = text.replace("\x0b", " ")
    # Mangled em-dash bytes: pdftotext sometimes emits the literal UTF-8 for an
    # em-dash on its own line as a floating dash. Normalize real em-dashes.
    text = text.replace("\u2014", "—").replace("\u2013", "–")
    # OCR routinely mangles curly quotes. The scanner reads a closing curly
    # double-quote as two apostrophes ('') and sometimes as a slash+apostrophe
    # (/' or /). Normalize the common garbled forms to straight quotes.
    text = text.replace("''", '"').replace("``", '"')
    text = text.replace("/'", '"').replace("/\"", '"')
    # A slash immediately after a letter at a word boundary is almost always a
    # misread closing quote ("doll/" -> 'doll"').
    text = re.sub(r"([a-z])/(?=[\s.,;])", r'\1"', text)
    return text


# Common in-word junk. These are conservative: only patterns that are almost
# never legitimate English.
def fix_inword_artifacts(line):
    """Fix stray spaces and obvious misreads inside a line of text."""
    # Possessive split: "Maximiliane' s" -> "Maximiliane's"
    line = re.sub(r"(\w)'\s+s\b", r"\1's", line)
    # Space before a possessive/contraction apostrophe: "word 's" -> "word's"
    line = re.sub(r"(\w)\s+'(\w)", r"\1'\2", line)
    # Collapse 2+ internal spaces to one.
    line = re.sub(r"[ \t]{2,}", " ", line)
    # Stray space before colon/semicolon: "face : chubby" -> "face: chubby"
    line = re.sub(r'\s+([;:])', r'\1', line)
    return line


# ---------------------------------------------------------------------------
# Step 3: rebuild paragraphs from the wrapped, indented lines.
# ---------------------------------------------------------------------------

def leading_spaces(s):
    """Count leading spaces on a raw (not yet stripped) line."""
    return len(s) - len(s.lstrip(" "))


def is_page_number(stripped):
    """A line that is just digits or Roman numerals (a page number) we drop."""
    if stripped.isdigit() and len(stripped) <= 4:
        return True
    # Roman numeral page numbers: i, ii, vii, xiv, etc.
    if re.fullmatch(r'[ivxlcdm]+', stripped.lower()) and len(stripped) <= 6:
        return True
    return False


def is_floating_dash(stripped):
    """A line containing only a dash is an extraction artifact -> drop it."""
    return stripped in {"—", "–", "-", "....", "...."}


def join_wrapped(prev, addition):
    """Join a continuation line onto the running paragraph.

    Handles end-of-line hyphenation: if the previous text ends with a hyphen,
    we assume the word was split across the line break and glue it back without
    a space ("judg-" + "ments" -> "judgments"). Otherwise we join with a space.
    """
    if prev.endswith("-"):
        # Remove the trailing hyphen and glue directly.
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

        # Blank line -> end of paragraph.
        if not stripped:
            if current:
                paragraphs.append(current)
                current = ""
            continue

        # Drop page numbers and floating-dash artifacts.
        if is_page_number(stripped) or is_floating_dash(stripped):
            continue

        stripped = fix_inword_artifacts(stripped)

        # Indented line -> start a new paragraph (flush the old one first).
        if indent >= PARA_INDENT_THRESHOLD:
            if current:
                paragraphs.append(current)
            current = stripped
        else:
            # Continuation of the current paragraph.
            current = join_wrapped(current, stripped)

    if current:
        paragraphs.append(current)

    return paragraphs


# ---------------------------------------------------------------------------
# Step 3b: merge paragraphs that were falsely broken mid-sentence.
# ---------------------------------------------------------------------------

# A paragraph that genuinely ends should close on sentence-final punctuation
# (. ? !), optionally followed by a closing quote, or a colon/semicolon. If it
# ends on anything else -- a bare word, a comma, an open clause -- the break was
# almost certainly a layout artifact and the next paragraph continues it.
_SENTENCE_END = re.compile(r"""[.?!]['"\u2019\u201d]?$|[:;]$""")


def _ends_a_sentence(text):
    return bool(_SENTENCE_END.search(text.rstrip()))


def _looks_like_continuation_start(text):
    """The next paragraph continues the previous if it starts lowercase.

    A capital letter or an opening quote at the start usually signals a real new
    paragraph (new sentence, new line of dialogue), so we DON'T merge those --
    being conservative here avoids gluing separate dialogue turns together.
    """
    stripped = text.lstrip()
    if not stripped:
        return False
    return stripped[0].islower()


def merge_broken_sentences(paragraphs):
    """Join consecutive paragraphs where the first ends mid-sentence and the
    second clearly continues it (starts lowercase).
    """
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
            # Glue with hyphen-awareness, same as wrapped-line joining.
            merged[-1] = join_wrapped(prev, nxt.lstrip())
        else:
            merged.append(nxt)

    return merged


# ---------------------------------------------------------------------------
# Step 4: flag paragraphs that look suspicious so you can review them.
# ---------------------------------------------------------------------------

# Patterns that suggest leftover OCR junk we did not fix automatically.
# Kept deliberately tight: a flag that fires on normal English is useless.
SUSPECT_PATTERNS = [
    (re.compile(r"[a-z]/[a-z]"), "slash inside word (e.g. v/ould)"),
    # Three+ non-ascii junk chars clustered = garbled scan line, not a stray accent.
    (re.compile(r"[^\x00-\x7f]{3,}"), "cluster of non-ascii junk"),
    # A digit jammed against letters mid-word (not "1945" or "N.Y."): "ap1roaching".
    (re.compile(r"[a-z]\d[a-z]"), "digit inside word"),
    # Leftover doubled/garbled quote artifacts.
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
                break  # one flag per paragraph is enough
    return suspects


def render_cover_image(pdf_path, page_index, out_png, zoom=2.0):
    """Rasterize one PDF page to a PNG to use as a cover.

    We render the whole page (cover art and all) rather than extracting the
    embedded image, because the page may have multiple images plus positioning;
    rendering captures exactly what the page looks like. zoom=2.0 gives a crisp
    result without an enormous file.
    """
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


# ---------------------------------------------------------------------------
# Step 4b: detect and strip running headers (e.g. "THE LAST CIVILIAN",
# "Introduction") that appear at the top of many pages.
# ---------------------------------------------------------------------------

def detect_running_headers(pdf_path, skip_pages=0):
    """Scan the document for text that repeats near the top of many pages.

    Returns a set of header strings to strip. A phrase counts as a running
    header if it appears near the top of at least 20% of pages.
    """
    from collections import Counter
    top_line_counts = Counter()
    total_pages = 0

    for pno, page_text in extract_positional.extract_document(pdf_path):
        if pno < skip_pages:
            continue
        total_pages += 1
        lines = page_text.strip().split("\n")
        # Look at the first 3 non-empty, short lines — headers live here.
        checked = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Headers are short — skip anything that looks like body text.
            if len(stripped) > 60:
                break
            # Skip pure page numbers — we already handle those.
            if is_page_number(stripped):
                checked += 1
                if checked >= 3:
                    break
                continue
            top_line_counts[stripped] += 1
            checked += 1
            if checked >= 3:
                break

    # A header repeats on at least 20% of pages.
    threshold = max(3, total_pages * 0.2)
    headers = set()
    for text, count in top_line_counts.items():
        if count >= threshold:
            headers.add(text)

    return headers


def strip_headers_from_page(page_text, headers):
    """Remove lines that match detected running headers from a page's text.

    Also handles the case where a header appears inline (merged into a line
    with surrounding text, e.g. "surround- 4 THE LAST CIVILIAN ing villages").
    """
    if not headers:
        return page_text

    cleaned_lines = []
    for line in page_text.split("\n"):
        stripped = line.strip()
        # If the whole line is a known header, drop it.
        if stripped in headers:
            continue
        # Check if a header + page number is embedded inside the line.
        for header in headers:
            escaped = re.escape(header)
            # Match: optional page number before the header, anywhere in line
            line = re.sub(r'\s*\d{1,4}\s+' + escaped + r'\s*', ' ', line)
            # Match: header at start of line followed by text
            line = re.sub(r'^' + escaped + r'\s+', '', line)
            # Match: header embedded mid-line
            line = re.sub(r'\s+' + escaped + r'\s+', ' ', line)
            # Match: header at end of line
            line = re.sub(r'\s+' + escaped + r'$', '', line)
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


# ---------------------------------------------------------------------------
# Step 5: render the cleaned paragraphs to a fresh PDF.
# ---------------------------------------------------------------------------

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

    # If we have a cover, place it first, scaled to fit the printable area,
    # then a page break so the text starts on a fresh page.
    if cover_png and os.path.exists(cover_png):
        from PIL import Image as PILImage
        # Leave a little extra headroom below the nominal margins so the image
        # never overflows the frame (reportlab's usable frame is slightly
        # smaller than page minus margins).
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


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def convert(pdf_path, output_path, font_size=12, report=False, backend="positional",
            skip_pages=0, cover_page=None):
    print(f"Extracting text from {pdf_path} (backend: {backend}) ...")
    if skip_pages:
        print(f"Skipping the first {skip_pages} page(s) of front matter.")

    # Render the cover page first, if requested, so it survives the text skip.
    cover_png = None
    if cover_page is not None:
        cover_png = render_cover_image(pdf_path, cover_page, "/tmp/_cover.png")
        if cover_png:
            print(f"Rendered cover from page {cover_page}.")
        else:
            print(f"Could not render cover page {cover_page}; skipping cover.")

    all_paragraphs = []

    if backend == "positional":
        # First pass: detect running headers across the document.
        headers = detect_running_headers(pdf_path, skip_pages=skip_pages)
        if headers:
            print(f"Detected running headers: {headers}")

        # Second pass: extract, strip headers, and reflow.
        page_count = 0
        for pno, page_text in extract_positional.extract_document(pdf_path):
            if pno < skip_pages:
                continue
            page_count += 1
            page_text = clean_raw_text(page_text)
            page_text = page_text.replace("<<<PAGEBREAK>>>", "")
            page_text = strip_headers_from_page(page_text, headers)
            if page_text.strip():
                all_paragraphs.extend(reflow_page(page_text))
        print(f"Processed {page_count} pages.")
    else:
        # Legacy pdftotext backend (faster, but can scramble reading order).
        raw = extract_layout_text(pdf_path)
        raw = clean_raw_text(raw)
        pages = raw.split("<<<PAGEBREAK>>>")
        pages = pages[skip_pages:]
        print(f"Found {len(pages)} pages. Reflowing ...")
        for page in pages:
            if page.strip():
                all_paragraphs.extend(reflow_page(page))

    print(f"Reconstructed {len(all_paragraphs)} paragraphs.")

    # Merge paragraphs that were falsely split mid-sentence by layout artifacts.
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
                        help="extraction backend: 'positional' (coordinate-aware, fixes reading order) "
                             "or 'pdftotext' (faster legacy)")
    parser.add_argument("--skip-pages", type=int, default=0,
                        help="number of leading pages to drop (book jacket, title, copyright)")
    parser.add_argument("--cover-page", type=int, default=None,
                        help="page index (0-based) to render as a cover image and keep at the front")
    args = parser.parse_args()

    convert(args.input, args.output, font_size=args.font_size, report=args.report,
            backend=args.backend, skip_pages=args.skip_pages, cover_page=args.cover_page)


if __name__ == "__main__":
    main()