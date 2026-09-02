# RFCs

Changes to the **Capability Tag specification** go through an RFC. Changes to the
implementation do not — those are ordinary pull requests. The reasoning for the split is
in [`GOVERNANCE.md`](../../GOVERNANCE.md).

## Why even an added field needs one

Tags validate strictly: `additionalProperties: false` in the JSON Schema, `extra="forbid"`
in the Pydantic models. A reader built against an older spec does not ignore a field it
has never seen, it **rejects the whole tag**. So there is no such thing as a backward
compatible addition here, and every change bumps `mhs_version`.

That strictness is deliberate. A reader that silently ignored an unknown key would happily
accept a tag whose safety-relevant field it does not implement, and enforce a weaker
envelope than the tag declares while reporting success.

## Process

1. Copy [`0000-template.md`](0000-template.md) to `NNNN-short-name.md`, next free number.
2. Open a pull request with the RFC only. No implementation yet.
3. Discussion on that pull request. The maintainer decides in public, with reasons.
4. Merged either way. Accepted RFCs record the spec version they land in; rejected ones
   record why and **stay in the tree**, so the next person can see what was already
   considered.
5. Implementation lands separately and bumps `mhs_version`.

## Index

| RFC | Title | Status | Spec version |
|---|---|---|---|
| [0001](0001-modular-quantities.md) | `period` for modular quantities | draft | 0.3 if accepted |

Candidates already named on the roadmap and worth writing up before they are built:
sensor confidence (so the middleware can refuse to act on a degraded reading), signed
capability tags, and orientation of a held object.
