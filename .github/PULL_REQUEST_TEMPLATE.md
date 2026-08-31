## What this changes

<!-- One or two sentences. What is different after this PR? -->

## Why

<!-- The problem, not the patch. -->

## Safety impact

- [ ] This change does **not** touch the safety evaluation path
- [ ] It does — and I have explained why below, with a test that fails without it

<!-- If it touches safety: which enforcement point, and what stops the other one from drifting? -->

## Checklist

- [ ] `pytest` passes
- [ ] `ruff check .` passes
- [ ] New behaviour has a test that fails before the change
- [ ] For a driver: the transport is mocked, not the driver, and rejection tests assert
      **zero bytes transmitted**
- [ ] No safety bound was widened to make something validate

## What I did not verify

<!-- Be specific. "Not tested against real hardware" is a fine answer; an unstated gap is not. -->
