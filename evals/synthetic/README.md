# Synthetic runtime cases

These fixtures are separate from the immutable golden set. They exercise successful and exceptional payment-term paths in more variety than the golden set, which has one clear invoice.

Each source is deliberately plain text so its provenance is inspectable. Its matching transcript is a curated synthetic extraction, not a recorded model response. The mock fixture directory is complete and is selected explicitly with the command line `--fixtures` option.

The cases cover equal terms, a validated longer term, a discount taken, a discount declined, a never-accelerate vendor and an over-60-day term without express agreement.
