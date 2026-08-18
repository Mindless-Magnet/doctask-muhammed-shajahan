import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api, asObject, formatValue } from "./api.js";
import DocumentViewer from "./DocumentViewer.jsx";
import Setup from "./Setup.jsx";
import StageTimeline from "./StageTimeline.jsx";

/**
 * Four views, one job each, rather than one screen with everything toggled on and off.
 *
 * The order is the order the work happens in: put documents in, see what the system did with them,
 * read the sources it cited, decide what to keep. Someone who has never seen this before should be
 * able to move left to right and follow what happened without being told.
 */
const VIEWS = [
  { id: "documents", label: "Documents", blurb: "What goes in" },
  { id: "process", label: "What it did", blurb: "Every step, and the decisions that changed the path" },
  { id: "sources", label: "Sources", blurb: "The documents, with quoted passages marked" },
  { id: "decide", label: "Decide", blurb: "Approve or reject, item by item" },
];

const KINDS = [
  {
    id: "update",
    label: "Facts it wants to record",
    blurb: "Terms it read out of the documents. Approve to add them to the register.",
  },
  {
    id: "conflict",
    label: "Contradictions",
    blurb: "Two documents say different things. Nothing is applied until you pick.",
  },
  {
    id: "finding",
    label: "Problems it found",
    blurb: "Rules from your playbook that these documents would break.",
  },
  {
    id: "escalation",
    label: "It could not tell",
    blurb: "Documents it refused to guess about, and passed to you instead.",
  },
];

const REVIEWER = "reviewer";

