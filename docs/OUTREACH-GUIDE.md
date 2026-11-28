# Outreach volunteer guide

This guide is for the volunteer who handles press, social media and the newsletter. It goes from a finding that is already published to private drafts, then to a record of coverage you actually received, in the order you do the work. Every step names its command as it exists today and links the reference section in [PUBLICATION.md](PUBLICATION.md), [NEWSLETTER.md](NEWSLETTER.md), [MEETINGS.md](MEETINGS.md) or [SITE-CONTENT.md](SITE-CONTENT.md), which stay the reference; nothing is moved out of them. Writing and reviewing findings is somebody else's job: see the [findings guide](FINDINGS-GUIDE.md) and the [review guide](REVIEW-GUIDE.md).

**These offline commands post, send and publish nothing. A draft is not approval to use it.** That sentence is about the commands in this guide. The project also contains separate hosted outbound executors (for example the newsletter send in [NEWSLETTER.md](NEWSLETTER.md#approve--send) and the per-post social posting in [PUBLICATION.md](PUBLICATION.md), section "Posting one approved social draft"); this guide does not run them, and nothing here authorises them.

## Before you start

- **`D`** is an existing campaign folder outside this repository, made by `init` and `content init`, whose content passes `content-check` and whose `site.json` has a `base_url`. In the commands below it is written `$CAMPAIGN`.
- **`ID`** is the exact id of one published finding, such as `fnd_0000000000000001` (not a slug and not a path). It is written `$FINDING_ID`.
- Run every command from the engine checkout (this repository). Quote paths that contain spaces.
- Every `python3 -m campaign_tool ...` command below also accepts `--campaign NAME` (a registered campaign) instead of `--directory`; see [MULTI-CAMPAIGN.md](MULTI-CAMPAIGN.md#--campaign-name). The newsletter command (`python3 -m campaign_tool.newsletter`) takes `--directory` only.
- Drafts are written only under the campaign's private `kit/` folder. Nothing is written to `content/` or `public/`.
- Putting anything on the internet, mailing it, printing it or handing it out is a separate decision for a person, outside this tool.

Task checklist: read the finding, social posts, a thread, meeting, outcome and event posts, press release and letter to the editor, flyer, newsletter draft, the newsletter approval card (described only), record coverage, final review.

## What you may say

Outreach says only what a published finding says, with its citations, its limitations and every correction it carries. Do not add facts, motives or conclusions, and do not claim a legal violation. The tool's legal-claim guard only prints alerts: an alert needs a human, and a clean run does not certify accuracy.

Never use as outreach input: a draft or preview that is not published, review receipts, raw originals, contact lists, personal data, or a finding that has been withdrawn. An information gap is not proof of misconduct.

## 1. Read the finding first

Read the published finding page: its title and summary, its sources and their locators, its classification and confidence, its limitations and each correction. Reference: [states](PUBLICATION.md#states), [corrections and withdrawal](PUBLICATION.md#corrections-and-withdrawal), [before publishing: evidence check](PUBLICATION.md#before-publishing-evidence-check).

```sh
python3 -m campaign_tool content-check --directory "$CAMPAIGN"
```

`content-check` checks structure. It does not check that anything is true, and it is not legal approval. If the finding is not yet published, stop and ask its author; publishing is not your step.

## 2. Social posts, one draft per platform

```sh
python3 -m campaign_tool social --directory "$CAMPAIGN" --platform x --platform bluesky --platform mastodon
```

Reference: [social drafts per platform](PUBLICATION.md#social-drafts-per-platform-never-posted) and [social drafts](PUBLICATION.md#social-drafts-never-posted). Each platform counts length its own way (X 280 weighted with links as 23, Bluesky 300 characters with the full link, Mastodon 500). Only the title is shortened. If a draft cannot fit, the whole run stops and names the platform. Drafts are in `kit/social/PLATFORM/`.

- Platform drafts always cover **every** published finding, not just `ID`. `--finding` belongs to a thread (task 3): `social --platform bluesky --finding ID` without `--thread` is refused with `--finding needs --thread`.
- A draft you edited is kept; the fresh text goes to `kit/social/PLATFORM.suggested/`. Stale drafts (finding no longer published) are listed, never deleted.
- `social --directory D --check` (add `--text` for a report grouped by folder) reads every private draft folder it finds: `kit/social/`, each platform folder, the meeting, outcome, thread and event folders and their `.suggested` copies. It writes nothing and exits 3 on any problem. It also checks `kit/social/` itself, which only `social` without `--platform` fills, so a campaign that made only platform drafts sees each finding there as `missing`. `--check` cannot be combined with `--platform`, `--meetings`, `--outcomes`, `--events` or `--thread`. Reference: [checking every draft folder](PUBLICATION.md#checking-every-draft-folder-read-only).

## 3. A thread for one finding

```sh
python3 -m campaign_tool social --directory "$CAMPAIGN" --thread --finding "$FINDING_ID" --platform bluesky
```

Reference: [social threads for findings](PUBLICATION.md#social-threads-for-findings-never-posted). Output: `kit/social/threads/bluesky/SLUG.md`. The posts are numbered `(n/N)`: the title and summary, one post per public source page, the limitations, then the latest correction. A thread has at most ten posts. If a post does not fit or an eleventh would be needed, nothing is written; shorten the published text, never leave out the limitations or the correction. `--thread` needs exactly one `--finding` and cannot be combined with `--check`, `--meetings`, `--outcomes`, `--events` or `--within-days`. An edited draft is kept and the new text goes to `kit/social/threads/bluesky.suggested/`.

## 4. Posts for meetings, meeting outcomes and campaign events

```sh
python3 -m campaign_tool social --directory "$CAMPAIGN" --meetings --within-days 14 --platform bluesky
python3 -m campaign_tool social --directory "$CAMPAIGN" --outcomes --within-days 14 --platform bluesky
python3 -m campaign_tool social --directory "$CAMPAIGN" --events --within-days 14 --platform bluesky
```

Reference: [social drafts for meetings](PUBLICATION.md#social-drafts-for-meetings-never-posted) and [participation (organizer-verified)](MEETINGS.md#participation-organizer-verified). With `--meetings`, only scheduled, upcoming staged meetings with verified participation get a draft (in `kit/social/meetings/bluesky/`); cancelled, past, unverified and unreadable ones are counted under `skipped`. An empty result is normal. Times use the meeting's own UTC offset. Check the official deadline and any cancellation again before you reuse a draft. This command does not find or stage meetings, register speakers or submit comments.

With `--outcomes` ([social drafts for outcomes](PUBLICATION.md#social-drafts-for-outcomes-never-posted)), each staged meeting whose organizer-recorded outcome is dated in the last N calendar days, today included, gets a draft in `kit/social/outcomes/bluesky/`. The result and any vote line are quoted exactly as recorded; the tool derives no tally and infers no decision. Meetings without an outcome, cancelled ones and ones outside the window are counted under `skipped`. `--outcomes` cannot be combined with `--meetings` or `--check`.

With `--events` ([social drafts for campaign events](PUBLICATION.md#social-drafts-for-campaign-events-never-posted)), each campaign event in `content/events.json` that starts within N days gets a draft in `kit/social/events/bluesky/` linking to its place on the events page. An event that has already started counts as past. The description, registration link and accessibility note are never used. `--events` cannot be combined with `--meetings`, `--outcomes`, `--check` or `--thread`.

In all three, `--within-days` is 1 to 60 (default 14), an edited draft is kept with the fresh text in the `.suggested` folder, and drafts that no longer apply are listed as `stale`, never removed.

## Supporting organizations and co-signers (private, read-only)

Before a meeting comment names supporting organizations, look up their ids and what was recorded:

```sh
python3 -m campaign_tool supporters list --directory "$CAMPAIGN"
python3 -m campaign_tool supporters list --directory "$CAMPAIGN" --json
```

Reference: [supporting organizations](SITE-CONTENT.md#supporting-organizations-contentsupportersjson-optional). Each line shows an organization's id, kind, name and joined date, whether its private consent record exists, its co-sign grant (`any`, meeting ids or `none`) with the date, and the staged meetings that already name it; a private record whose public entry is gone is one `private record without public entry` line. `none recorded` is a normal result. The output is private: keep it out of drafts and posts. It writes nothing and prints no hashes or correspondence.

A listing is not co-sign approval, and the staged meetings it names can be past, cancelled or older than a grant that was since narrowed or cleared. `content-check` (task 1) is the gate before any comment kit is built or reused. Recording or changing consent (`supporters consent`) and removing an organization (`supporters remove`) are separate tasks for the person authorised to record consent, described in SITE-CONTENT.md, not steps of this guide.

## 5. Press release and letter to the editor

```sh
python3 -m campaign_tool press draft --directory "$CAMPAIGN" --finding "$FINDING_ID"
python3 -m campaign_tool press letter --directory "$CAMPAIGN" --finding "$FINDING_ID" --words 250 --outlet "Example News"
```

Reference: [press drafts](PUBLICATION.md#press-drafts) and [letters to the editor](PUBLICATION.md#letters-to-the-editor). The release goes to `kit/press/` and the letter to `kit/press/letters/`. The release is refused for an unpublished or unknown finding or a campaign without `base_url` (the letter just leaves out the finding link), and an edited copy is kept while the new text goes to a `.suggested` folder.

A letter takes one to three findings and 150 to 400 words (default 250). If the words do not fit, whole trailing paragraphs are dropped and the output lists the retained and dropped finding ids and the word count; a first paragraph that cannot fit is refused. `[YOUR NAME]` and `[YOUR CITY]` are left for you. The outlet name is plain text for the salutation: the tool looks up no address, submits nothing and implies no endorsement. Keep the `sent: false` marker and read the file in `kit/press/letters/` before use.

Before you reuse an older release or letter, check it against the published findings ([check existing press drafts](PUBLICATION.md#check-existing-press-drafts)):

```sh
python3 -m campaign_tool press check --directory "$CAMPAIGN"
```

It reads the drafts in `kit/press/`, `kit/press/letters/` and their `.suggested` folders and gives each one a status. `stale` means, for example, a cited finding withdrawn, no longer published or corrected after the date in the file name, or a meeting named in a letter's closing cancelled or past. `not checked` means the tool could not check it: an ordinary edited copy whose first `<!-- DRAFT: ... -->` line was removed, an unrecognized letter closing, or finding or meeting files it cannot trust. A symlink, special file or unreadable file is named `not regular` or `unreadable`. It exits 0 only when every draft is `ok` and 3 otherwise. Anything other than `ok` needs review by hand. Even `ok` is not clearance to reuse a draft: it only means these checks passed (it does not notice a rescheduled meeting, a changed summary or a new finding the draft does not cite). It rewrites, sends and publishes nothing.

## 6. The flyer

```sh
python3 -m campaign_tool flyer --directory "$CAMPAIGN" --max-findings 3
```

Reference: [campaign flyer](PUBLICATION.md#campaign-flyer-never-printed-or-posted). Output: `kit/flyer/flyer.html` and `flyer.txt`, not `public/`. An existing flyer is refused. Look at it first; `--replace` overwrites your edits and is not the edit-keeping protocol of the social drafts. For one upcoming campaign event instead, `flyer --directory "$CAMPAIGN" --event EVENT_ID` writes `kit/flyer/event-EVENT_ID.html` and `.txt` ([event flyer](PUBLICATION.md#event-flyer)); it cannot be combined with `--max-findings`, and an unknown event or one that starts before 00:00 UTC today is refused. That cutoff is the start of the current UTC day, not the current time (task 4 uses the current time, so there an event that has already started is past): an event that started, or even ended, earlier today in UTC still gets a flyer. Check that the event is still upcoming before you use the flyer. Printing and handing out are outside this guide.

## 7. The newsletter draft

```sh
python3 -m campaign_tool.newsletter --directory "$CAMPAIGN" --from-content
```

Reference: [drafts](NEWSLETTER.md#drafts) and [offline sends](NEWSLETTER.md#offline-sends). Output: `kit/newsletter-draft.html`, `.txt` and `.json`. Read which findings were left out (a correction or the date window), every link, the unsubscribe placeholder and any meeting details, which must be verified. `--since YYYY-MM-DD` overrides the private last-send boundary. `--site PUBLIC_FOLDER` checks links only if that built folder already exists; it never shows that anything was delivered or published. Copy edits you want to keep somewhere private before you run it again. A draft run cannot be recorded as a sent issue, and this guide never records one.

Do not reuse a draft made earlier as it is. The draft is built only from the campaign's staged content, never from the official agenda, so rebuilding it cannot pick up a change the organizer has not entered. Before you use any draft, new or rebuilt, check every meeting's current participation instructions and deadlines against the official agenda, check every event detail, and read the draft text yourself. If anything differs, do not use the draft: ask the organizer to correct the staged meeting or event first, then run the draft command above again and check the new draft the same way.

`python3 -m campaign_tool.newsletter --directory "$CAMPAIGN" --check` is only an alert for some changes since a draft was made ([draft provenance and `--check`](NEWSLETTER.md#draft-provenance-and---check-df1622-13)), not a check that a draft can be reused. It takes no other flag except `--json` and writes nothing. It does not read the rendered draft or the official agenda, and it compares only these facts saved with the draft. For each finding in the draft: whether it is still published, its slug, its publication date and its corrections; plus any finding published or corrected in the draft's date window that the draft leaves out. For each meeting in the draft: whether it was removed or cancelled, its start time, its agenda link and whether it has already started; plus any upcoming meeting the draft leaves out. Nothing else is compared: not a finding's title, summary, classification or confidence, not a meeting's body, agenda item or participation instructions (in person, remote, written comment and its deadline, speaker time), and not campaign events, requests, records, the map or decisions, even though the draft can list events. The match line's own words "findings, corrections and meetings" mean only the facts listed here, so a changed written-comment deadline, event venue or event time still prints the match line. Exit 0 prints `draft matches current content ...`; exit 3 lists each difference with the command to rebuild. A draft made without `--from-content` cannot be checked and also exits 3.

## 8. The newsletter approval card (described, not run)

There is no command here that creates a card, and the draft above creates none. In the hosted workspace a person with access can propose a done draft; that creates a `send_newsletter` card carrying the subject, HTML, text and list for explicit human approval ([approve and send](NEWSLETTER.md#approve--send)). Approval and execution are different steps, and delivery can be ambiguous ([ambiguous delivery and recovery](NEWSLETTER.md#ambiguous-delivery-and-recovery), [status](STATUS.md)). That is hosted work outside this guide: it needs no credentials, provider or list ids or subscriber export from you, and neither a generated draft nor a passing test is authorisation to send.

## 9. Record coverage you received

Only after an article actually exists:

```sh
python3 -m campaign_tool content coverage add --directory "$CAMPAIGN" --outlet "Example News" --headline "Published headline" --url https://news.example.test/story --published-on "$PUBLISHED_ON" --finding "$FINDING_ID" --kind news
python3 -m campaign_tool content coverage remove --directory "$CAMPAIGN" --url https://news.example.test/story
```

Reference: [In the news](SITE-CONTENT.md#in-the-news-contentcoveragejson-optional). Type the outlet and headline exactly as published. The link must be new (a duplicate is refused), at most three published finding ids may be named, and `--kind` is `news`, `opinion`, `broadcast` or `other`. The tool does not fetch, copy or endorse the article. `remove` deletes your local record, not the article. `news.html` appears only after a later, separately authorised site build; `coverage add` builds and publishes nothing. Never record an invented example as real coverage.

## 10. What the tool never does

| It never | So |
| --- | --- |
| posts, tweets or toots | you post by hand, if the campaign decides to |
| mails press, editors or subscribers | you send by hand, if the campaign decides to |
| prints or distributes the flyer | people do |
| publishes a page, a newsletter or coverage | a separate build and deploy, authorised by others |
| looks up recipients, handles accounts or credentials | none are needed |
| decides that wording is accurate or lawful | people review it |

## Worked example (synthetic)

Everything here is invented: the Cedar Police Department and the fixture finding. Make a disposable copy and never edit the tracked example:

```sh
cp -R examples/fictional-campaign "$CAMPAIGN"
```

The copy has one published finding: `fnd_0000000000000001`, slug `cedar-policy-posted`, "Cedar Police produced a written ALPR usage policy (fictional)", a correction dated 2026-02-03, one limitation, and a `base_url` of `https://cedar-records.example.invalid`. Set `FINDING_ID=fnd_0000000000000001`. Then run tasks 1 to 7 as written above, each with `--directory "$CAMPAIGN"`. Expect, and open to read:

- `kit/social/x/`, `bluesky/` and `mastodon/` each hold `cedar-policy-posted.md`; the draft keeps the correction date.
- `kit/social/threads/bluesky/cedar-policy-posted.md` is a short numbered thread: the finding, the limitation and the correction.
- `kit/press/` holds the release and `kit/press/letters/` the letter, both marked not sent, with `[YOUR NAME]` left blank.
- `kit/flyer/flyer.html` and `flyer.txt`; `kit/newsletter-draft.*`.
- `press check` lists the release and the letter as `ok`, and `newsletter --check` prints that the draft matches current content.
- `social --check --text` lists the platform and thread drafts as `ok` but exits 3, because task 2 used `--platform` and so `kit/social/` itself has no draft: the finding shows there as `missing`. Running `social --directory "$CAMPAIGN"` once without `--platform` clears it.
- The meeting command may report that the example's only meeting is past and write no draft (only its private `.written.json` and `.check.json` records); that is a valid result. The outcome and event commands also write no draft for this example.
- `content/` and `public/` are byte for byte unchanged. No coverage exists for this example, so there is nothing to record in task 9: the commands there are shown only for a real article.

## Before anything leaves your hands

Go channel by channel. Is the published wording, link and correction current? Is any private identifier present? Do you understand the length and which paragraphs the letter kept? Were meeting deadlines rechecked on the official page? Has the campaign's owner reviewed the draft? Using it outside this tool is a person's separate decision.
