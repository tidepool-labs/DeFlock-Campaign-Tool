# Records follow-up engine

## Drafting into Home (never sends)

`records followup-engine --project P --run-id R` prints counts by pattern/handler,
never letter bodies. Dry run is default; `--apply` adds pending `followup-playbook`
rows to Home's follow-up group. Only engine routes with agency `autonomy: draft`
are eligible. Settings remain off by default and are never changed here.
Replies require one uniquely latest received item for the agency/request and a
resolvable stored message. Missing/ambiguous provenance becomes manual, with its
reason in the summary. Manual coverage gaps list unreconciled queue ids.
New requests deliberately have no thread or recipient: G fills the recipient.
Placement rechecks the immutable vehicle/source evidence; replies never fall back
to unthreaded messages. Existing kinds retain their original placement behavior.
Repeated identities are skipped even after rejection. Changed state, residual
asks or source message creates a new row; G rejects stale older drafts.
Prior produced/pending asks are suppressed; empty letters are not queued.
G edits away placeholders/reviewer notes and signs the exact text on the existing
approval pages. Residue is refused. Sending remains off by default and attended.
The Python API's optional `as_of` must match the run snapshot, not override it.

Latest-source selection compares receipt instants in UTC, not timestamp spelling.
Multiple rows at the latest instant are ambiguous, including equivalent offsets
or zero-fraction spellings and exact duplicates. Invalid non-null receipt dates
for that agency/request become manual (`source_message_date_invalid`), never an
older-message fallback. Missing/null dates are excluded. The selected receipt's
original spelling stays in the evidence; existing drafts and signatures are not
rewritten. New requests remain deliberately unthreaded.

## Reminder text

The clock drafter's `reminder-no-response`, `reminder-promised-date` and
`formal-notice-no-response` bodies contain only the letter: no draft banner,
reviewer ladder, recipient/subject headers, purpose statement or statute citation.
Clock evidence and review context remain outside the outgoing body. Existing
queue rows retain their text and evidence; rerunning does not rewrite them.

Each request register row carries an optional `answer_arrived` (`state` none, `arrived_unrecorded` or
`unknown`, `first_arrival` in UTC, `occurrence_ids`). A bound agency-side delivery on or after the clock start
with no recorded determination makes the next action `record answer` rather than a reminder. Arrival is a
review prompt only: it is not a determination, extension, production, timeliness or fulfillment. Our own sent
copy never counts; a delivery whose direction or time cannot be established gives `unknown` and the issue
`arrival_unresolved`, which needs a person. The deadline, promised date and every other field are unchanged.

The optional `default.signature` in `followup-settings.json` supplies the project
name and public sending address (maximum 200 characters and three newline-separated
lines, no other control characters). Per-agency signatures are not accepted.
An absent signature leaves `[EDIT ME: signature]`; invalid settings turn all
reminders off. A formal notice uses the approved reminder's recorded approval day,
not proof of sending; if unavailable it leaves `[EDIT ME: date of our reminder]`.
G must check this date before approving the exact text. Missing request dates also
remain edit placeholders. The residue and owner-approval/send gates are unchanged.
Letter wording requires G's approval before merge; no setting is changed here.

## Replies that have arrived

`answer_arrived` (see Reminder text) tells the register and the reminder planner whether an agency reply is waiting to be recorded:

- `none`: no unrecorded agency reply, or a determination date is already recorded. Nothing changes.
- `arrived_unrecorded`: a bound agency-side delivery (mail or portal notice) dated on or after the clock start has no recorded determination.
  The next action is `record answer`, with the arrival date in its reason, unless an earlier rule already gives `clarify` (a row issue or a
  clarification status) or the request is recorded closed.
- `unknown`: a delivery's direction or time could not be established (issue `arrival_unresolved`); a person checks the source.

The reminder planner (`followup_clock`) skips every `arrived_unrecorded` and `unknown` row: no `reminder-no-response`,
`reminder-promised-date` or `formal-notice-no-response` draft is queued for it, and the queue summary counts it as `skipped_answer_unrecorded`.
The Home reminders list shows `reply arrived; record it first` (confirmed) or `Reply status unclear; check the source` (unknown) as that
request's reason. A draft already queued is never withdrawn or edited; the hold only stops new drafts. Registers written before
`answer_arrived` existed are unaffected.

