# Dev Notes / Backlog

Running notes for contributors. Not user-facing — see README (Phase 9) for usage.

## Testing

- Frontend has no runtime DOM tests by design (no JS build step per SPEC §10); loading/render lifecycle is verified by manual passes and static assertions over the served JS/CSS. Reconsider a headless-browser harness (e.g. Playwright) if `web/` grows materially.
