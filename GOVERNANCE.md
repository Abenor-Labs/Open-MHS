# Governance

This project calls itself a standard, so it owes you an honest answer to one question:
**who decides what goes in the schema, and how?**

## Where the project actually is

One maintainer. No foundation, no steering committee, no vendor consortium. Inventing one
on a repository this young would be theatre, and you would be right not to believe it.

What that means in practice:

- The maintainer merges everything and can be overruled by nobody.
- The bus factor is one. Everything needed to continue the project is in this repository:
  no secrets outside environment variables, no private build steps, a public CI
  configuration, and a release process written down in `docs/DEVELOPING.md`.
- Recruiting a second person with merge rights is an open task, not a formality. Until it
  is done, treat single-maintainer risk as real.

If that is disqualifying for your use, that is a reasonable conclusion to reach, and it is
better reached from this page than from a surprise.

## Two artifacts, two standards of care

The repository contains a **specification** and an **implementation**, and they are not
governed the same way.

| | Specification | Implementation |
|---|---|---|
| What | `schema/capability_schema.json`, the versioning policy, the conformance suite | `open_mhs/`, the CLI, the MCP adapter, the drivers |
| Changed by | an RFC (below) | a pull request |
| Breaks | every tag author and every other reader | this package's users |
| Versioned as | `mhs_version` inside each tag | the package version |

A change to the implementation affects people who chose this code. A change to the
specification affects people who never ran it, including implementations that do not exist
yet. That asymmetry is why the spec gets a slower process.

## Changing the specification: RFCs

Any change to the Capability Tag format goes through an RFC in [`docs/rfcs/`](docs/rfcs/).
That includes adding a field, and the versioning policy explains why: tags validate
strictly, so a reader built against an older spec does not ignore an unknown field, it
*rejects the tag*. There is no such thing as a purely additive change here.

The process, in full:

1. Copy [`docs/rfcs/0000-template.md`](docs/rfcs/0000-template.md) to
   `docs/rfcs/NNNN-short-name.md`, taking the next free number.
2. Open a pull request with the RFC alone, no implementation.
3. Discussion happens on that pull request. There is no minimum comment period and no
   voting; the maintainer decides, in public, with the reasoning written down.
4. An accepted RFC is merged with `Status: accepted` and the spec version it lands in.
   A rejected one is merged too, with `Status: rejected` and the reason. **Rejected RFCs
   stay in the tree**, because the most useful thing a specification can tell you is what
   was already considered and turned down.
5. Implementation follows in a separate pull request, and bumps `mhs_version`.

Two things an RFC will always be asked, and it is faster to answer them up front:

- **Which device cannot be described today?** A concrete piece of hardware, not a
  hypothetical. Every field in the schema exists because something real needed it.
- **What happens to a reader that does not understand the new field?** If the answer is
  "it silently ignores it", the design is wrong, because that reader would then enforce a
  weaker envelope than the tag declares.

Changes to the conformance suite follow the same process once that suite exists, because
its fixtures define what "compliant" means.

## What "Open-MHS compliant" means

Precisely one thing: **the implementation passes the published conformance suite for a
stated spec version.** Not "uses the schema", not "was inspired by this". Anyone may build
a reader or a driver and claim compliance on that basis, without asking permission and
without any relationship to this project.

The name is not a trademark and is not policed. Several unrelated repositories use it.
That is the reason compliance is defined against a suite you can run rather than against
anyone's approval: the claim should be checkable by the person hearing it.

Until the conformance suite ships, nothing can be called compliant, including this
implementation. That gap is tracked on the roadmap.

## Safety changes are different

The invariants in [`CLAUDE.md`](CLAUDE.md) are not style preferences, and a pull request
touching them gets read differently:

- A safety bound is never widened to make something validate. If a device cannot be
  expressed, that is an RFC, not a local edit.
- A test is never weakened to make a change pass. If a test is wrong, it is fixed in its
  own commit, with the reason in the message.
- Both enforcement points stay. There are tests that fail if either is deleted, and those
  tests are the point.
- A capability tag must not claim a capability the hardware does not have.

A change that trips one of these will be asked to justify itself against the invariant, in
writing, in the pull request. "The tests pass" is not the argument, because the tests were
written by the same people who might be wrong.

## Licensing and contributions

Apache-2.0, for both the specification and the implementation. By opening a pull request
you certify that you wrote the contribution or have the right to submit it, and that it is
offered under the same licence. Sign off your commits with `git commit -s` if you want that
recorded explicitly.

## Reporting a security issue

Do not open a public issue. Follow [`SECURITY.md`](SECURITY.md).

## Changing this document

Governance changes go through the same RFC process as the specification, and for the same
reason: they affect people who are not in the room.
