# License review required

**This repository does not yet carry a license.**

That is deliberate, and it is not an oversight to be fixed by copying a license file in.

## What "no license" means right now

Without a license, default copyright applies: the work is the author's, and no permission to
use, copy, modify, or redistribute it is granted to anyone. If you are reading this in a
private repository, that is the intended state.

## Why the decision is pending

This repository is an independent, clean-room portfolio implementation. The author previously
worked in the same problem domain, and `docs/CLEAN_ROOM_PLAN.md` records how that experience
was kept from becoming a source of content. Choosing a license is the point at which that
claim becomes a public statement rather than an internal process note, so it is made
deliberately and only after a human review confirms:

1. no employer-confidential material is present in the working tree or in Git history;
2. no third-party code has been incorporated under an incompatible license;
3. every dependency's license permits the intended distribution;
4. publishing this work is compatible with the author's obligations.

`PRE_PUBLICATION_REVIEW.md` records the result of that audit.

## Intended outcome

The intended license is **MIT** — permissive, well understood, and appropriate for a
portfolio project meant to be read and learned from. When the review above is complete and
approved, this file is replaced by a `LICENSE` file containing the MIT text with the correct
copyright line, and `pyproject.toml` declares it.

## Dependencies

Every runtime dependency is a widely used open-source library under a permissive license
(BSD-3-Clause, MIT, or Apache-2.0). No dependency carries a copyleft obligation that would
constrain the license choice. The inventory is in `PRE_PUBLICATION_REVIEW.md`.

## Until then

Do not redistribute this repository or incorporate it into another project.
