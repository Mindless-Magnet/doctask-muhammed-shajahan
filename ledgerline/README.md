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
make venv      # once: creates .venv and installs the package. only needed for the targets below
make test      # 110 tests, no AWS account, no key, no network
make test-pg   # adds 4 dialect-specific concurrency tests, needs `docker compose up -d db`
make web       # the interface with hot reload, against an API you are already running
```

`make up` needs Docker and nothing else — no Python environment, no AWS account. Every target that
runs Python calls the virtualenv binary explicitly rather than assuming it is on `PATH`, because
`make` invokes `/bin/sh`, which never inherits an activated environment. A bare `pytest` in a
Makefile works only for whoever happened to activate first.

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

## Prove it to yourself, in under a minute

Four questions every reviewer of this repository ends up asking. Answers, and the exact command
that lets you check each one without trusting a word of this file.

**Is this actually agentic, or one model call in a UI?** `agent/graph.py`'s docstring documents
every conditional edge: classify routes to extract *or* straight to the gate if nothing was
classified confidently enough, extract retries once before falling back to the judge, and any
stage can escalate to a person instead of guessing. Those are decisions that change which stages
run next, not steps in a fixed sequence with labels on them. `test_nothing_confidently_classified_routes_straight_to_the_gate`
and `test_a_value_not_in_its_span_is_retried_then_recorded_as_unsupported` (both in
`tests/test_graph.py`) exercise the reroutes directly.

**Is what I'm looking at just something recorded, playing back?** Yes, deliberately, and here is
exactly what that means: `ReplayClient` (`llm/client.py`) matches each request against
`tests/fixtures/llm/` by hashing the *full* request — model id, prompt, schema, the lot — and
serves back a response genuinely recorded from live Bedrock by `make demo-fixtures`. Nothing about
the fixture is invented; it is what the real model said the one time this exact request was made
against it. Everything downstream of that call — the rules engine, the write-allowlist, the judge
comparison, the reconciliation and the proof — runs for real on every single run, replay or not.
Swap `LEDGERLINE_MODEL_CLIENT=bedrock` in `.env` and the same code path calls the real model
instead; nothing else changes.

**Will it work if I clone this onto a different machine?** Yes. The fixtures are committed to git,
so a stranger's laptop replays the exact same recorded model answers you would get, with no AWS
account, no key, and no spend — that is the whole point of `make test` and `make up`'s default.
Try it: `git clone`, `make up`, and the seeded pile above runs to completion with no credentials
present anywhere on that machine.

**Can I watch a run get killed mid-flight and pick itself back up, right now, in this terminal?**
Yes — this one is not simulated:

```bash
pytest tests/test_durability.py::test_a_killed_run_resumes_without_losing_or_repeating_work -v -s
```

Read what it does before you run it, because the mechanism is the point: it spawns a real child
process (`tests/kill_child.py`) that sends itself a real `SIGKILL` partway through the `extract`
stage — not an exception, not a clean exit, an actual kill a process cannot catch or clean up
after. The test process then calls `execute_run` on the same run id and asserts every stage
appears exactly once across both processes, ending at the gate. That resume happens because
`execute_run` passes `None` into the graph instead of a fresh input, which continues from the
LangGraph checkpoint rather than replaying from the start — the "Three bugs the tests found"
section below is the story of finding that this was originally backwards.

**Can I watch it accept documents in two batches and see the second batch cost less than a
re-run?** Yes, and the corpus this repository ships with is split for exactly this:
`corpus/northwind-logistics/` (11 documents, one pile, designed contradictions and all) can be fed
in as a first batch and a second batch. `pytest tests/test_incremental.py -v` proves it with real
call counts; see "What an update actually costs" below for the numbers from that test, and "Driving
it from a machine" for how the same batching works over MCP (`start_run`'s `document_ids`).

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

**One workspace, not a sequence of tabs someone has to already know to click through in order.**
The first version of this interface was four separate views — Documents, What it did, Sources,
Decide — and it read exactly like what it was: a checklist built to prove each behaviour exists,
not a tool built for the person using it. A reviewer who landed on "Documents" and never thought to
click "Decide" would never see that anything was waiting on them; the most important content in the
whole interface (the proof line, the cost, the queue itself) could be a click away from someone who
did not know to make it. That is now a single three-pane workspace instead, the shape a real review
tool actually takes — a file list, a diff, an inspector, all visible together:

- **Left, the rail.** Create a pile, drop documents in, press Read. It stays on screen the whole
  time, not behind a tab, because adding one more document while reviewing is a normal thing to do
  mid-review, not a context switch.
- **Middle, the queue.** Every pending update, conflict, finding and escalation, grouped by kind,
  each showing the before → after change and how many citations back it. This is the default thing
  on screen — not something reached by clicking "Decide" first.
- **Right, the inspector.** Selecting an item on the left fetches its cited span and shows the
  quoted words inside the sentence they sit in — a reviewer cannot judge *"net 45 days"* from the
  number alone. A link inside it swaps the same pane over to the full source document, every span
  it was ever cited from marked in place, without losing the pane's place in the layout.

Above the three panes, a single line always states what happened — documents read, model calls,
cost, and the proof line (*"3 fields recomputed, 27 byte-identical"*) — so the two numbers this
system's whole write-up rests on (proof over assertion, and what it cost) are visible without
opening anything. **The stage-by-stage path the run took** — every stage, the decision it recorded,
and, marked separately, the decisions that changed the path: an escalation instead of a guess, a
retry, a refused out-of-scope write, a model tier lost — sits one click below that in a disclosure,
collapsed by default because the headline is already visible, never buried behind a whole tab. A
list of stage names with ticks would be a fixed script with labels, which is the thing the brief
rules out; naming the reroutes is the difference.

Marks in the source view come from the stored evidence the verifier checked, not from re-searching
the text in the browser, so what is highlighted is what was actually verified. Items with no
resolvable citation are labelled *"rests on judgement alone"* rather than being given a citation
they do not have. Approve, reject, and commit are the same calls a program makes — the gate is an
operation, and this interface is one caller of it, not the place it lives.

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
| `get_run_status` | status, degradation, per-stage timings, cost, **and the decision log** |
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

**Documents arriving in batches over time — "add some now, add more later" — is this same flow run
twice.** Call `add_document` for the first batch, `start_run` naming those document ids, wait for
`awaiting_approval`, decide, and `commit_run`. Later, call `add_document` again for whatever
arrived since, then `start_run` again — naming only the new batch's document ids this time. That
second call is what makes the second run cost like an update instead of a full re-run: passing the
new ids scopes classification and extraction to just those documents, and reconciliation only
recomputes register fields those documents could actually affect (`agent/state.py`'s
`impacted_paths`); everything else is proven byte-identical rather than recomputed.
`get_run_status.cost` on the second run's id is where that shows up as a number, not a claim. A
run started with `document_ids` left empty treats the whole pile as new — correct for the very
first run over a pile, expensive for every run after that. `test_a_new_invoice_costs_an_update_not_a_rerun`
and `test_the_second_run_reproposes_nothing_it_already_committed` (`tests/test_incremental.py`)
measure exactly this, and "What an update actually costs" below quotes the numbers.

## Where to see each behaviour, without reading the code

For a reviewer with the stack running:

| Behaviour | Where to look |
|---|---|
| 1 · steps you can watch | Review interface → the disclosure below the status line, **"What it did"**. Every stage, what it decided, and which decisions rerouted execution, marked in amber. Same data on `GET /runs/{id}` and MCP `get_run_status`. |
| 2 · survives being stopped | `pytest tests/test_durability.py -q`. It kills a real child process with SIGKILL mid-run and asserts each stage appears exactly once across both processes. |
| 3 · a human holds the gate | Review interface → the middle pane: approve some items, reject others, then press Save in the header. Only the approved ones land. The register is untouched until you press it. |
| 4 · a machine can drive it | The MCP section above, or `pytest tests/test_surfaces.py -q`. |
| 5 · never bluffs | Review interface → the "What it refused to claim" panel inside the disclosure. Selecting any item and pressing **"See it in the whole document"** in the right-hand pane opens the source in full with every cited span marked in place, so a citation can be checked against the document rather than only against the sentence around it. Items with no resolvable citation are labelled *rests on judgement alone* rather than given one. |
| 8 · takes no orders from its documents | The seeded corpus contains `inv_2209_poisoned.txt` (`corpus/northwind-logistics/`). After a run it appears as a finding, and nothing it asked for happened. Select it and open the source to see the text it contains. |
| 9 · two runs stay two runs | `make test-pg`. Threads racing against real Postgres. |
| 10 · knows what it cost | Review interface → the status line above the workspace, and the per-stage table inside the disclosure. |

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

## Two bugs found by adding a third vendor

Adding `corpus/vantage-cloud/` (variant `c`) and recording it against live Bedrock, rather than
trusting the two vendors that already had committed fixtures, found two more real bugs — both
invisible until an actual second and third vendor exercised paths the first vendor's numbers
happened not to.

**A spend cap phrased as "USD 12,000" made `SPEND_CAP` silently never fire.** `rules/engine.py`'s
`_as_number` stripped `,` and `$` before parsing a monetary field, but not a leading currency code.
The model extracted the cap verbatim as `"USD 12,000"`, exactly what the source document says;
`_as_number` returned `None` on it; the rule's own `if limit is None: return []` guard — correct
for a field that is genuinely absent — treated "could not parse this" identically to "not stated at
all" and quietly skipped the check. The invoiced total on that vendor's three invoices was 14,280
against a stated cap of 12,000, a breach by design, and it produced zero findings. No existing test
caught this because every unit test for `SPEND_CAP` seeded a bare number (`_register({"contract.spend_cap.amount":
250000})`), which is what a person writing a test types and not what a model transcribing a
document says. Fixed in `_as_number` (strip a leading or trailing two-or-three-letter currency
code as well as `$` and `,`), pinned by `test_spend_cap_parses_a_currency_coded_amount`, which seeds
the exact string the live model actually returned rather than a number a test author chose.

**The review interface always ran the whole pile, even for a second batch.** `Setup.jsx`'s "Read
the documents" button called `api.startRun(pileId)` with no document ids, and the API's documented
default for that — treat every document in the pile as new — is correct for the very first run and
silently expensive for every run after it. The service layer, the MCP surface, and the watcher all
scope a run correctly; only the button a person actually clicks did not. Found by trying to reproduce
the "add five now, add five later" claim through the interface itself rather than only through
`tests/test_incremental.py`, which drives `execute_run` directly and never touches this button. Fixed
by having Setup pass only the document ids whose `status` is still `"received"` — a document a run
has classified or extracted moves off that status, so a second click after adding more documents
now costs like an update instead of a full re-run, and a first click still runs everything. `Setup`
also now reloads its own document list when a run finishes, because without that its local `status`
values go stale and the *next* run would silently mis-scope itself again.

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

Two vendors ship, each in its own subfolder so they can be opened and read directly without
running anything: `corpus/northwind-logistics/` (variant `a`, the default — 11 documents, every
designed contradiction listed at the top of `corpus/generate.py`) and `corpus/calder-freight/`
(variant `b` — a different vendor, a different document mix, for proving a second run behaves
differently from the first rather than replaying the same fixtures under a new name). Regenerate
either with `ledgerline seed --pile <name> --variant a|b`; both are backed by fixtures already
committed in `tests/fixtures/llm/`, so both run end to end with no AWS account. Adding a third vendor scenario is straightforward (a new `VARIANT_*` dict and label in
`generate.py`), but making it runnable through the CLI, API or MCP — as opposed to a unit test that
seeds its expected extraction directly via `Corpus.seed_extract` (`tests/conftest.py`) — needs a
fixture recorded against a live model first (`make demo-fixtures`, needs credentials). The two
vendors shipped here are the ones that have that.

---

Built for the SuperDocs Round 2 task. AI-assisted throughout, directed and reviewed by me.
