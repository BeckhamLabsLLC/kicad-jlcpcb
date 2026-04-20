# Runtime troubleshooting (for the plugin skill)

Quick-reference for failure modes the skill is likely to encounter. For the full user-facing guide see `/TROUBLESHOOTING.md` at the plugin root.

## Detection / install

- `detect_kicad` returns `meets_min: false` → relay the `install_hint` field to the user verbatim; do not try to install KiCad yourself.
- `detect_kicad` returns `found: false` → KiCad isn't on PATH; same — relay the install hint.
- MCP server won't start at all → user needs `pip install -e .` from the plugin directory.

## Part sourcing

- `lcsc_resolve_bom` has unresolved rows → ask the user for substitutions; don't silently pick a different part.
- Every search result is extended-tier → loosen the query (drop tolerance spec, widen package options) and try again; if still extended-only, proceed but surface the setup-fee cost to the user as part of the BOM checkpoint.
- `lcsc_search` returns zero results on a reasonable query → suggest refreshing the cache (`rm ~/.cache/kicad-jlcpcb/lcsc.sqlite` and retry).

## EasyEDA pin maps

- `part_pin_map` returns `PartLibraryError: EasyEDA has no component data` → hardcode a `pinmap` in the PCB spec for that component, or find an alternative LCSC part that does have data.
- First-run delay (~12 s per unique IC) is expected and documented. Reassure the user; don't retry.
- `EasyEDA 403 / backing off 60s` → let it retry once. If it fails again, fall back to explicit `pinmap` fields.

## `pcb_generate`

- `Footprint not found: Library:Footprint` → grep `/usr/share/kicad/footprints/` for a close match and fix the spec.
- A net has fewer than 2 pads → one terminal is dangling; inspect the `nets` dict for typos.
- `result.errors` is non-empty → each error names the offending ref and pin; fix the spec and retry (pin maps are cached so retry is cheap).

## Session persistence

- `/pcb-new` on an existing directory offers resume → honor the user's choice: resume reads the persisted `bom` / `spec`; restart deletes the session file first.
- Session file is corrupt → plugin logs a warning and returns `None`; treat as a fresh project.
