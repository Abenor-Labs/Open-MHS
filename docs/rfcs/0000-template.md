# RFC NNNN: <short title>

| | |
|---|---|
| **Status** | draft \| accepted \| rejected \| superseded by RFC NNNN |
| **Spec version** | the `mhs_version` this lands in, or "n/a" |
| **Author** | |
| **Created** | YYYY-MM-DD |
| **Decided** | YYYY-MM-DD, or blank while in draft |

## The device that cannot be described today

Name a real piece of hardware and the thing about it the current schema cannot express.
Not a hypothetical. Every field in this schema exists because something physical needed
it, and a field added on speculation is a field that has to be enforced forever.

If the answer is "you can express it, but awkwardly", say that instead and argue that the
awkwardness is dangerous rather than merely ugly.

## What breaks if we do nothing

The consequence of the gap, stated concretely. A bound that has to be written wider than
reality? A capability a tag claims but a driver cannot deliver? An envelope that goes
stale? Say which of the invariants in `CLAUDE.md` is under pressure.

## Proposal

The exact change to `schema/capability_schema.json`, as JSON. Include:

- the new or changed field, with its type, constraints and whether it is required
- the prose that will appear in `docs/capability-tags.md`
- a complete example tag fragment using it

```json
{
  "example": "here"
}
```

## What a reader that does not understand this field does

The question that decides most RFCs. Tags validate strictly, so an older reader **rejects**
a tag carrying an unknown field rather than ignoring it. Confirm that is the behaviour you
want, and state the spec version bump.

If your answer is that older readers should ignore it, explain why that does not leave them
enforcing a weaker envelope than the tag declares. That is a high bar and it is meant to be.

## Enforcement

Where the new field is checked, and what happens when it is violated:

- **Ingestion** — which cross-field rule in `open_mhs/server/models.py` validates it.
- **Runtime** — whether it affects `open_mhs/server/safety.py`, and at which of the two
  enforcement points.
- **On violation** — the error code and what the refusal tells the caller.

A field that is parsed and never enforced is worse than an absent one. If this RFC adds
one, say when enforcement lands and why it is acceptable to ship the gap.

## Tests that must exist before this ships

List them. At minimum, for anything touching a bound: a test that the refusal transmits
zero bytes, and a mutation you will apply to prove that test bites.

## Alternatives considered

Including doing nothing. Say why each was rejected. This section is what makes the RFC
useful to the next person, who will otherwise propose the same alternative again.

## Effect on existing tags

Every shipped tag and fixture that must change, and whether a tag written before this RFC
remains valid.
