# Contributing to kicad-jlcpcb

Thanks for your interest in improving the plugin. This guide covers the dev setup, how tests are organised, and what we look for in pull requests.

## Dev setup

```bash
git clone https://github.com/BeckhamLabsLLC/kicad-jlcpcb.git
cd kicad-jlcpcb
python -m venv .venv
source .venv/bin/activate       # Windows: .\.venv\Scripts\activate
pip install -e ".[dev]"
```

After the editable install the `kicad-jlcpcb` entry-point script is on your `PATH` — that's the same script `.mcp.json` calls, so local changes take effect immediately.

## Running tests

```bash
pytest tests/
```

`pyproject.toml` sets `pythonpath = ["src"]`, so `pytest` works from a clean checkout with no environment fiddling.

Tests that talk to KiCad's `pcbnew` Python module are gated behind an env var:

```bash
KICAD_INSTALLED=1 pytest tests/ -v
```

Run a single file or test:

```bash
pytest tests/test_schematic.py -v
pytest tests/test_pcb.py::TestPlace::test_three_band_layout -v
```

## Code style

We use [ruff](https://docs.astral.sh/ruff/) for both linting and formatting. Before opening a PR:

```bash
ruff check .
ruff format .
```

The configuration lives in `pyproject.toml`. Key rules:

- Line length 100
- Target Python 3.10
- Lint rules: E, F, W, I (imports)
- `E501` (line length) is ignored so comments/strings can flow naturally

## Filing a bug report

Open a GitHub issue with:

- Your environment: OS + Python version + KiCad version (`kicad-cli --version`)
- Plugin version (`cat .claude-plugin/plugin.json | grep version`)
- Exact reproduction steps — the Claude Code prompt you used is ideal
- What you expected vs what happened
- Any relevant log output. To capture verbose MCP logs, set `CLAUDE_MCP_DEBUG=1` before launching Claude Code.

## Submitting a pull request

Checklist:

- [ ] Tests added or updated for any behaviour change
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] Full test suite passes (`pytest tests/`)
- [ ] If the change is user-facing, `README.md` is updated
- [ ] An entry is added to `CHANGELOG.md` under `[Unreleased]`
- [ ] Manually exercised the affected flow (especially for PCB generation changes)

Commit messages: imperative mood, explain the "why" in the body when it isn't obvious from the diff. Example:

```
Prefer extended-tier fallback over loosened basic query

When every basic part is out of stock, the loosened query
was silently dropping tolerance specs, which masked a bug
where basic 10k@1% resistors were out but basic 10k@5% were fine.
Tighten the fallback to try extended first.
```

## Scope and philosophy

- **Phase 1.6 is about reliability, not routing.** The plugin wires things up; EasyEDA routes and orders. PRs that add headless routing (Freerouting integration, SA-PCB, etc.) will need a serious justification.
- **Hard preference for JLCPCB basic-tier parts.** Any change that silently picks extended-tier parts without a cost warning is a regression.
- **Tests gate refactors.** If a refactor "can't be tested," that usually means the refactor is also hard to reason about.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Report issues to alex@beckhamlabs.com.

## License

By contributing, you agree your changes are licensed under the [MIT License](LICENSE).