What clears it: a person records the determination date in the project's `MASTER-REQUEST-TRACKER.csv` (or the ledger) and the register is
rebuilt; the state becomes `none` and the clock is read again. An extension or a production date alone does not clear it,
and `kit-response` does not feed this register.

## Tracker proposals

`records tracker-proposals --project P --run-id R --out DIR` proposes tracker cells from agency replies already received. It reads the run's
`CURRENT-REQUEST-REGISTER.json` (only rows with `answer_arrived` state `arrived_unrecorded`), `outcomes/received-items.json` and the project's
`MASTER-REQUEST-TRACKER.csv`, all read-only. `DIR` must not exist; its parent must be owned by you, not group- or world-writable, and outside the
project. The command writes a new private directory holding two files and prints `proposals=N conflicts=M | private, review-required; nothing applied`;
a refusal prints `tracker_proposals_<code>` and exits 1. It never writes the live tracker, ledger or stores.

- `MASTER-REQUEST-TRACKER.proposed.csv`: the tracker with only empty cells filled. The command runs `tracker_proposal` on it before publishing.
- `proposals.json` (`records-tracker-update-proposals-v1`, `applied: false`): per proposed cell the row, request id, column, value and sources
  (item id, kind, page, a quote of at most 200 characters), plus every conflict.

