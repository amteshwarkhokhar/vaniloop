---
name: Work order
about: One task that one PR can finish (at most 600 changed lines, 20 files)
labels: []
---

<!-- What and why, in plain words. Name one concrete call (placeholder names only). -->

Allowed paths: `src/vaniloop/...`, `tests/`
Forbidden paths: `transcripts/`, `runs/`, `data/raw/`, `.env` (never commit call data)
Verify: `bin/check`
Stop rule: stop and report if `models.py` must change and this issue does not say so, or a check still fails after 2 attempts.

<!-- Optional, one per line: Depends on #n -->

## Acceptance

- <claim> — done when: <a test, command or file that shows it>
- <what must not change> — done when: <check>
