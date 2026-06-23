# pdf-to-ereader

Turn scanned-book PDFs (the kind from Internet Archive, with a messy OCR text
layer) into clean, reflowable PDFs that read well on an e-reader.

Most scanned books are miserable on a Boox or Kindle: the text is locked to the
original page geometry, the background is yellowed, and naive text extraction
produces scrambled, mid-sentence-broken garbage. This tool reconstructs the
actual prose and re-renders it as plain, reflowable text at a readable size.

## Setup and Use
Move pdf to pdf-to-ereader folder on computer. 
Use venv, since you have many other projects.

### Use

1. Run the following to convert to clean PDF

```Bash
python pdf_to_ereader.py "whatever.pdf" output.pdf
```
2. Run this command to convert the clean pdf to epub.

```
ebook-convert "The Visoko Chronicle - Ivan Tavcar.pdf" "The Visoko Chronicle.epub"
```

## Before / after

The input is a scanned PDF whose OCR text layer, when extracted with standard
tools, comes out like this:

> ...build a new life on the ruins of the old for herself and her **As she**
> **children. struggles** against the devastation of war...

The output reads the way the book actually reads:

> ...build a new life on the ruins of the old for herself and her **children.**
> **As she struggles** against the devastation of war...

## Why this is harder than it looks

The obvious approach -- run `pdftotext`, dump the result into a new PDF --
fails for three distinct reasons. Each one took a separate fix.

### 1. Reading order is scrambled

`pdftotext` (and PyMuPDF's default `get_text()`) emit words in the PDF's
internal *block* order, which on many scanned pages does not match human
reading order. Words from different parts of a line get interleaved, producing
sentences that are individually misordered even though every word is present.

The underlying word coordinates, however, are correct. The fix is to ignore the
block numbering entirely and reconstruct reading order from geometry: extract
every word with its (x, y) position, group words into visual lines by their
vertical position, then order each line left-to-right by horizontal position.
This is what `extract_positional.py` does.

### 2. Lines on the same row arrive out of order

Even within a single visual line, individual words can sit a few points above or
below their neighbors -- dropped letters, italics, baseline jitter from the
scan. A naive "is this word vertically close to the previous word" grouping pass
breaks when a jittered word arrives out of sequence: it lands on the wrong line
and then sorts into the wrong position (e.g. "entered **The** / **it.** first"
instead of "entered **it. The** first").

The fix is to group words by comparing each word's vertical *center* against a
line's running average center, which is stable regardless of the order words
arrive in.

### 3. Paragraphs break in the middle of sentences

Page layout artifacts cause the reconstruction to start a new paragraph in the
middle of a sentence. The heuristic that fixes this: a paragraph should only end
on sentence-final punctuation (`.` `?` `!`, optionally followed by a closing
quote) or a colon/semicolon. If a paragraph ends mid-sentence **and** the next
paragraph starts with a lowercase letter, the break was an artifact and the two
are merged. Starting with a capital or an opening quote is left alone, which
preserves dialogue turns and genuine new paragraphs.

On a 380-page novel this merged 284 false breaks while leaving conversations
intact.

## Features

- Coordinate-based extraction that fixes reading order
- Robust line grouping that survives baseline jitter
- Sentence-continuity merging for false paragraph breaks
- `--skip-pages N` to drop front matter (jacket, title, copyright) whose
  multi-column layouts are the worst-case for any text extractor
- `--cover-page N` to keep the original color cover as a rendered image at the
  front, so the output still looks like a finished book
- `--report` to flag the handful of paragraphs with leftover OCR junk for a
  quick manual pass
- Clean 12pt black body text, adjustable with `--font-size`

## Usage

```bash
python pdf_to_ereader.py input.pdf output.pdf
python pdf_to_ereader.py input.pdf output.pdf --skip-pages 11 --cover-page 0
python pdf_to_ereader.py input.pdf output.pdf --font-size 13 --report
```

Different books have different amounts of front matter, so `--skip-pages` and
`--cover-page` are set per book. Run once with `--report` to see where the
novel actually starts and which page holds the cover.

## Requirements

```bash
pip install PyMuPDF reportlab pillow
```

`pdftotext` (from poppler-utils) is used only by the optional legacy backend.

## How it fits together

- `extract_positional.py` -- coordinate-aware extraction. Turns a page into
  lines of text in true reading order, synthesizing leading indentation so the
  reflow logic can detect paragraph starts.
- `pdf_to_ereader.py` -- the pipeline: extract, clean junk characters, reflow
  into paragraphs, merge false breaks, optionally add a cover, render to PDF.

## Limitations

This gets a book to "comfortably readable," not "typeset perfectly." What
remains after the structural fixes is character-level scanner noise: an
occasional misread drop-cap, a stray garbled quote. These are isolated typos,
not broken structure, and `--report` surfaces the worst of them. Heavily
designed pages (book jackets, title pages) are best skipped rather than parsed.