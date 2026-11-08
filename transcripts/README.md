# Extraction transcripts

Each folder holds the transcript the local reader wrote for one golden invoice: `make read` runs `src/invoice_agent/reader.py` over `evals/golden/invoices/` and `evals/golden/injection/` and rewrites this folder. The run replays these transcripts; it never reads `evals/golden/expected.json`.

## How a transcript is produced

- A PDF whose pages carry a text layer (at least 50 characters per page) is read from that layer with PyMuPDF. Mode `pdf_text_layer`; every quote is a line of the layer; confidence is 1.0 because no recognition took place. KBP-2025-0417, RIC-250319-00718 and FPC-25-10733 are read this way.
- A PDF without a text layer, or a PNG, JPEG or TIFF image, is read by OCR with RapidOCR and the PP-OCRv4 detection and recognition models that ship inside the `rapidocr-onnxruntime` wheel. Nothing is downloaded and no network call is made. Mode `local_ocr`. NBF-0051376, PCS-25-03917 and DLP-2025-0907 are read this way.
- Each OCR page is recognized twice, as supplied and scaled to a 3500 pixel long side, because the recognizer drops the space between two words at some sizes and keeps it at others. Per line, the reading with more spaces is kept, then the higher score. Dropped word boundaries are re-opened, the letter o inside an IBAN is read as the digit 0, and a mixed-case legal form at the end of a line is re-cased from the legal-form table in `rules.py`.
- The seven fields are found by label-anchored parsers: a value is read inline after its label, else from the nearest line to the right on the same row, else from the nearest aligned line below. The supplier name comes from the line under a From, Supplier or Vendor heading, else the letterhead, else a Remit to block; the address is the aligned block under an Address heading or under the name.
- The guard runs over the full text of every page the reader kept. Its hits are the `flags`, with accounts masked.

## What a transcript contains

| Key | Content |
|---|---|
| `metadata.source_sha256`, `metadata.source_path` | The exact bytes and the repository-relative path of the invoice that was read |
| `metadata.mode` | `pdf_text_layer` or `local_ocr` |
| `metadata.reader_version`, `metadata.model` | The reader version and the text source: the PyMuPDF text layer or the PP-OCRv4 models from `rapidocr-onnxruntime` 1.4.4 |
| `metadata.provenance_note` | One sentence written by the reader on how the page was read |
| `response` | The seven fields, each with `value`, `page`, `quote` and `confidence`, and the `flags` |
| `reading.lines` | Every line the reader kept, in reading order, each with `page`, `text` and `score` |

The reading is the page's text as the reader read it, account numbers included; it is part of the transcript, not of the run's output, and real invoices and their transcripts stay out of the repository.

A quote is one line of the reading, or several lines joined with `\n`. A field's confidence is the lowest score over the lines it quotes, rounded to two decimals.

## What the replay verifies

The `source_sha256` binds the transcript to the exact bytes of its source. For a PDF or image source the replay also requires a reader transcript with its reading, and for every field with a value: a non-empty quote whose lines each equal a line of the reading on the field's page, in reading order, with whitespace folded and case kept; and the value inside its quote. A quote edited by hand, moved to another page or stitched from text the reader never kept quarantines the invoice. The replay also re-scans the stored reading with the guard, so an instruction printed on the page is flagged even if the `flags` list were edited away.

The reading itself is bound to the page by the source hash and by `tests/test_reader.py`, which re-runs the reader on the six invoices and compares values, quotes and confidence (within 0.02) with these files. It is not re-recognized on every replay.

For a `.txt` source, used by the synthetic cases in `evals/synthetic/`, the transcript stays a curated `curated_synthetic` fixture without a reading; every field quote must occur in the source text and the value inside its quote.

Re-rendering an invoice with `scripts/render_invoices.py` changes its bytes (a PDF carries a creation time). Run `make read` afterwards, or the replay refuses the source.

Runtime output may normalize exact source strings for comparison with the immutable golden labels. The transcript remains source-exact.
