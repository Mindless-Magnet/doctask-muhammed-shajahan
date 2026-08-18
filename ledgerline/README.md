# Ledgerline

An agentic system that owns a vendor document file end to end. Contracts, amendments, SOWs, rate
cards, purchase orders and invoices go in. One grounded Vendor Obligation Register comes out, every
field citing the exact span it came from. New documents patch only what they affect and the rest is
proven byte-identical. A human approves or rejects every change, item by item, before anything
commits.

Built for the SuperDocs Round 2 engineering task (Task 1).

## Run it

```bash
make up
```

Brings up Postgres, migrates, starts the API, two workers and the review interface, then generates a
synthetic vendor file and loads it.

- **Review interface** http://localhost:5173 — the queue a person actually works in
- **API docs** http://localhost:8000/docs

```bash
make test      # 110 tests, no AWS account, no key, no network
make test-pg   # adds 4 dialect-specific concurrency tests, needs the compose database
make web       # the interface with hot reload, against an API you are already running
```

### Running it without an AWS account

`make up` defaults to `replay`: the system serves model responses from committed fixtures in
`tests/fixtures/llm/` and never calls a live model. A reviewer clones, runs `make up`, and gets a
genuine run — real recorded model output, real verification, real decisions — with no AWS account
and no spend. That is the same mechanism the test suite uses.

If those fixtures are missing, the error says so and names the fix rather than failing obscurely.

To record them (needs credentials, and only has to be done once per corpus or prompt change):

```bash
make demo-fixtures       # records against real Bedrock, then commit tests/fixtures/llm/
```

To call the model live instead, set `LEDGERLINE_MODEL_CLIENT=bedrock` in `.env`.

### Credentials

Model calls need AWS. Compose passes `AWS_ACCESS_KEY_ID` and friends if you have them exported, and
also mounts `~/.aws` read-only into the containers, which is where `aws configure --profile
ledgerline` writes them. Set `AWS_PROFILE=ledgerline` in `.env` if that is the profile you used.
Nothing is baked into an image.

If a run fails, the reason is shown in the interface and returned by `GET /runs/{id}` and MCP
`get_run_status`. Missing credentials look like a `NoCredentialsError` or an `AccessDenied` naming
the model ARN.

`make up` is safe to run twice: seeding reuses an existing pile and skips documents already
present, because a setup command that only works the first time does not work.

Everything after that happens in the interface — create a pile, drop documents in, start a run,
review, commit. Nothing needs a terminal. The CLI and the watched folder remain for the paths a
person would not drive by hand:

```bash
ledgerline watch --pile northwind --directory ./inbox
```

## The ten behaviours, and where each is proven

