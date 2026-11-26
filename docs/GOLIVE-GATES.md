# Attended go-live gates

**GO is evidence, not permission: the attended step still needs its owner and G as the plan says**.

## Use at each sitting

1. **Capture.** Save fresh, time-stamped readbacks in a private working directory, outside Git. Use ordinary copied files, not symlinks, and never give the gate a live records path. Save the validated `spec()` result from the linked gate pack as `<gate_id>.spec.json` in `specs/`; use the approved pins and thresholds, not values chosen to make the captures pass. Names below are relative to that working directory; substitute your own paths and run the commands with the engine checkout importable (for example, use absolute capture paths from the checkout).
   - **`evidence`** is a copied go-live evidence directory, not a folder of raw receipts. Take a fresh snapshot of the recorded rehearsal and gate JSON rows, with their original filenames, and keep the source receipts for readback. Independently confirm the exact predecessor `<gate_id>.json`, its `gate_id`, time and scope: the evaluator's prior-gate check matches a source-name prefix. Check rehearsal qualification and source receipt time too; the gate checks the recorded row's age. Don't re-record old evidence to freshen it. The default age is 14 days for the Sitting 6 and lane packs; use any narrower approved limit and the actual sitting's requirements.
   - **`pair`** is the JSON output of the `golive_pair` byte check on the copied wrapper and `LIVE.json` for the engine that would perform the step. Read back the successful pair verdict against the approved pins and a single full `engine-` SHA; keep the exact bytes. Except for the switch, the gate's rehearsal check uses the reported engine, not a verification that the pair report binds the captures.
   - **`disk` / `space`** is a fresh available-space capture for the plan's target filesystem: save only the `Avail` header and a single available-byte count on the next line. Record the target and measurement time separately. Don't use a rounded human-readable value.
2. **Run the gate.** Use the matching command below, with an existing private `receipts/` directory that has no file for this attempt. Window checks use the current clock in Pacific time. Use `--help` to inspect the interface without evaluating evidence.
3. **Record the receipt before acting.** Every command saves a new `receipts/<gate_id>.json` (no overwrite), with the stage IDs, UTC evaluation time, verdict, check states and reason codes, and SHA-256 hashes of readable inputs actually consulted. The `evidence` hash is over the validated rows, not directory names or raw JSON formatting. Keep the receipt, exact spec, capture set and rollback reference together in the private sitting record. The evidence owner can record either verdict in an approved evidence copy with the recorder below; it writes metadata for each declared stage, not live acceptance. Keep the exact receipt basename for downstream checks. Never point this example at the canonical ledger.

   ```sh
   python3 -B -m campaign_tool.records.golive_record gate receipts/GATE_ID.json --evidence evidence
   ```

   Substitute the actual gate ID. Gate exit `0` means all checks passed, `1` is `NO-GO` (including unknown checks), and `2` is an invocation, spec or output error: stop, even if an old receipt exists. The recorder can return `1` after recording a `NO-GO`; preserve that evidence, not just passes.
4. **Act or stop.** Only the owner may perform the exact planned action, after a fresh `GO`, all independent checks and protected holds are resolved, and G is present as required. Nothing here runs, approves, schedules or proves completion of an attended action. On `NO-GO` or error, do not start. If an attended action fails or its readback disagrees after it starts, stop, follow the approved rollback for that step, verify the restored state, and record the outcome. Do not continue to the next step on the strength of the preflight receipt.

## `s5b-backup` — D2-A pre-switch backup (19)

**Capture:** `quiescence` is the operator's dated readback that the plan's writers are quiescent; the gate only checks that the copy is readable. Take `disk` for the backup destination (40 GB decimal minimum), `pair` for the **pre-switch** engine and `evidence` with its qualified stage 19 rehearsal (at most seven days old).

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/s5b-backup.spec.json --out receipts \
  --input quiescence=captures/quiescence.txt --input disk=captures/disk.txt \
  --input pair=captures/pair.json --input evidence=evidence
```

**Receipt:** `s5b-backup.json`. GO covers the D2-A preflight, not a completed or verified backup, and does not cover Part 2 of the backup work. Confirm quiescence independently and verify the backup under the plan before relying on it.

## `s5-engine-switch` — Sitting 5 engine switch (15)

**Capture:** `wrapper` and `live` are byte-for-byte copies of the candidate `run-records.sh` and `LIVE.json`; use the approved SHA-256 pins as `wrapper` and `live` in the [Sitting 5 spec](../campaign_tool/records/golive_gate_specs_s5.py). Take `disk` for the switch target (40 GB decimal minimum), `pair` from those very copies and `evidence` with the qualified stage 15 rehearsal (at most seven days old) for that engine. Confirm today's approved booking separately: the recurring Thursday 10:00–10:10 Pacific check is not a date or a rebooking.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/s5-engine-switch.spec.json --out receipts \
  --input wrapper=captures/run-records.sh --input live=captures/LIVE.json \
  --input disk=captures/disk.txt --input pair=captures/pair.json --input evidence=evidence
```

**Receipt:** `s5-engine-switch.json`. GO binds rehearsal and pair to the candidate bytes inside the checked window; it neither lifts the switch hold nor proves that the switch or the scheduled run succeeded. Have the approved old pair and its readback ready for rollback.

