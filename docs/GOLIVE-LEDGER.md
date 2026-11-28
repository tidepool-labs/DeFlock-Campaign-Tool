# Go-live ledger: operator guide

Use this order in a sitting: rehearse on synthetic data, record the receipt,
check and record the attended gate, then read **Go-live** (`/golive`) with G.
If a rehearsal is not qualified or the gate says NO-GO, keep the reason visible
and stop before the attended step. A qualified rehearsal or GO does not perform,
authorize, or schedule that step. Existing approvals and holds still apply.

The examples run from the engine checkout and use synthetic paths under `scratch/`.
Use separate scratch directories, never the canonical records or evidence store,
when trying them. Add `--help` to any example to see its options without running it.

## 1. Rehearse on the candidate engine

List the available drivers without running them, then select a stage from the
list. Repeat `--stage` for several stages, or omit it to run every driver. Stage
`3` below is an example; a driver can cover more than one stage.

```sh
python3 -B -m campaign_tool.records.rehearse --list
python3 -B -m campaign_tool.records.rehearse --stage 3 --out scratch/rehearsals
```

Each driver runs in a fresh sandbox and prints the path to its JSON receipt.
Keep the receipt and its sandbox together. **QUALIFIED** means there was at
least one step, every step was `OK`, and no manual step remained. At finish,
the runner also checked the private sandbox marker and the hashes of declared
outputs against the sandbox files.
The receipt must identify the engine commit. When rechecked, a rehearsal must
have finished within the last 14 days (not in the future) and match the target
engine when the check supplies one. Use the reason codes to decide what to fix
before rerunning; do not edit a receipt to turn a failure into a pass.

## 2. Record the rehearsal, then check and record the gate

Use the receipt path printed by the runner in place of the example path below.
The evidence directory must already exist. If a rehearsal declares multiple
stages, record it once per relevant declared stage using `--stage` each time.

```sh
python3 -B -m campaign_tool.records.golive_record rehearsal scratch/rehearsals/example/receipt.json --stage 3 --evidence scratch/golive-evidence
```

For a stage that needs an attended gate, use its admitted gate spec and captured
inputs. In the example, replace `rehearsal` with the input name(s) the spec calls
for, repeating `--input` as needed. Use an existing output directory; the evaluator
writes a new `<gate_id>.json` there. GO means every declared check passed; a failed
or unknown check gives NO-GO. Record either verdict, using the actual receipt path.
By default the recorder writes a row for each declared gate stage; `--stage` can
limit it to one.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec scratch/gate-spec.json --input rehearsal=scratch/rehearsals/example/receipt.json --out scratch/gates
python3 -B -m campaign_tool.records.golive_record gate scratch/gates/example-gate.json --evidence scratch/golive-evidence
```

The recorder validates the receipt and appends JSON metadata: stage, kind, result,
time, reason codes, and a SHA-256 link to the exact receipt bytes. It does not copy
receipt contents or overwrite an existing record. The same receipt hash, stage,
and kind cannot be recorded twice. Exit `1` means failure evidence was recorded;
exit `2` means a refusal or error. When recording multiple gate stages, check the
printed filenames: an earlier append can succeed before a later one is refused.
The recorder rechecks rehearsal age but does not know the target engine and does
not re-read sandbox outputs. Keep the original receipts for review.

Live rows come from an adapter, not from a receipt. Save the adapter's `--json`
output (a JSON array of evidence rows, e.g. from `golive_adapt_facts` or
`golive_adapt_gaps`) to a file and record it; `--stage` keeps one row:

```sh
python3 -B -m campaign_tool.records.golive_record live scratch/facts.json --stage 9 --evidence scratch/golive-evidence
```

Every row must be valid `live` evidence that is not dated in the future, with
no failure codes on a pass and each stage at most once; otherwise nothing is
written. The rows are stored as the adapter wrote them (their SHA-256 links to
the output file the adapter read). A recorded live row is observed proof only:
the ledger can show `LIVE` with a gap under **Next**, which is not a go-live.

The reply-draft and follow-up joins (stages 13 and 14) print such an array with
`--evidence-row` instead of their result: one live row whose source is the
export manifest (`EXPORT.json`, the same SHA-256 the join prints), a pass only
when the join passed, otherwise a fail carrying the join's code. An unverified
export prints no row (exit 2), so there is nothing to record.

With `--evidence-row` the age limit stays at most the policy default (36 h;
a wider `--max-age-hours` is refused, plain output keeps it). If a join was
first run for the wrong queue id or revision, its fail row stays on record and
ONE later pass for the same export may follow it; nothing is recorded for that
export after a pass, and a pass older than the recorded fail is refused (run
the join again).

```sh
python3 -B -m campaign_tool.records.golive_draft_join --export-root ROOT --name EXPORT --queue-id ID --revision N --evidence-row > scratch/s13.json
python3 -B -m campaign_tool.records.golive_record live scratch/s13.json --evidence scratch/golive-evidence
```

## 3. Read the ledger and the live count

Use the phone console's **Go-live** page for the configured view. For the same
ledger calculation on a local set of run reports and evidence, use:

```sh
python3 -B -m campaign_tool.records.golive_ledger --reports scratch/reports --evidence scratch/golive-evidence
```

Each stage has one row. `BUILT` means the stage is listed but has no higher
positive progress state or recorded failure; it is not a readiness check.
**Rehearsed** and **Gate** show recorded results or why proof is missing (or
`not needed`). The ledger treats rehearsal records older than 14 days as stale
and checks rehearsal and gate engine identities against the newest live record,
if there is one. **Next** points to the first required proof still missing or
failed; it is not a command or approval.

**Live** comes from passing live evidence or, where the stage allows it, a run
report that proves positive work. An aggregate count alone is not live proof.
`/golive` counts only catalogue rows marked as pipeline stages whose state is
`LIVE`, out of all pipeline rows. Console-only rows are shown separately and do
not affect the count. A rehearsal or gate receipt alone never increases it.
A row can be `LIVE` on observed proof and still show a gap under **Next**. An
unavailable-input notice on the page is not a count of zero.

**Evidence is machine-valid metadata, not semantic acceptance.** Review the
underlying work and the meaning of the results with G before treating them as
accepted. The page and ledger command are read-only. For the separate contributor
workflow, see [run_stages tests before review](RUN-STAGES-PRE-REVIEW.md).