export default function App() {
  const [piles, setPiles] = useState([]);
  const [pileId, setPileId] = useState(null);
  const [view, setView] = useState("documents");
  const [items, setItems] = useState([]);
  const [version, setVersion] = useState(0);
  const [run, setRun] = useState(null);
  const [selected, setSelected] = useState(null);
  const [activeSpan, setActiveSpan] = useState(null);
  const [busy, setBusy] = useState(new Set());
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [loading, setLoading] = useState(true);

  const refreshPiles = useCallback(async (selectId) => {
    try {
      const data = await api.piles();
      setPiles(data.piles);
      if (selectId) setPileId(selectId);
      return data.piles;
    } catch (err) {
      setError(err.message);
      return [];
    }
  }, []);

  useEffect(() => {
    api
      .piles()
      .then((data) => {
        setPiles(data.piles);
        if (data.piles.length) setPileId(data.piles[0].pile_id);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  const refresh = useCallback(async () => {
    if (!pileId) return;
    try {
      const [pending, latest] = await Promise.all([api.pending(pileId), api.latestRun(pileId)]);
      setItems(pending.items);
      setVersion(pending.register_version);
      setRun(latest.run);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, [pileId]);

  useEffect(() => {
    setSelected(null);
    setActiveSpan(null);
    setNotice(null);
    refresh();
  }, [pileId, refresh]);

  const grouped = useMemo(() => {
    const map = Object.fromEntries(KINDS.map((k) => [k.id, []]));
    items.forEach((item) => (map[item.kind] ?? (map[item.kind] = [])).push(item));
    return map;
  }, [items]);

  async function decide(itemIds, approve) {
    setBusy(new Set([...busy, ...itemIds]));
    setNotice(null);
    try {
      await (approve ? api.approve : api.reject)(pileId, itemIds, REVIEWER);
      setItems((current) => current.filter((item) => !itemIds.includes(item.id)));
      if (selected && itemIds.includes(selected.id)) setSelected(null);
      setNotice(
        `${itemIds.length} ${approve ? "approved" : "rejected"}. Still nothing written — press Save to apply the approved ones.`,
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy((current) => {
        const next = new Set(current);
        itemIds.forEach((id) => next.delete(id));
        return next;
      });
    }
  }

  async function commit() {
    if (!run) return;
    setNotice(null);
    try {
      const result = await api.commit(pileId, run.run_id, version, REVIEWER);
      setNotice(
        `Saved ${result.applied} change${result.applied === 1 ? "" : "s"} into the register, now version ${result.register_version}.`,
      );
      setVersion(result.register_version);
      await refresh();
      await refreshPiles();
    } catch (err) {
      if (err instanceof ApiError && err.isVersionConflict) {
        setError(`${err.message} Reloading.`);
        await refresh();
      } else {
        setError(err.message);
      }
    }
  }

  function openCitation(item) {
    setSelected(item);
    setActiveSpan(item.evidence?.[0] ?? null);
  }

  if (loading) {
    return <Shell><p className="muted">Loading.</p></Shell>;
  }

  if (!piles.length) {
    return (
      <Shell>
        <div className="intro">
          <h2>Start here</h2>
          <p>
            Give it a vendor's paperwork — the contract, the amendments, the invoices — and it reads
            the lot, pulls out the terms that matter with a quote behind each one, and tells you
            where the documents contradict each other. It writes nothing without your say-so.
          </p>
        </div>
        <Setup
          piles={piles}
          pileId={pileId}
          onPileCreated={(id) => refreshPiles(id)}
          onChanged={() => refreshPiles()}
        />
      </Shell>
    );
  }

  const pile = piles.find((p) => p.pile_id === pileId);

  return (
    <Shell>
      <header className="bar">
        <label className="field">
          <span>Vendor file</span>
          <select value={pileId ?? ""} onChange={(e) => setPileId(e.target.value)}>
            {piles.map((p) => (
              <option key={p.pile_id} value={p.pile_id}>
                {p.name} — {p.documents} document{p.documents === 1 ? "" : "s"}
                {p.pending ? `, ${p.pending} waiting on you` : ""}
              </option>
            ))}
          </select>
        </label>
        <div className="bar-right">
          <span className="version">register v{version}</span>
          <button className="btn primary" onClick={commit} disabled={!run || !items.length}>
            Save approved changes
          </button>
        </div>
      </header>

      <RunBanner run={run} pile={pile} pending={items.length} />

      {error && (
        <div className="banner error" role="alert">
          <strong>{error}</strong>
          <button className="link" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}
      {notice && !error && (
        <div className="banner ok" role="status">
          <strong>{notice}</strong>
          <button className="link" onClick={() => setNotice(null)}>Dismiss</button>
        </div>
      )}

      <nav className="views" aria-label="Views">
        {VIEWS.map((v) => (
          <button
            key={v.id}
            className={`viewtab ${view === v.id ? "on" : ""}`}
            onClick={() => setView(v.id)}
            aria-current={view === v.id}
          >
            <b>{v.label}</b>
            <span>{v.blurb}</span>
            {v.id === "decide" && items.length > 0 && <i className="badge">{items.length}</i>}
          </button>
        ))}
      </nav>

      {view === "documents" && (
        <Setup
          piles={piles}
          pileId={pileId}
          onPileCreated={(id) => refreshPiles(id)}
          onChanged={() => { refreshPiles(); refresh(); }}
        />
      )}

      {view === "process" && <ProcessView run={run} />}

      {view === "sources" && (
        <DocumentViewer pileId={pileId} activeSpan={activeSpan} onClose={() => setView("decide")} />
      )}

      {view === "decide" && (
        <div className="split">
          <div className="queue">
            {items.length === 0 ? (
              <div className="empty inline">
                <h2>Nothing waiting on you</h2>
                <p>
                  Either everything from the last read has been decided, or the documents raised
                  nothing to decide. An empty queue on a clean set of documents is a real answer, not
                  a failure to run — which is why it says so rather than showing a blank screen.
                </p>
              </div>
            ) : (
              KINDS.map((kind) =>
                grouped[kind.id]?.length ? (
                  <Group
                    key={kind.id}
                    kind={kind}
                    items={grouped[kind.id]}
                    selected={selected}
                    busy={busy}
                    onSelect={openCitation}
                    onDecide={decide}
                  />
                ) : null,
              )
            )}
          </div>
          <aside className="evidence">
            <Evidence item={selected} onOpenSource={() => setView("sources")} />
          </aside>
        </div>
      )}
    </Shell>
  );
}

function Shell({ children }) {
  return (
    <div className="app">
      <div className="masthead">
        <h1>Ledgerline</h1>
        <p>
          Reads a vendor's contracts and invoices, finds where they disagree, and shows you the exact
          words behind every claim. Nothing is written until you approve it.
        </p>
      </div>
      {children}
    </div>
  );
}

/** One line telling the reviewer where they are, before any detail. */
function RunBanner({ run, pile, pending }) {
  if (!run) {
    return (
      <p className="runbanner muted">
        {pile?.documents
          ? "These documents have not been read yet. Open Documents and press Read the documents."
          : "No documents yet. Open Documents to add some."}
      </p>
    );
  }

  if (run.status === "failed") {
    return (
      <div className="banner error" role="alert">
        <strong>The last read failed. {run.error ?? "No reason was recorded."}</strong>
      </div>
    );
  }

  const cost = run.cost ?? {};
  return (
    <p className="runbanner">
      Read <b>{pile?.documents ?? "?"}</b> document{pile?.documents === 1 ? "" : "s"} in{" "}
      <b>{(run.timings?.total_seconds ?? 0).toFixed(1)}s</b> using{" "}
      <b>{cost.total_calls ?? 0}</b> model call{cost.total_calls === 1 ? "" : "s"} costing{" "}
      <b>${(cost.total_cost_usd ?? 0).toFixed(4)}</b>.{" "}
      {pending > 0 ? (
        <><b>{pending}</b> thing{pending === 1 ? "" : "s"} waiting on you.</>
      ) : (
        "Nothing waiting on you."
      )}
      {run.degraded && (
        <span className="degraded" title={run.degraded_reason ?? ""}>ran degraded</span>
      )}
    </p>
  );
}

/** Behaviour 1, given a whole view rather than a collapsed panel inside a header. */
function ProcessView({ run }) {
  if (!run) return <p className="muted">Nothing has been read yet.</p>;

  const cost = run.cost ?? {};
  const stages = Object.entries(cost.by_stage ?? {});
  const proof = run.proof;

  return (
    <div className="process">
      <StageTimeline stages={run.stage_log} timings={run.timings} />

      {proof && (
        <section className="panel">
          <h2>What it changed, and what it left alone</h2>
          <p className={`proof ${proof.holds ? "" : "broken"}`}>
            <strong>{proof.fields_recomputed}</strong> field
            {proof.fields_recomputed === 1 ? "" : "s"} recalculated,{" "}
            <strong>{proof.fields_unchanged}</strong>{" "}
            {proof.holds
              ? "left byte-for-byte identical"
              : `CHANGED UNEXPECTEDLY (${proof.mismatched.join(", ")})`}.
          </p>
          <p className="hint">
            Not a claim: the untouched fields are hashed before and after and compared. If one had
            moved, this line would say so.
          </p>
        </section>
      )}

      {stages.length > 0 && (
        <section className="panel">
          <h2>What it cost, stage by stage</h2>
          <table className="tbl">
            <thead>
              <tr>
                <th>stage</th><th>calls</th><th>cost</th><th>median</th><th>p95</th><th>slowest</th>
              </tr>
            </thead>
            <tbody>
              {stages.map(([name, s]) => (
                <tr key={name}>
                  <td><code>{name}</code></td>
                  <td>{s.calls}</td>
                  <td>${(s.cost_usd ?? 0).toFixed(4)}</td>
                  <td>{s.latency_ms_p50}ms</td>
                  <td>{s.latency_ms_p95}ms</td>
                  <td>{s.latency_ms_max}ms</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint">
            Tail latency, not only an average. Prices come from a table committed with the date it
            was pulled; the model is never asked to estimate its own cost.
          </p>
        </section>
      )}

      {run.not_stated?.length > 0 && (
        <section className="panel">
          <h2>What it refused to claim</h2>
          <p className="hint">
            Fields the documents do not state, or values it could not tie to the words it quoted.
            Recorded rather than guessed at.
          </p>
          <ul className="unsupported">
            {run.not_stated.map((claim, i) => (
              <li key={i}><code>{claim.field_path}</code> — {claim.reason}</li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function Group({ kind, items, selected, busy, onSelect, onDecide }) {
  const ids = items.map((item) => item.id);
  return (
    <section className="group">
      <div className="group-head">
        <div>
          <h2>{kind.label} <span className="count">{items.length}</span></h2>
          <p>{kind.blurb}</p>
        </div>
        <div className="group-actions">
          <button className="btn small" onClick={() => onDecide(ids, true)}>Approve all</button>
          <button className="btn small ghost" onClick={() => onDecide(ids, false)}>Reject all</button>
        </div>
      </div>

      {items.map((item) => (
        <Item
          key={item.id}
          item={item}
          active={selected?.id === item.id}
          busy={busy.has(item.id)}
          onSelect={() => onSelect(item)}
          onDecide={onDecide}
        />
      ))}
    </section>
  );
}

function Item({ item, active, busy, onSelect, onDecide }) {
  const payload = asObject(item.payload) ?? {};
  const hasEvidence = (item.evidence ?? []).length > 0;

  return (
    <article className={`item ${active ? "active" : ""} ${busy ? "busy" : ""}`}>
      <button className="item-main" onClick={onSelect} aria-pressed={active}>
        <h3>{item.title}</h3>
        {item.field_path && <code className="path">{item.field_path}</code>}
        {item.kind === "update" && (
          <p className="change">
            <span className="was">{formatValue(payload.before)}</span>
            <span className="arrow">→</span>
            <span className="now">{formatValue(payload.after)}</span>
          </p>
        )}
        {item.kind === "conflict" && Array.isArray(payload.values) && (
          <p className="change">
            {payload.values.map((value, i) => (
              <span key={i} className="conflicting">{formatValue(value)}</span>
            ))}
          </p>
        )}
        {item.detail && <p className="detail">{item.detail}</p>}
        <p className="meta">
          {item.rule_id && <code>{item.rule_id}</code>}
          {hasEvidence ? (
            <span>
              {item.evidence.length} quote{item.evidence.length === 1 ? "" : "s"} — click to read them
            </span>
          ) : (
            <span className="nocite">no quote to point at — this rests on judgement alone</span>
          )}
        </p>
      </button>

      <div className="item-actions">
        <button className="btn small" disabled={busy} onClick={() => onDecide([item.id], true)}>
          Approve
        </button>
        <button className="btn small ghost" disabled={busy} onClick={() => onDecide([item.id], false)}>
          Reject
        </button>
      </div>
    </article>
  );
}

function Evidence({ item, onOpenSource }) {
  const [spans, setSpans] = useState([]);
  const [state, setState] = useState("idle");

  useEffect(() => {
    if (!item) {
      setSpans([]);
      setState("idle");
      return;
    }
    const evidence = item.evidence ?? [];
    if (!evidence.length) {
      setSpans([]);
      setState("none");
      return;
    }
    let cancelled = false;
    setState("loading");
    Promise.all(
      evidence.map((e) =>
        api
          .span(e.document_id, e.char_start, e.char_end)
          .then((data) => ({ ...data, locator: e.locator }))
          .catch((err) => ({ error: err.message, locator: e.locator })),
      ),
    ).then((results) => {
      if (cancelled) return;
      setSpans(results);
      setState("ready");
    });
    return () => {
      cancelled = true;
    };
  }, [item]);

  if (!item) {
    return (
      <div className="evidence-empty">
        <h2>The words behind it</h2>
        <p>Pick something on the left and the exact passage it came from appears here.</p>
      </div>
    );
  }

  return (
    <div className="evidence-inner">
      <h2>The words behind it</h2>
      <p className="evidence-title">{item.title}</p>

      {state === "loading" && <p className="muted">Reading the documents.</p>}
      {state === "none" && (
        <p className="muted">
          Nothing to quote. This one rests on judgement rather than a passage, and it says so instead
          of being given a quote it does not have.
        </p>
      )}

      {spans.map((span, i) =>
        span.error ? (
          <div key={i} className="cited broken">
            <span className="filename">{span.locator}</span>
            <p className="muted">{span.error}</p>
          </div>
        ) : (
          <div key={i} className="cited">
            <span className="filename">
              {span.filename}
              {span.doc_type && <em> · {span.doc_type}</em>}
              {span.locator && <em> · {span.locator}</em>}
            </span>
            <p className="passage">
              {span.truncated_start && <span className="ellipsis">…</span>}
              {span.before}
              <mark>{span.quote}</mark>
              {span.after}
              {span.truncated_end && <span className="ellipsis">…</span>}
            </p>
          </div>
        ),
      )}

      {state === "ready" && spans.length > 0 && (
        <button className="link" onClick={onOpenSource}>See it in the whole document</button>
      )}
    </div>
  );
}
