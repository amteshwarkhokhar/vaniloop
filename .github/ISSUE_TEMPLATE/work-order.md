---
name: Work order
about: One task for one agent or person
labels: []
---

## Goal

One or two sentences. What is true when this is done.

## Allowed paths

-

## Forbidden paths

- `transcripts/`, `runs/`, `data/raw/`, `.env` (read only if the goal needs real calls; never commit)

## Done when

- [ ]

## Verify

```bash
bin/check
```

## Stop rule

Stop and report if: the contract in `models.py` must change and this issue does not say so,
or a check still fails after 2 attempts.