| # | Behaviour | Implementation | Test |
|---|---|---|---|
| 1 | Visible stages, decisions that change the path | `agent/graph.py`, `web/src/StageTimeline.jsx` | `test_nothing_confidently_classified_routes_straight_to_the_gate`, `test_a_value_not_in_its_span_is_retried_then_recorded_as_unsupported`, `test_every_surface_shows_what_each_stage_decided`, `test_the_decision_log_records_the_reroute_not_just_the_stage` |
| 2 | Survives being stopped | `service.execute_run` | `test_a_killed_run_resumes_without_losing_or_repeating_work` (real SIGKILL to a child process), `test_a_resumed_run_reaches_the_same_result_as_an_uninterrupted_one` |
| 3 | A human holds the gate, item by item | `service.decide_items`, `web/` | `test_rejecting_one_item_leaves_the_rest_alone` |
| 3b | Stays alive: focused updates, proven untouched | `watcher.py`, `register/reconcile.py` | `test_a_new_invoice_costs_an_update_not_a_rerun`, `test_the_second_run_reproposes_nothing_it_already_committed`, `test_a_contradicting_source_raises_a_conflict_and_overwrites_nothing`, `test_the_ledger_answers_what_changed_when_and_because_of_which_source` |
| 4 | A machine can drive it, ingestion and approval included | `mcp/server.py` | `test_a_program_drives_the_entire_flow_through_mcp_alone`, `test_the_mcp_server_can_ingest_documents_too` |
| 5 | Never bluffs | `verification.py` | `test_the_judge_does_not_rubber_stamp`, `test_when_the_judge_is_unavailable_nothing_is_assumed` |
| 6 | A stranger can run it | `make up`, `web/` | `test_seeding_twice_converges_instead_of_failing`, `test_a_reviewer_can_go_from_empty_to_a_queue_through_http_alone` |
| — | Read-only endpoints the interface needs | `api/review.py` | `tests/test_review_api.py`, 7 tests |
| 7 | Proves itself without a live key | `llm/client.py` record and replay | the whole suite |
| 8 | Takes no orders from its documents | three layers, below | `test_a_document_that_gives_orders_is_reported_and_obeyed_by_nothing`, `test_an_invoice_cannot_write_a_contract_field` |
| 9 | Two runs stay two runs | `service.py` | `test_racing_workers_never_take_the_same_run`, `test_racing_commits_produce_one_winner_and_no_lost_update`, `test_two_runs_on_one_pile_execute_in_parallel` (threads, real Postgres) |
| 10 | Knows what it cost | `llm/client.py` | `test_the_run_reports_what_it_cost_and_where_the_time_went` |

## Design decisions

**Sync SQLAlchemy, runs in a worker process.** The API never blocks on a model call and a SIGKILL to
a worker is a real crash to recover from. This system is model-latency bound, not connection bound,
so async buys nothing and costs clarity around row locks.

**No broker.** Postgres already has `FOR UPDATE SKIP LOCKED`, which is all a claim loop needs. A
small team should not operate Celery to run a handful of jobs a minute. If throughput ever justifies
one, `claim_next_run` is the only thing that changes.

**Verification is graded, not binary.** `EXACT`, `NUMERIC` and `STRUCTURAL` are deterministic and
free and settle most fields. `JUDGED` exists because a correctly cited paraphrase is real and
common, and rejecting it would leave a capability that only works on the inputs it was tested
against. The judge is never shown the proposed value as a claim to confirm; it is asked what the
span states and the comparison happens in code. `test_the_judge_does_not_rubber_stamp` holds that
line. Every run reports its grade mix, so a reviewer sees which claims rest on judgement.

**Graceful degradation is a path, not an exception.** A throttled model retries with jittered
backoff, then falls back a tier, then the run continues on deterministic rules alone with
`degraded` set and a reason recorded. A thinner result is never returned as if it were complete.
Fault injection is keyed by model id as well as by stage, because Bedrock throttles one model while
its neighbours are fine, and only a model-keyed fault can exercise the fallback at all.

**The judge is its own tier, with no fallback.** It is a separate call with a separate prompt that
never sees the value it is checking: it is asked what the span states, and the comparison happens in
code. Dropping it into the extractor's own tier would keep a run alive at the cost of the only
property the judge exists for, so it degrades to deterministic-only and says so.
`test_the_judge_never_falls_back_into_the_extractors_family` and
`test_the_judge_runs_on_its_own_tier_and_never_borrows_the_extractors` hold that line.

Cross-*family* verification was the intent — a verifier sharing the extractor's architecture shares
its failure modes, so a correlated hallucination would have to occur twice in two architectures to
reach the register. Bedrock would not allow it: Llama 4 Maverick and Pixtral Large both reject forced
tool use, which every structured call in this system depends on, and Nova Premier is marked legacy.
What survives is prompt independence, not architectural independence, and that is recorded below
rather than glossed.

**Deterministic rules run before any model call.** Most of the playbook is arithmetic once the facts
are extracted. A question a comparison can settle should never be settled probabilistically, and the
deep model never sees one.

**`inputs_hash` is what makes an update cost like an update.** Each field is bound to the text of
every span it cites plus the prompt and playbook versions. If none moved, the field is not
recomputed; it is proven byte-identical instead.

