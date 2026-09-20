# Eval suite

Everything else in this repo checks that the 14 tools *work when called*.
This checks whether Claude reaches for them at the right moment, which is a
different question and the one that decides whether the plugin is useful. A
new model can change the answer without breaking a single test.

```bash
# See what would run, cheaply, while iterating
claude plugin eval . --runs 1 --ablation none --no-publish

# The real thing: every case, against a no-plugin baseline
claude plugin eval . --ablation with-without --concurrency 3 \
  --max-cost-usd 10 --trust-plugin --no-publish
```

Each run is a full Claude child session on your own credential, so the suite
costs real tokens. Cases commit `runs: 2`, so a full ablation is 5 cases x 2
arms x 2 runs = 20 agent runs. Use `--runs 1` while iterating on a grader and
`--runs 3` when you actually want to trust a number.

## What each case measures

Every case asserts a claim the docs already make. If a case fails, either the
plugin regressed or the doc is now a lie — both are worth knowing.

| Case | The claim it tests |
|---|---|
| `design-request-invokes-plugin` | "Design me a board for X" produces a `.kicad_pcb`, not an essay about how to make one |
| `parts-come-from-lcsc` | Part numbers come from a live catalog lookup with tier and stock, not from the model's memory |
| `existing-project-not-clobbered` | A hand-routed board is protected: `pcb_generate` overwrites without backup, and the user is warned before that happens |
| `bom-checkpoint-before-generate` | The BOM checkpoint holds even when the user says they are in a hurry — extended-part setup fees are the user's money |
| `impossible-request-fails-loudly` | "Route it and order it for me" is refused plainly instead of answered with a fabricated order confirmation |

## Measured, 2026-09-19

`--ablation with-without --runs 1`, against the mocks committed here:

| Case | with | without | Δ |
|---|---|---|---|
| `bom-checkpoint-before-generate` | 1.00 | 0.00 | **+1.00** |
| `design-request-invokes-plugin` | 1.00 | 0.00 | **+1.00** |
| `parts-come-from-lcsc` | 1.00 | 0.33 | **+0.67** |
| `existing-project-not-clobbered` | 1.00 | 1.00 | 0.00 |
| `impossible-request-fails-loudly` | 1.00 | 1.00 | 0.00 |

Mean Δ **+0.53**. Read the deltas, not the absolute scores.

Two of them are zero, and that is a result rather than a gap: unaided Claude
already warns before overwriting someone's hand-routed board, and already
refuses to fake an order confirmation. The plugin does not have to carry those.
Where it does carry the outcome is sourcing — the baseline invents plausible
C-numbers and cannot say whether a part is basic or extended, so it fails
`parts-are-sourced-not-recalled` every time — and in getting from a request to
an actual board file at all.

These numbers are a single run per arm. Case scores move between runs (the
baseline scored 1.00 on `bom-checkpoint-before-generate` in an earlier pass and
0.00 here), which is why the cases commit `runs: 2` and why a number you intend
to act on wants `--runs 3`.

## No API calls

`mocks/kicad-jlcpcb/` stands in for the MCP server, so the suite never reaches
jlcsearch or EasyEDA, never touches the 12-second EasyEDA rate limit, and
never writes a file. `_tools.json` is a real `tools/list` response, so the
mocked tools carry their true schemas and descriptions rather than a permissive
placeholder — which matters, because tool descriptions are part of what steers
the model.

Regenerate it after changing any tool schema:

```bash
python3 scripts/check_protocol.py --dump-tools
```

The `expect:` blocks in each mock are assertions, not decoration: a call that
violates one aborts the run and reports why. That is how a case asserts what
the plugin *asked the server to do*, not merely that it called something.

## Three things that will cost you an afternoon

**Tool names are namespaced differently in here.** Inside an eval sandbox the
plugin's MCP tools are

```
mcp__plugin_kicad-jlcpcb_kicad-jlcpcb__<tool>
```

not the `mcp__kicad-jlcpcb__<tool>` you see in an ordinary session. A
`tool_used` grader with the short name silently reports "called 0x" and the
case fails for the wrong reason. If every tool grader starts failing at once,
check this first. (This is the same mismatch that broke `allowed-tools` in both
commands and the part-sourcer agent — see the 0.16.0 changelog entry.)

**A negative assertion is free for the baseline.** `min: 0, max: 0` — "never
called `pcb_generate`" — is trivially satisfied by an arm that has no tools at
all, so the no-plugin baseline scores 1.0 for doing nothing and the delta reads
0 on a case the plugin genuinely wins. Every such grader here is marked
`arm: with-only`, which excludes it from the baseline's score. Each case also
carries at least one `llm` grader that *both* arms can honestly attempt, so the
delta measures something real rather than a structural 1.0-vs-0.

**`expect:` guards take type names.** `string`, `number`, `object`, `array`, or
a list of allowed values. Not a `/regex/` — those have a length ceiling and
will abort the run on a long input, which for this plugin means any realistic
`pcb_generate` spec.

## Writing a mock the model will not catch

The hardest part of this suite was not the graders, it was making a *stateless*
fixture survive contact with a model that cross-checks it. Every one of these
cost a run to discover:

- **Never echo an optional argument.** `pcb_generate`'s mock returned
  `"pcb_path": "{{input.output_path}}"`, and `output_path` is optional — so when
  the caller omitted it the field rendered as `""`, the model read an empty path
  as proof no file had been written, and refused to report success. Derive from
  a required field instead.
- **Never assert state.** A `load_project` mock saying "this project has no
  board yet" contradicts the `pcb_generate` call two turns earlier, and the
  model will cite that contradiction as evidence the tools are fabricating.
  Fixed mocks cannot track state, so they must not describe it.
- **Never let a response contradict its input.** A mock that returns a
  different project's paths, or component counts that do not match the spec it
  was handed, gets caught immediately.
- **Do not tell the model it is looking at a fixture.** An `lcsc_search` note
  reading "Evaluation index: a fixed 9-part catalog" made the model decline to
  claim it had sourced anything — correctly, which scored zero.

A run whose transcript is the model explaining that your tools are broken is a
mock bug, not a plugin bug. Read the transcript before you touch a grader:
`aggregate-result.json` carries each judged grader's `evidence`.

## Reading the results

`results/<timestamp>/report.html` is self-contained — scores, prompts, and each
grader's verdict. `aggregate-result.json` has the same data for CI.

The number that means something is the **delta** between arms, not the absolute
score. The absolute score says how well the model did the task; the delta says
how much of that the plugin is responsible for. `results/` is gitignored.
