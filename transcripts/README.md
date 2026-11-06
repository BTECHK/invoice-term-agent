# Replay provenance

The files in this folder are curated synthetic replay fixtures. They are not recorded model responses and do not claim a provider, model identifier, execution timestamp or model confidence.

Each envelope records the SHA-256 hash and repository-relative path of the source invoice. The `response` values and quotes were transcribed from page 1 of that source. The PDF invoices are printed from HTML and carry a text layer; their quotes were still checked against the rendered page. A quote keeps the line breaks of the layout, written as `\n`.

The confidence values are fixture confidence assigned after human visual verification. They exist only because the extraction schema requires a number. They must not be presented as model-generated confidence.

The invoice that prints its net term and discount on separate lines uses the exact printed discount term as its payment term value; its quote includes both labelled lines so the complete source context remains available.

Runtime output may normalize exact source strings for comparison with the immutable golden labels. The transcript remains source-exact and the runtime never reads `evals/golden/expected.json`.

Re-rendering an invoice with `scripts/render_invoices.py` changes its bytes (a PDF carries a creation time). Copy the printed hash into `source_sha256` of the matching transcript, or the replay refuses the source.

## What the hash proves

The `source_sha256` value binds a transcript to the exact bytes of its source. It does not prove that a quote is accurate.

For a `.txt` source the replay also requires every field to carry a quote, every quote to occur in the source text and every value to occur inside its own quote, ignoring whitespace differences, and an invoice that fails this is quarantined. The same holds for every transcript in this folder and in `evals/synthetic/transcripts/`. PDF and image sources are bound by hash only, so their curated quotes are trusted provenance from the visual check described above, not text the code verified.
