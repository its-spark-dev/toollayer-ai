#!/usr/bin/env python3
"""Generate an ephemeral Ed25519 key pair for snapshot signing.

Prints shell ``export`` lines so a caller can ``eval`` them:

    eval "$(python scripts/generate_signing_key.py)"

The point of generating rather than shipping is that there is then no key file anywhere in
this repository. A committed development key is a credential-shaped object that eventually
gets copied into somewhere it matters — and a reviewer cannot tell, from the file alone, that
it was never meant to be one.

Each invocation produces a new pair. That is correct for a demonstration, where the Control
Plane and the Runtime are started together by the same script. A real deployment generates
the key once, holds the private half in a secret manager, and distributes only the public
half to each runtime; ``docs/deployment.md`` describes that.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for package in (
    "packages/contracts",
    "packages/openapi-converter",
    "packages/policy-engine",
    "packages/mock-llm",
):
    sys.path.insert(0, str(REPO_ROOT / package))

from toollayer_contracts.signing import (  # noqa: E402
    encoded_private_key,
    generate_signing_key,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key-id",
        default="demo-signing-key",
        help="Key identifier embedded in the signature and used to select a trusted key.",
    )
    parser.add_argument(
        "--format",
        choices=("shell", "compose-env"),
        default="shell",
        help="'shell' prints export lines to eval; 'compose-env' prints bare KEY=VALUE lines.",
    )
    arguments = parser.parse_args()

    key = generate_signing_key(arguments.key_id)
    values = {
        # The Control Plane signs with this. It never leaves the process that holds it.
        "TOOLLAYER_SNAPSHOT_SIGNING_KEY": encoded_private_key(key),
        "TOOLLAYER_SNAPSHOT_SIGNING_KEY_ID": key.key_id,
        # The Runtime verifies with this. Public, and safe to log or print.
        "TOOLLAYER_SNAPSHOT_TRUSTED_KEYS": f"{key.key_id}:{key.encoded_public_key()}",
    }

    prefix = "export " if arguments.format == "shell" else ""
    for name, value in values.items():
        print(f"{prefix}{name}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