**Configuration over code.** A new rule, threshold or client is an edit to `rules/playbook.yaml`. A
new document type is an entry in `IMPACT_MAP` and `ALLOWED_PATTERNS`. Neither is a code change.

## The review interface

`web/` is a React app served by the `review` compose service. It is the surface a person uses; it is
not a surface the system depends on.

**It performs no operation the API and MCP server do not already expose.** Approve, reject and commit
are the same calls a program makes, which is what keeps behaviour 4 honest: the gate is an operation,
and the interface is one caller of it rather than the place it lives. Everything the interface added
to the backend is a read (`api/review.py`).

It covers the whole path, not just review: create a pile, drag documents in, start a run, watch it
progress, then decide. A reviewer never needs a shell.

What it shows, in the order it shows it:

1. **The path the run took.** Every stage, the decision it recorded, and — marked separately — the
   decisions that changed the path: an escalation instead of a guess, a retry, a refused
   out-of-scope write, a conflict surfaced rather than overwritten, a model tier lost. A list of
   stage names with ticks would be a fixed script with labels, which is the thing the brief rules
   out; naming the reroutes is the difference.
2. **The proof line.** *"3 fields recomputed, 27 byte-identical"*, with the run's model calls,
   cost and elapsed seconds beside it, and a per-stage table of calls, cost and p50/p95/max latency
   behind a disclosure. The clearest statement this system makes should not be buried in a panel.
3. **Pending items grouped by kind** — updates, conflicts, findings, escalations — each showing the
   before → after change and how many citations back it.
4. **The evidence panel.** Selecting an item fetches the cited span and renders the quoted words
   highlighted inside the clause around them. A reviewer cannot judge *"net 45 days"* without the
   sentence it sits in.
5. **Decisions per item, or per group.** Rejecting one finding does not touch the others.
6. **Commit**, sending the register version. A 409 reloads the queue and says why rather than
   pretending the write succeeded.

**Read the sources** opens each document in full with every cited span marked in place, so a
reviewer can check a citation against the document rather than only against the sentence around it.
Marks come from the stored evidence the verifier checked, not from re-searching the text in the
browser, so what is highlighted is what was actually verified.

Items with no resolvable citation are labelled *"rests on judgement alone"* rather than being given
a citation they do not have.

One endpoint is genuinely new rather than a convenience: `GET /documents/{id}/span`. The API returns
evidence as offsets, which is what a program wants. Computing the surrounding context in the browser
would mean shipping whole documents to the client, so it is computed server-side from the same
canonical text the citation was verified against, and the quote and its context are returned as
separate fields so the client never has to parse markup out of an API.

## Driving it from a machine

The MCP server exposes twelve tools over stdio. It is the same service layer the HTTP API calls, so
a program can do everything a person can, including the parts that are easy to leave out: creating a
pile, getting documents in, and approving.

```bash
# from ledgerline/, with the venv active and the database up
python -m ledgerline.mcp.server
```

To connect Claude Code or any MCP client, put this in `.mcp.json` at the repository root:

```json
{
  "mcpServers": {
    "ledgerline": {
      "command": "ledgerline/.venv/bin/python",
      "args": ["-m", "ledgerline.mcp.server"],
      "env": {
        "LEDGERLINE_DATABASE_URL": "postgresql+psycopg://ledgerline:ledgerline@localhost:5432/ledgerline"
      }
    }
  }
}
```

| Tool | |
|---|---|
| `list_piles` | every pile, with document and pending counts |
| `create_pile` | one vendor relationship |
| `add_document` | ingest a document by path; content-addressed, so the same bytes twice is a no-op |
| `start_run` | queue a run; a worker claims it |
| `get_run_status` | status, degradation, per-stage timings, **and the decision log** |
| `list_pending_items` | everything waiting on a person |
| `approve_items` / `reject_items` | decide specific items; the rest are untouched |
| `commit_run` | apply approved updates under the version check |
| `get_register` | the deliverable, with citations |
| `get_change_ledger` | what changed, when, because of which source |
| `explain_field` | why the register says what it says, with full history |