## `s8-units` — arrival and monitoring units (3, 18)

**Capture:** `pair` from the intended switched engine; `evidence` with the same-sitting engine-switch GO (gate limit: one day) and qualified rehearsals for stages 3 and 18 on that engine (at most seven days old). Read back the switched pair and booked sitting independently.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/s8-units.spec.json --out receipts \
  --input pair=captures/pair.json --input evidence=evidence
```

**Receipt:** `s8-units.json`. GO is predecessor and rehearsal evidence, not proof a unit was installed, started or triggered.

## `s6a-trust-mount` — Sitting 6 Part A (20)

**Capture:** `run_records` and `live_json` are byte copies of the approved running wrapper and `LIVE.json`. Supply `run_records_sha256`, `live_json_sha256` and `disk_floor_gib` to the [Sitting 6 spec](../campaign_tool/records/golive_gate_specs_s6.py). Derive the floor from the fresh attended space budget, including pending backup when applicable, without rounding down. Capture `space` on that filesystem, `pair` for those same files and `evidence` with engine-switch GO and the matching stage 20 rehearsal.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/s6a-trust-mount.spec.json --out receipts \
  --input run_records=captures/run-records.sh --input live_json=captures/LIVE.json \
  --input space=captures/space.txt --input pair=captures/pair.json --input evidence=evidence
```

**Receipt:** `s6a-trust-mount.json`. GO checks byte pins and the supplied floor, not the pair verdict or the budget's age. The owner still checks the approved kit and anchor metadata, clean scheduled run, retained container, booked window, quiescence, and the fresh budget and backup-done recheck. GO is not the A12 `anchor ok` readback.

## `s6b-decisions` — Sitting 6 Part B (A5, A6)

**Capture:** `pair` from the intended signing engine; `evidence` with Part A gate GO and qualified A5 and A6 rehearsals on that engine. Separately capture the attended A12 `anchor ok` readback and B2 recheck. Confirm Q restored, the approved C9 kit and base, active loopback console without the drop-in, an eligible item, slot clearance and the signer present.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/s6b-decisions.spec.json --out receipts \
  --input pair=captures/pair.json --input evidence=evidence
```

**Receipt:** `s6b-decisions.json`. GO does not prove the anchor is mounted, set the decisions directory, or sign or authorize the first yes. Do not substitute Part A preflight or rehearsal for the actual anchor readback.

## `s10-console` — new loopback console (A3)

**Capture:** `pair` from the intended engine; `evidence` with engine-switch GO and a qualified A3 rehearsal on that engine. Separately capture S9's qualifying real draft and fresh attended precheck.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/s10-console.spec.json --out receipts \
  --input pair=captures/pair.json --input evidence=evidence
```

**Receipt:** `s10-console.json`. GO does not prove S9 or install the console. Preserve the plan's loopback fallback; public access is a separate step.

## `c6-findings-on` — findings lane (16)

**Capture:** With the [lane specs](../campaign_tool/records/golive_gate_specs_lanes.py), copy `pair` for the intended findings engine and `evidence` with Part A gate GO and the matching qualified stage 16 rehearsal. Confirm the required live prerequisite readbacks separately.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/c6-findings-on.spec.json --out receipts \
  --input pair=captures/pair.json --input evidence=evidence
```

**Receipt:** `c6-findings-on.json`. GO neither enables the lane nor accepts or publishes findings.

## `l17-packets` — packets lane (17)

**Capture:** `pair` for the intended packets engine; `evidence` with findings-lane GO and the matching qualified stage 17 rehearsal. Check the intended staging target and approvals under the plan.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/l17-packets.spec.json --out receipts \
  --input pair=captures/pair.json --input evidence=evidence
```

**Receipt:** `l17-packets.json`. GO neither enables the lane nor authorizes release or publication of any packet.

## `c5-portal-apply` — portal apply (4)

**Capture:** `hold_release` must be a copy of the actual operator/G hold-release record for the portal admin-approved-list hold. If that record is missing, invalid or out of scope, stop; a placeholder is not a release. Copy `evidence` with engine-switch GO. Check the preceding scheduled run separately: the daily 14:00–18:00 Pacific window is only a clock check.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/c5-portal-apply.spec.json --out receipts \
  --input hold_release=captures/hold-release.json --input evidence=evidence
```

**Receipt:** `c5-portal-apply.json`. GO only checks that the release copy is readable, the clock is in range and the prior gate qualifies. It does not authenticate the release, clear the hold, modify the root-owned allowlist or permit downloads. Verify the release with its owner and leave the hold in place without that verification.

## `a4-hostname` — public admin hostname (A4)

**Capture:** `pair` for the engine the installed console is meant to use; `evidence` with S10 GO and the matching qualified **A3** rehearsal. Read back the actual loopback console and separately confirm the approved hostname and access scope with the owner.

```sh
python3 -B -m campaign_tool.records.golive_gate --spec specs/a4-hostname.spec.json --out receipts \
  --input pair=captures/pair.json --input evidence=evidence
```

**Receipt:** `a4-hostname.json`. GO is not proof the console was installed or that the public endpoint enforces access. No DNS, tunnel or Access change is authorized by it.
