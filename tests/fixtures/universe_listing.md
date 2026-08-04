# Fixture universe document

**This document describes nothing real.** It exists so the universe-extraction
path has bytes to read, and so a test asserting that identities are *extracted*
rather than stated can point at the characters they came from.

No ThetaData document stating which SPX/SPXW contracts exist has been read by
this repository, and the production universe registry is empty. See
`docs/OPEN_DECISIONS.md` OD-11.

## How an extractable rule looks

A universe document states its contracts in a delimited, machine-readable block.
The delimiters are what make the reading reproducible: an extractor finds the
block by rule name, parses the rows inside it, and records the character range it
read them from. Prose around the block is not consulted, because a rule inferred
from prose is a rule somebody decided.

<!-- universe-rule: spxw_march_20_ladder -->
symbol,expiration,strike,right
SPXW,2026-03-20,4990,C
SPXW,2026-03-20,5000,C
SPXW,2026-03-20,5010,C
<!-- end-universe-rule -->

## A second rule, stating something else

Two rules in one document, so a test can show that naming the wrong one extracts
a different contract set rather than the same one.

<!-- universe-rule: spxw_march_20_puts -->
symbol,expiration,strike,right
SPXW,2026-03-20,5000,P
<!-- end-universe-rule -->