A whole flow, driven by a program, is: `create_pile` → `add_document` ×n → `start_run` → poll
`get_run_status` → `list_pending_items` → `approve_items` / `reject_items` → `commit_run`. No step
in that sequence requires a human to click anything, which is the point of behaviour 4.
`test_a_program_drives_the_entire_flow_through_mcp_alone` walks exactly that path and touches no
other surface.

## Where to see each behaviour, without reading the code

For a reviewer with the stack running:

| Behaviour | Where to look |
|---|---|
| 1 · steps you can watch | Review interface → the run header → **"The path this run took"**. Every stage, what it decided, and which decisions rerouted execution, marked in amber. Same data on `GET /runs/{id}` and MCP `get_run_status`. |
| 2 · survives being stopped | `pytest tests/test_durability.py -q`. It kills a real child process with SIGKILL mid-run and asserts each stage appears exactly once across both processes. |
| 3 · a human holds the gate | Review interface → approve some items, reject others, then commit. Only the approved ones land. The register is untouched until you press commit. |
| 4 · a machine can drive it | The MCP section above, or `pytest tests/test_surfaces.py -q`. |
| 5 · never bluffs | Review interface → run header → **"claims the sources did not support"**. **Read the sources** opens each document in full with every cited span marked in place, so a
reviewer can check a citation against the document rather than only against the sentence around it.
Marks come from the stored evidence the verifier checked, not from re-searching the text in the
browser, so what is highlighted is what was actually verified.

Items with no resolvable citation are labelled *rests on judgement alone* rather than given one. |
| 8 · takes no orders from its documents | The seeded corpus contains `inv_2209_poisoned.txt`. After a run it appears as a finding, and nothing it asked for happened. Click **Read the sources** to see the text it contains. |
| 9 · two runs stay two runs | `make test-pg`. Threads racing against real Postgres. |
| 10 · knows what it cost | Review interface → run header → the stat row and the per-stage table behind it. |

**Read the sources** is worth pressing early. It shows each document in full with every cited span
marked in place, so a citation can be checked against the document rather than only against the
sentence around it.

## Three layers of injection defence

They do different jobs and none is redundant.

1. **Structural**, `prompting.py`. Source text only ever appears in the user position inside a
   delimited data block. It never occupies the system instruction.
2. **Detection**, `security/injection.py`. Instruction-shaped content becomes a reported finding,
   because the structural layer cannot tell a reviewer what it defended against.
3. **Write allowlist**, `agent/state.py`. A document may only produce fields its own type owns. An
   invoice that talks its way into proposing a liability cap has that proposal dropped at the
   boundary and reported. Without this, injection is a data-integrity problem rather than a
   nuisance.

## Formats and domain accepted

PDF with a text layer, DOCX, EML, XLSX (as a source only), TXT and MD. Scanned PDFs without a text
layer are rejected with a named cause and fix rather than silently producing an empty document.

Domain: vendor contract files. `ledgerline seed --variant b` generates a second, different vendor
with a different document mix for the second run.

## Three bugs the tests found

All three would have shipped. All three were invisible in the output.

**Resume was a full replay.** `execute_run` passed a fresh input dict to the graph every time, so a
resumed run re-entered at START and replayed every completed node. The output was identical either
way, so only an assertion counting stage-log entries across both processes could see it. Passing
`None` continues from the checkpoint; passing state starts over. That one line is the whole of
behaviour 2.

**Schema creation sat in the hot path.** Every run called `checkpointer.setup()`, a
`CREATE TABLE IF NOT EXISTS` that is harmless single threaded and a unique-violation race the moment
two runs start together. Moved to `bootstrap()` and a `ledgerline init-db` command. Found only once
the concurrency tests ran on Postgres with real threads.