Mapping: determination date only from a denial or records item (that item's own receipt day, Pacific); production date only from a records
item; extension `yes` only from an extension item; promised date only from a date the same item states, with page and quote. A filled cell is never
changed. A cell that already holds the same value is skipped. These are listed as conflicts and never applied: `filled_cell_differs`,
`replies_differ`, `date_unavailable`, `date_before_request_start`, `date_after_as_of`, `tracker_binding_ambiguous`, `tracker_row_unresolved`,
`reply_unmatched` and `reply_without_request_id`.

Manual steps, done by a custodian: (1) read `proposals.json`, every proposal and conflict, against the cited item; (2) check the copy again with
`python3 -m campaign_tool.records.tracker_proposal FILE` (optional `--mail-index` and `--expect-rows`, as in `docs/RECORDS-TRACKER-PROPOSAL.md`);
a clean result means well formed, not that the facts are right; (3) apply the cells you accept to the live tracker by hand, with the project
owner's go-ahead, and rebuild the register. Nothing is applied, approved, sent or published by the command.

## Refusal log

`records denials --project P --run-id R [--json]` reads existing run outputs
and approval/send journals; it never refreshes the timeline or writes anything.
Each withheld/exemption-cited ask appears once; a denial event without item rows
appears as `denied_whole`. Quotes are verbatim prefixes (400 characters maximum),
with available page and document hash; absent quotes say `no quote extracted`.
Citation text is the agency's quoted wording, labelled **agency's citation;
UNVERIFIED by us**, never a validation of the exemption. Item text is capped at
160 characters. Dates are recorded receipt dates in Pacific time, or UNKNOWN.
The same-run received-items report supplies receipt timestamps and hashes;
without it those fields remain unknown unless the timeline records the date.
Reply status uses exact source/item coverage, not queue kind alone. Ambiguous
coverage stays `not_drafted`; a current-revision send receipt is needed for `sent`.
Engine-playbook draft coverage is deliberately deferred to DEN-DRAFT-1.
Missing required sources or invalid/stale sources return `state: unavailable`
with a content-free code and exit 2, never an empty successful report.
Private machine reading of the response; review required; not proof of a lawful
or unlawful denial. Nothing drafts, approves, sends or publishes.

## Read-only response state

`records followup-state --project P --run-id R` prints private, review-required JSON;
it writes nothing. It reads the run's timeline and optional response-items report,
plus the same-run register for original asks and clarification status (not in the timeline).
Each request has one display state, flags, open ask numbers, event ids/types/dates
and an exhaustive `item_needs` partition: withheld, audit_no_records,
other_no_records, not_addressed, produced. Ask and response text are never printed.
Closed requests retain the partition; agency closure does not mean all asks were produced.
The explicit mixed-answer case (produced + not addressed + other no-records)
is partial, not no_records; withheld still takes precedence. Register issues yield
unknown after closed/clarification. Superseded events and expired extensions do not
activate routes. Send and reply evidence is compared by calendar day only: an agency
event on or after the latest send day prevents silence, because the order within one day is
unknown in the derived timeline. A same-day reply is therefore classified by its item
ratings (withheld, answered, unknown), not as silence. This tie rule is a conservative
status lead; it is not evidence of delivery chronology, fulfillment, legal timeliness or
authorization to send. An acknowledgment alone never marks asks answered.
These are machine leads for review, not findings or proof that records exist.
No drafts, sends, approvals, publication or ledger writes occur.

## Read-only follow-up routing

`records followup-route --project P --run-id R` prints one private, review-required
JSON row per request; nothing is drafted or written. First-match precedence is:
closed/tier-1, closed, clarification, queued fee answer, silence, exceeded extension
limit (even with a future promise), waiting extension, mixed-answer composition,
answered/waiting/nothing open, unknown. Silence belongs to the existing clock drafter.
Closed tier-1 scope uses a new request; other actionable patterns reply in thread.
Composite patterns retain all residual ask numbers, in denial/audits/no-records/
partial order. Queue kind alone never proves coverage. Machine coverage uses only
exact received-item evidence and is review-required; unmapped or overlapping scope
fails closed to manual `coverage_unreconciled`, with no engine asks or coverage.
An operator may stage `P/followup-coverage/AGENCY_KEY.tsv` with columns
`queue_id`, `ask_numbers`, `confirmed_by`, `note` (tab-separated). After reading the
letter, list comma-separated ask numbers or `none`; this overrides machine mapping.
Duplicate rows, inactive/unknown queue ids or out-of-scope asks yield manual
`coverage_table_invalid`. The table is read-only. Neither ask text nor quotes are
printed. `records_contradict_policy` requires a reviewed finding (see Reviewed contradictions). `vague_answer` is not a
finding: it is an evidence-bound machine question (see Vague answers).

## Read-only follow-up cases

`case_builder.build(route, timeline_request, response_items, asks_rows=...)` returns
a private `records-followup-case-v1` for human review, never a sent request.
`build_for_run(project, run_id, agency_key, request_id)` reads the same-run outputs
and optional `project/agency-asks/AGENCY_KEY.tsv` (the staged asks ledger).
Only `route.asks_for` becomes `asks`; produced items outside that scope are named
separately as `received_asks`, never requested again. Patterns pass verbatim and
unknown/non-identity patterns refuse in CASE. Mixed `extra_patterns` append their
questions once, preserving goals, do-not notes and citation status. Prior ledger
produced/pending text suppresses repeated asks; other statuses remain open.
Missing ledgers carry the R1/R7 manual-check note. No recipient, contact or sender
is inferred. Closed routes use the recorded request id and unsuperseded closure
date; missing values remain edit placeholders. No ledger writes or network calls.

## Proposing and confirming the asks ledger

`records asks-propose --project P --run-id R [--agency K]` is a dry run: it checks the
same run's register, received items and optional response items and prints counts
only (`records-asks-proposal-summary-v1`); it writes nothing. Add `--apply` to create
`P/agency-asks/K.proposed.tsv` (private 0700/0600) in the `case_builder` columns. It
numbers asks exactly as the run's register does (`ask-1`, `ask-2`, ... in document
order) and refuses with a named code (`ASKS_REGISTER_INVALID`, `ASKS_SCOPE_INCOMPLETE`,
`ASKS_EVIDENCE_INVALID`, `ASKS_PROPOSAL_EXISTS`, ...) rather than write a clipped or
incomplete ledger. It never overwrites an existing proposal and never touches `K.tsv`.

`produced` and `pending` appear only when one exact received-item quote names that same
numbered item and says it is attached/enclosed/produced/provided or will be provided;
everything else, including a generic extension or acknowledgment, stays `unknown`. These
are machine proposals for review, not findings.

