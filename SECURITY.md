# Security policy

## Reporting a vulnerability

This is a portfolio project, not a production system. If you find a security issue, please open
a GitHub issue describing it. There is no embargo process and no bounty.

Do not include real credentials, real customer data, or anything confidential in a report.

## What this project is

ToolLayer AI demonstrates how an AI application can call real APIs under governance. Several of
its controls are genuine and tested; several are deliberate simplifications. Both are documented
in [`docs/threat-model.md`](docs/threat-model.md), which is the authoritative statement.

**Do not deploy this as-is.** [`docs/deployment.md`](docs/deployment.md) §10 lists the specific
gaps: static bearer tokens with no rotation, no TLS in the compose topology, an admin token
shipped in the console bundle, no rate limiting, and no secret management.

## Implemented controls

Each has a test in [`tests/security/`](tests/security/) or [`tests/contract/`](tests/contract/):

- Default-deny destination allowlist with exact origin matching.
- Post-resolution address checks; link-local refused unconditionally.
- No redirects, no environment proxies, finite timeouts, bounded responses.
- Closed input schemas; requests built from published bindings rather than supplied arguments.
- One authorization function shared by discovery and execution.
- Server-authoritative publication.
- Digest-verified artifacts, recomputed by the consumer.
- Separate administrator and service credentials, enforced at startup.
- Untrusted-content marking with no path from a result back into tool selection.
- Error messages that name the failing rule and location, never the rejected value.

## Known limitations

The full list is in [`docs/threat-model.md`](docs/threat-model.md) §7. The ones most likely to
matter:

- The runtime does not authenticate callers; it enforces roles the client asserts.
- Disablement takes effect at the next snapshot refresh, not immediately.
- A time-of-check-to-time-of-use gap exists between DNS resolution and connection.
- Argument validation is structural, not semantic — authorization over *which records* a caller
  may see remains the upstream API's decision.

## Local development escape hatches

`TOOLLAYER_ALLOW_PLAINTEXT_HTTP`, `TOOLLAYER_ALLOW_LOOPBACK_DESTINATIONS`, and
`TOOLLAYER_ALLOW_PRIVATE_ADDRESSES` widen the destination policy so the demo can call a service
on localhost.

All three default to **off**. `/healthz` reports when any is on. A test proves loopback is
refused when the flag is off. No setting enables link-local addresses.