**The gate's dedupe was read-then-write.** Two parallel runs both read an empty key set and both
inserted, producing every pending item twice. The SQLite test passed because it ran the two runs
sequentially. Fixed with a unique constraint on `(pile_id, dedupe_key)` and a per-item savepoint, in
the database rather than in application code, because only the database can settle that race.

The pattern in all three: a test that compares results cannot find them. Each needed a test that
asserts on how the work happened.

## Two more bugs a live run found that the tests could not

Both shipped past 84 green tests. Both showed up in the first run against real Bedrock.

**The model was asked to count characters.** `EXTRACT_SCHEMA` asked for `char_start`/`char_end`
into the source block, and the prompt told the model to compute them by counting from the
delimiter. Models cannot count characters reliably; every offset came back approximately right,
which against a citation is simply wrong. On an 11-document live run this meant 65 of the run's
~76 extracted claims failed deterministic verification on the first pass, retried on every single
document, and needed the judge to confirm values a human could read in the first paragraph — 76 of
101 model calls and roughly three quarters of the run's cost, spent compensating for broken input.
Fixed by asking for the exact quoted text instead of an offset, and locating it in the document
with `str.find`, which is exact where a model's arithmetic is not (`resolve_extracted_field` in
`agent/stages.py`, `ExtractedField`/`EXTRACT_SCHEMA` in `schemas.py`). A quote that resolves to
nothing is now a stronger rejection signal than a bad offset ever was: a hallucinated citation,
named as such, rather than a span that merely points slightly to the left of the truth. The test
suite could not see this because `Corpus.seed_extract` built each fixture's offsets the same way
the prompt told the model to compute its own: locate the needle, add `block_offset()`. A systematic
offset error and the harness computing "the right answer" the same broken way agree with each other
by construction; only a model actually attempting the arithmetic disagrees with itself.

**Template field names reached the model as field names.** `FIELD_SETS` uses placeholders like
`rate_table.<n>.unit_price` to mean "one entry per rate row." Handed to the model verbatim as the
name of a field to extract, it came back exactly as verbatim: a field literally named
`rate_table.<n>.unit_price`, which the write allowlist could not match and which landed in
`unsupported_claims` under a name nothing downstream recognises. Fixed by spelling out the
substitution in the rendered prompt (`_describe_field_path` in `prompting.py`) and by rejecting
outright any field path still containing `<` or `>` after extraction, naming it as an unsubstituted
placeholder rather than an ordinary out-of-scope write. No test caught this because every existing
fixture was seeded with the field path already resolved (`invoices.INV-2205.amount`), which is what
a person writing a fixture does and not what a model filling in a template does.

Confirmed on a live run of a 5-document pile: 11 model calls, $0.0086, zero `verify_judge` calls and
zero `extract_retry` calls, against a baseline of 101 calls, $0.075, 65 judge calls and 11 retries
on 11 documents before the fix. Every contract, invoice and purchase-order field the corpus states
landed as a proposed update with a real citation; `unsupported_claims` was empty.

## Two bugs found by refusing to accept a passing rerun

**A sentinel in the wrong slot, labelled wrongly.** The model sometimes put the literal string
`"not_stated"` into the `quote` field instead of listing the field in the `not_stated` array. The
hallucinated-citation check refused it, which is the correct outcome — but it labelled the refusal a
hallucinated citation, which is a misleading entry in an audit trail, and it triggered a retry that
could never succeed. The sentinel check has to run *before* `str.find`, because an empty string is
found at index 0 of every document and would otherwise resolve to a real span in a real document.
What changed is the label and the retry decision; the refusal itself was not loosened. The class of
problem is a model returning a schema-shaped answer in the wrong slot, not one particular string.

**The completion check trusted a single field.** `execute_run` treated "LangGraph reports no next
task" as proof a run had finished. It is not proof. The pregel loop calls `put()` and `put_writes()`
as two separate commits, so a kill landing between them leaves a checkpoint where real work exists
and `next` reads empty — indistinguishable, at that field alone, from a completed run. The system
skipped the graph entirely and returned `awaiting_approval` with zero pending items. Work silently
dropped, which is worse than work repeated.