Confirm step, done by an operator and never by a command: (1) read each proposed row
against the original request and its evidence; (2) correct statuses and `period`, keeping
the original numbering; (3) only then rename `K.proposed.tsv` to `K.tsv`, and only if
`K.tsv` does not already exist. If it does, compare and merge privately; never overwrite
or force. `case_builder` never reads a `.proposed.tsv`, so until the rename the R1/R7
manual-check note stays. There is no auto-confirm. Proposals are private and nothing is
approved, sent or published.

## Proposing and confirming the follow-up coverage table (FLK-REC-7)

`followup_route` settles `coverage_unreconciled` only from an operator-staged
`P/followup-coverage/AGENCY_KEY.tsv` (`queue_id, ask_numbers, confirmed_by, note`). The tool can propose one:

```
records coverage-propose --project P --run-id R            # dry run: counts only, writes nothing
records coverage-propose --project P --run-id R --apply    # creates AGENCY_KEY.proposed.tsv only
```

It reads only the same run's register, timeline, received-items and response-items plus the approval queue, and
reuses the router's own exact item-evidence mapper. A row whose scope maps cleanly gets its ask numbers and the note
`machine mapping; review required`; a row that does not map, or whose numbers overlap another row of the same request,
is left blank with the note `needs a person: unmapped scope` or `needs a person: overlapping scope` (every overlapping
row is blank: no row is chosen as the winner, and `none` is never proposed because it is a person's judgment). Every
`confirmed_by` is empty, so the router never reads a `.proposed.tsv` and refuses a naive rename (`coverage_table_invalid`).
An existing `AGENCY_KEY.tsv` is never opened for writing; an identical proposal is a no-op and a differing one is refused
(`COVERAGE_PROPOSAL_EXISTS`).

Confirm step, done by a person and never by a command: read the whole draft and its evidence for every row, fill each
`ask_numbers` (or write `none` deliberately) and put your label in `confirmed_by`, then rename the file to
`AGENCY_KEY.tsv` only when no confirmed table exists; if one does, compare and merge privately and never overwrite it. If
the run's sources change or a draft is rejected, review fresh sources rather than a stale proposal. Nothing here confirms,
approves, signs, sends or publishes.

## Reviewed contradictions (FLK-REC-2)

`records_contradict_policy` is routed only from a finding with a valid signed `finding-review-v1` approval
(DF2007-05). A model verdict, PUBLIC-OK, an old packet or a review-state string is not approval. The supported
convention is an exact signed **topic** of `contradiction` (the signature binds topic, claim, ordered fact ids and rule
ids; there is no separate kind field). No prose is classified and no existing finding is reclassified.

Pass the analysis root explicitly; without it every route and draft is exactly the legacy output:

```
records followup-route  --project P --run-id R --analysis-root ROOT
records followup-engine --project P --run-id R --analysis-root ROOT            # dry run
records followup-engine --project P --run-id R --analysis-root ROOT --apply    # pending drafts only
```

`followup_findings.load_reviewed_contradictions` reads fresh on every invocation (a revoked or untrusted signature stops
new drafting on the next call; it does not cancel existing drafts). It maps profiles by exact `records_agency_id`,
reuses the finding-review evaluator (checked facts, canonical pages, flags, duplicates) and requires every cited fact to
have a source hash, a positive page and a verbatim quote, and every cited source to join one received item of the same
agency and one request. An unusable root prints `followup_findings_unavailable`.

Route position: after rules 1-7 (closed, clarification, fee, silence, extension keep their first-match output) and before
the residual rules. The cited record must be an answered ask of the request. One eligible finding gives
`reviewed_contradiction` (reply in thread, `asks_for: []`, a `finding` locator of id, hashes and fact ids); several give
manual `contradiction_findings_ambiguous`; an approved finding whose citations cannot be joined gives manual
`contradiction_evidence_unavailable`. Unreviewed, rejected and signature-refused findings are never routed.

The case holds one new, case-local ask per cited fact (numbered 1..N in signed order), quoting that fact; the original
produced asks stay `received_asks` and are never re-requested. Slot `n` lists only the cited record, page and fact; the
retention/sharing slot is filled only when the cited quotes name exactly one of the two words, otherwise it stays an
edit placeholder. The queue row's evidence carries the locator, and its identity includes the finding and evidence
hashes, so a rerun adds nothing and changed evidence adds a distinct draft.

Finding approval is not approval of the draft, a send or a publication. Scheduled root wiring is not part of this unit.

## Vague answers (FLK-REC-4)

A same-run denial that cites an exemption but does not tie it to numbered items gets one private, pending `vague_answer`
reply-in-thread draft asking the agency to tie each cited exemption to the items it withholds and to state the
justification. It is a machine reading, not a finding: no approval, send, placement or publication is wired.

Eligibility (all required, otherwise the route is unchanged): a same-run denial-log entry (`denied_whole`, `withheld` or
`exemption_cited`) with a complete quote (under 400 characters), a positive page, a 64-hex original-document hash and a
section-numbered agency citation (`§ 7922.540`, `Gov. Code section 9999`; a bare "exempt" or "we conducted a search" never
qualifies), and at least one open known ask left after the existing coverage reconciliation. The denial log quotes a
whole denial only from the current hash-bound extraction of its exact source item, read-only (no extraction, no
writes), one complete cited sentence; two distinct candidate sentences stay manual
(`vague_answer_evidence_ambiguous`). Any item reference the agency itself wrote (`item 2`, `items 1, 2 and 3`, `#1`, a
`1.` list heading, including an unknown `item 99`) makes the reply item-level: it never routes `vague_answer` and keeps
its blanket-denial/coverer route. Statute digits are not item numbers.

Precedence: closed/tier-1, closed, clarification and a queued fee answer still win. An eligible vague answer is checked
next and therefore wins over silence, exceeded extension and waiting extension (and over the reviewed-contradiction
branch and the residual rules); for unknown-state requests (no register issues) it also applies. Without eligible
evidence every route is exactly as before. Fully covered scope keeps the existing covered disposition.

The route carries only nontext locators (`vague_answer_evidence`: denial id, page, hash, quote digest); the case builder
and engine re-resolve them against the same-run denial log and fail closed on any mismatch. Statute wording comes from
the campaign-selected law package's written-denial rule (selected by rule identity, never a hardcoded paragraph or the
agency's citation) and is prefixed `DRAFT law-package wording — unreviewed; not a legal finding` while the package is not
reviewed; a missing package or rule refuses before any queue write. The agency's quote, page, document hash and
citation (labelled `agency's citation; UNVERIFIED by us`) are queue evidence only (`evidence.vague_answer`), never in
the letter body. The queue identity binds the evidence and the package wording, so changed evidence is a new draft.

## Agency events import

This is a private, review-required timeline input, not a legal finding or publication.
An operator stages an EVENTS-SCHEMA TSV at `PROJECT/agency-events/AGENCY_KEY.tsv`
and a CSV at `PROJECT/agency-events/request-map.csv` with exactly these columns:
`request_ref,agency_key,request_id`. Map each reference to its timeline request id.
No operator-workspace files are discovered or copied automatically.

Preview with `python3 -m campaign_tool.records events-import --project PROJECT --agency-key AGENCY_KEY`.
Add `--apply` to append mapped events to `manual-events.jsonl` (0600).
The default is dry run: no journal or lock is created.
The JSON report lists mapped counts by timeline type, skipped count, unmapped and
refused event ids, added count and already-present count. Unmapped rows block all
writes and exit 1 unless `--allow-unmapped` explicitly permits only mapped rows.
Malformed input or refused rows always block writes, even with that option.
Row text is never printed; invalid event ids are reported by row number.

Computed clocks, fetched-link/system/contract events, request-received events,
context references and references ending `(third party)` are skipped.
Fixed type mappings include clarification/reconsideration sent as our follow-up
and clarification requested as its own type (no stage change).
Promised dates retain their row date as `new_date`. Dates are taken as written
(YYYY-MM-DD, Pacific per EVENTS-SCHEMA); summaries are clipped to 200 characters.
Every field must be free of angle brackets. Unknown types and missing mappings
are listed as unmapped, never guessed.

Ids are `tsv-AGENCY_KEY-EVENT_ID`. Re-import skips ids already in the journal,
even if the staged text changed; it never rewrites earlier entries. The shared
console append helper retains its private lock, bounded journal and append-only
behavior. Console forms still enforce tickets, freshness and conflicting retries.
Refresh the derived run timeline through the normal records output stage to see
imports. Ledger events may also appear: distinct sources remain visible.
Nothing sends, fetches, approves, publishes or writes the coordinator ledger.

## Deadline calculator

`records deadlines --project P --run-id R [--json]` reads only that run's register
and the shipped California law package. It lists every request, including closed
ones: received (DATED recorded receipt, or INFERRED send-date proxy), determination
due and extension limit (COMPUTED), and agency-promised date (DATED). Missing dates
are UNKNOWN. The limit applies only with a lawful written extension notice; the
output separately says whether an extension was recorded. This is a determination
reminder, not a production deadline.

Both computed dates carry the package rule's Gov. Code § 7922.535 citation,
primary-source URL and `jurisdictions/us-ca/package.json sha256 <hex>` file hash.
The register's normalized `request_date` and receipt/proxy basis supply the start.
Register dates are checked against `law.deadline`, allowing only the register's
weekend roll. Package-rule differences or stale computed dates are visible issues
and labelled UNVERIFIED, never silently COMPUTED. Existing register issues remain.
The table and JSON are private and review-required; nothing is queued or written.
The package's `needs_attorney_review` rule status is unchanged.

Provisional reminder date: receipt, holiday last-day rules and extension-notice requirements are not checked. Not a legal finding.

## Clocks and the due rule

`records followups clocks --project P --run-id R` prints a read-only JSON table
for every request in that run, including closed requests. It shows statutory
base/extended/due dates and state, the agency's promise, follow-up due dates,
formal-notice due date, extension-limit flag (null without a matching timeline),
autonomy and issues. Rows sort by earliest date on or after the run's `as_of`,
then agency and request; rows without upcoming dates sort last.

`silence_after_days` is an integer from 0 through 30 in the settings `default`
or per-agency entry (the agency overrides the default). Its default is 0:
`follow-up due = statutory due or unsatisfied promised date + 1 + silence_after_days`
calendar days. Thus 0 retains the first-day-after behaviour; 5 waits until D+6.
Closed requests and satisfied clocks have no follow-up due. Issues or autonomy
`off` still prevent drafts. Invalid settings fail closed to all off.

Formal-notice timing remains the approved reminder's `updated_at` day plus
`notice_after_days`; stale-date reminders never escalate a replacement clock.
These dates are **on our records; not a legal finding**. Drafting never sends;
all drafts remain subject to human review. `rows` keeps its existing display.

### Holiday list (`PROJECT/holidays.csv`)

`PROJECT/holidays.csv` is an optional file the custodian keeps; no holiday list ships with the tool or a law package. When it is present and
valid the register moves the 10-day determination date and the extension limit past listed holidays as well as weekends. Header exactly `date,name,source`; each row is a `YYYY-MM-DD` date, a non-empty name and a
non-empty source, with no repeated date, at most 1000 rows and 64 KiB. One bad row makes the whole file invalid: none of it is used, only
weekends roll, the register adds the gap `holiday_list_invalid` and the same issue on every request row, and the reminder planner skips those rows
until the file is fixed. With a file, each register row and the register report carry `holiday_list` (`used` or `invalid`) and a 12-character
hash prefix of the file; without one nothing is added and the output is unchanged. `records deadlines` names the custodian's list in its caveat.
Dates stay provisional reminders: receipt and extension-notice requirements are not checked and the list is the custodian's, not the law
package's. Not a legal finding.

### What stays human

Recording an answer, checking and applying a tracker proposal, keeping `holidays.csv`, and approving and sending every reminder are people's
steps. The tool sends nothing and publishes nothing; sends and publication stay G-approved.
