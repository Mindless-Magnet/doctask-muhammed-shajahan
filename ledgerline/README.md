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

Brings up Postgres, migrates, starts the API and two workers, generates a synthetic vendor file and
loads it. API docs on http://localhost:8000/docs.

```bash
make test      # 80 tests, no AWS account, no key, no network
make test-pg   # adds 4 dialect-specific concurrency tests, needs the compose database
```

Then drop a document into the watched folder and watch a focused update happen:

```bash
ledgerline watch --pile northwind --directory ./inbox
```

## The ten behaviours, and where each is proven

| # | Behaviour | Implementation | Test |
|---|---|---|---|
| 1 | Visible stages, decisions that change the path | `agent/graph.py` | `test_nothing_confidently_classified_routes_straight_to_the_gate`, `test_a_value_not_in_its_span_is_retried_then_recorded_as_unsupported` |
| 2 | Survives being stopped | `service.execute_run` | `test_a_killed_run_resumes_without_losing_or_repeating_work` (real SIGKILL to a child process), `test_a_resumed_run_reaches_the_same_result_as_an_uninterrupted_one` |
| 3 | A human holds the gate, item by item | `service.decide_items` | `test_rejecting_one_item_leaves_the_rest_alone` |
| 3b | Stays alive: focused updates, proven untouched | `watcher.py`, `register/reconcile.py` | `test_a_new_invoice_costs_an_update_not_a_rerun`, `test_the_second_run_reproposes_nothing_it_already_committed`, `test_a_contradicting_source_raises_a_conflict_and_overwrites_nothing`, `test_the_ledger_answers_what_changed_when_and_because_of_which_source` |
| 4 | A machine can drive it, approval included | `mcp/server.py` | `test_a_program_drives_the_entire_flow_through_mcp_alone` |
| 5 | Never bluffs | `verification.py` | `test_the_judge_does_not_rubber_stamp`, `test_when_the_judge_is_unavailable_nothing_is_assumed` |
| 6 | A stranger can run it | `make up` | manual, one command |
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

**The judge is a different model family from the extractor.** Nova extracts; Llama independently
reads the cited span. A verifier that shares the extractor's architecture shares its failure modes,
so a correlated hallucination would have to happen twice, in two families, to reach the register.
The judge tier therefore has no fallback: dropping into Nova would keep the run alive at the cost of
the only property the judge exists for, so it degrades to deterministic-only and says so.
`test_the_judge_never_falls_back_into_the_extractors_family` holds that line.

**Deterministic rules run before any model call.** Most of the playbook is arithmetic once the facts
are extracted. A question a comparison can settle should never be settled probabilistically, and the
deep model never sees one.

**`inputs_hash` is what makes an update cost like an update.** Each field is bound to the text of
every span it cites plus the prompt and playbook versions. If none moved, the field is not
recomputed; it is proven byte-identical instead.

**Configuration over code.** A new rule, threshold or client is an edit to `rules/playbook.yaml`. A
new document type is an entry in `IMPACT_MAP` and `ALLOWED_PATTERNS`. Neither is a code change.

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

`JUDGED` fields rest on a model reading a span. That is a weaker guarantee than the deterministic
grades, and the register marks them, but a determined adversarial paraphrase could pass. The
mitigation is that the judge never sees the value it is checking.

Booleans and nulls cannot be quoted, so they verify on span existence alone and are graded
`STRUCTURAL`. Same weaker guarantee, counted separately.

pgvector retrieval is implemented but only engages above 25 documents in a pile. Below that, spans
pass directly, because for a ten document file vector search is theatre. The threshold is a config
value and the honest position is that this corpus never crosses it.

The React review interface is not built. The gate is fully exercised through the HTTP and MCP
surfaces, so the capability is present and the interface is absent, rather than present and broken.

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