It surfaced as a test failing roughly one run in ten, and only under full-suite load: the durability
file alone passed 20 out of 20. A green rerun was one shrug away from shipping it. `resume_strategy`
now refuses to trust an empty `next` unless the stage log actually reached the gate, and it is unit
tested against the corrupted snapshot shape directly rather than against LangGraph internals that
could shift on a library upgrade.

A flaky test on a guarantee you advertise is not flaky. It is a bug you have not found yet.

## What an update actually costs

Measured, from `test_a_new_invoice_costs_an_update_not_a_rerun`. A pile of two documents is
committed, then one invoice arrives:

- the second run makes 1 classify call and 1 extract call, against the first run's 2 and 2
- the impact set is `["derived.invoiced_total", "invoices."]`; no contract field is even considered
- 3 fields recomputed, 8 byte-identical, 0 mismatches
- the register is unchanged until the new work is approved and committed

`test_the_second_run_reproposes_nothing_it_already_committed` is the stronger version: the same
documents arriving again produce zero proposals and zero recomputed fields.

## Where this build is weak

Stated rather than buried.

The judge shares the extractor's model family. Cross-family verification was the design goal —
Nova extracts, something else independently reads the span — and Bedrock would not allow it: Llama 4
Maverick and Pixtral Large both reject forced tool use, and Nova Premier is marked legacy. What
survives is prompt independence, not architectural independence. The judge tier has no fallback, so
it can never quietly become the extractor's own call.

`JUDGED` fields rest on a model reading a span. That is a weaker guarantee than the deterministic
grades, and the register marks them, but a determined adversarial paraphrase could pass. The
mitigation is that the judge never sees the value it is checking.

Booleans and nulls cannot be quoted, so they verify on span existence alone and are graded
`STRUCTURAL`. Same weaker guarantee, counted separately.

pgvector retrieval is implemented but only engages above 25 documents in a pile. Below that, spans
pass directly, because for a ten document file vector search is theatre. The threshold is a config
value and the honest position is that this corpus never crosses it.

The review interface is single-user and read-mostly. It has no live updates, so two reviewers working
the same queue would each see a stale list until they reload; the optimistic version check makes that
safe rather than silent, but it is friction rather than collaboration. Deliberate: live multiplayer
was explicitly out of scope.

The watcher polls rather than using inotify. Deliberate, and stated in `watcher.py`: a poll is
portable, has no state to rebuild after a restart, and works over the network mounts these folders
actually live on. It costs a second of latency on a workload measured in minutes.

Judge-tier findings that resolve no span are reported as resting on judgement alone. They are not
dressed up with a citation they do not have, but they are weaker than every other finding and a
reviewer should treat them that way.

There is no auth beyond a single bearer token and no multi-tenancy. Out of scope for this build and
not half-built.

The checkpointer's own durability has a gap that is not SQLite-specific. `langgraph` commits a
step's channel values and the writes that schedule its next task as two separate transactions, in
both `SqliteSaver` and `PostgresSaver` — confirmed by reading `put()`/`put_writes()` in both
packages, not assumed from one dialect behaving worse than the other. A process killed between the
two leaves a checkpoint that shows real progress but reads as finished. `service.resume_strategy`
closes this by refusing to trust "nothing scheduled" as proof of completion unless the stage log
actually reached the gate, and it does so identically for both checkpointers, since the defect
was never a SQLite one to begin with.

## Scope boundary

This system reads and reconciles documents. It does not execute AP transactions: no vendor master
CRUD, no PO issuance, no payment runs, no ERP sync. Purchase orders and invoices exist here as
extracted facts, not as artefacts this system issues.

## Data

Every document in `corpus/` is invented, generated by `corpus/generate.py`. No real vendor, no real
client, no third party's paperwork. The contradictions are designed, and listed in the seed output.

---

Built for the SuperDocs Round 2 task. AI-assisted throughout, directed and reviewed by me.
