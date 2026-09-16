# Security policy

## Supported versions

Only the latest release is supported. Fixes land on `main` and ship in the
next tag.

## Reporting a vulnerability

Please report privately via
[GitHub Security Advisories](https://github.com/BeckhamLabsLLC/kicad-jlcpcb/security/advisories/new)
rather than opening a public issue. Expect an initial response within a week.

## What this plugin touches

Worth knowing before you audit it, and before you run it somewhere sensitive:

- **It executes `kicad-cli`** as a subprocess, with arguments partly derived
  from user/model input (file paths, project names). Project names are
  validated against `^[A-Za-z0-9][A-Za-z0-9_\-]*$`; paths are resolved before
  use. Arguments are passed as a list, never through a shell.
- **It writes files** — projects, libraries, Gerbers, and a session file —
  under a directory the caller chooses.
- **It makes unauthenticated outbound HTTPS requests** to two third-party
  services, `jlcsearch.tscircuit.com` and `easyeda.com`. No credentials are
  sent, and no user data beyond the search terms. Both base URLs are
  overridable via `KJLC_JLCSEARCH_BASE` and `KJLC_EASYEDA_BASE` if you need
  to point at a mirror you control, or block them entirely.
- **It caches part metadata** in `~/.cache/kicad-jlcpcb/`. Nothing secret
  goes there; delete it freely.
- **It requires no API keys or credentials** of any kind.

## Not a vulnerability

- Part data being wrong or stale. Both upstream services are community-run
  and unofficial. Verify a BOM before ordering; the plugin flags stock it
  cannot confirm.
- `kicad-cli` or `pcbnew` crashing on malformed input.
