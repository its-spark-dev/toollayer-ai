# Contributing

This is a portfolio project, so the bar is a little different from a library's: changes should
make the *engineering* clearer, not only add features.

## Getting set up

```bash
make setup
make check    # lint + typecheck + test — exactly what CI runs
make demo     # the full flow against real services
```

Python 3.11 or 3.12. Node 20+ for the console. No API key, ever.

## Ground rules

**Every architectural claim needs a test.** If the README says a control refuses something,
`tests/security/` must show it refusing. A claim without a test is not a claim.

**The two services never import each other.** `control_plane` and `runtime_service` may import
the shared packages; the shared packages may import neither. This is the boundary the whole
design rests on.

**Shared packages hold no business logic.** `packages/contracts` owns schemas, models,
serialization, errors, and adapters. Putting service behavior there recreates the coupling the
boundary exists to prevent.

**Contracts change in both representations at once.** The JSON Schema and the Pydantic model
describe the same document. `tests/contract` fails if they drift — and it has caught a real
defect, so do not work around it.

**Refuse rather than approximate.** When the converter meets something it cannot represent
faithfully, it raises with a stable code and a JSON Pointer. Silent approximation produces tools
that behave differently from their descriptions.

**Never echo rejected input.** Error messages name the failing rule and its location. Rejected
values end up in logs and browsers.

## Style

- English everywhere: code, comments, docs, commit messages, UI copy.
- Ruff and mypy (strict) must pass. Type annotations on everything public.
- Comments explain *why*, not *what*. A comment restating the line below it is noise.
- Tests are named as sentences describing the behavior:
  `test_a_path_argument_cannot_escape_its_segment`.

## Adding a dependency

Justify it. A dependency that is used by one function is usually worse than fifteen lines. No
dependency is added for keyword value.

## Commits

Conventional-commit style, imperative, one concern per commit:

```
feat(control-plane): reject a version that does not increase
fix(policy-engine): check every resolved address, not only the first
docs: explain the OpenAI strict-mode normalization
test(security): cover redirect refusal
```

Do not add co-author or generated-by trailers. CI enforces this.

## Adding a provider adapter

1. Subclass `ProviderAdapter` and implement `project_tool`.
2. Never widen what the canonical definition asserts. If a constraint cannot be expressed,
   raise `AdapterError` with a stable code — a silently dropped `maxLength` is a validation hole.
3. If a normalization is unavoidable, make it *reversible* and document it, as the OpenAI
   strict-mode projection does.
4. Add a round-trip test in `tests/contract/`.

## Documentation

`docs/` is a deliverable, not an afterthought. A change to a boundary, a contract, or a security
control updates the corresponding document in the same commit. A decision with a real trade-off
gets an ADR.
