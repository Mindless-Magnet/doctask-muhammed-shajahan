import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";

/**
 * Everything needed to get from an empty database to a queue worth reviewing, without a terminal.
 *
 * The run is queued, not executed here: a worker claims it. So this polls for status rather than
 * waiting on a response, which is also the honest shape — a run takes minutes on a real pile, and
 * an interface that blocks on it would be lying about what is happening.
 */
export default function Setup({ piles, pileId, onPileCreated, onChanged }) {
  const [name, setName] = useState("");
  const [documents, setDocuments] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const fileInput = useRef(null);
  const polling = useRef(null);

  const pile = piles.find((p) => p.pile_id === pileId);

  const loadDocuments = useCallback(async () => {
    if (!pileId) return setDocuments([]);
    try {
      const data = await api.documents(pileId);
      setDocuments(data.documents);
    } catch (err) {
      setError(err.message);
    }
  }, [pileId]);

  useEffect(() => {
    loadDocuments();
    setStatus(null);
    setNotice(null);
  }, [pileId, loadDocuments]);

  useEffect(() => () => clearInterval(polling.current), []);

  async function createPile(event) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    try {
      const created = await api.createPile(trimmed);
      setName("");
      setNotice(`Created “${created.name}”. Add documents to it below.`);
      onPileCreated(created.pile_id);
    } catch (err) {
      setError(err.message);
    }
  }

  async function ingest(files) {
    if (!pileId || !files.length) return;
    setUploading(true);
    setError(null);
    const added = [];
    const duplicates = [];
    for (const file of files) {
      try {
        const result = await api.uploadDocument(pileId, file);
        (result.created ? added : duplicates).push(result.filename);
      } catch (err) {
        setError(err.message);
        break;
      }
    }
    setUploading(false);
    await loadDocuments();
    onChanged();

    const parts = [];
    if (added.length) parts.push(`${added.length} added`);
    // Content-addressed, so the same bytes twice is a no-op rather than an error. Saying so is
    // better than silently doing nothing and letting the reviewer wonder.
    if (duplicates.length) parts.push(`${duplicates.length} already present, skipped`);
    if (parts.length) setNotice(parts.join(" · "));
  }

  async function startRun() {
    setStarting(true);
    setError(null);
    try {
      const queued = await api.startRun(pileId);
      setStatus({ run_id: queued.run_id, status: queued.status });
      setNotice("Started. This takes a minute or two on a full file.");
      clearInterval(polling.current);
      polling.current = setInterval(async () => {
        try {
          const latest = await api.latestRun(pileId);
          if (!latest.run) return;
          setStatus(latest.run);
          if (["awaiting_approval", "committed", "failed"].includes(latest.run.status)) {
            clearInterval(polling.current);
            setStarting(false);
            onChanged();
            if (latest.run.status === "failed") {
              setError(latest.run.error ?? "The run failed and recorded no reason.");
              setNotice(null);
            }
            if (latest.run.status === "awaiting_approval") {
              setNotice("Finished. Everything below is what it wants to change — nothing has been written yet.");
            }
          }
        } catch {
          /* transient; the next tick retries */
        }
      }, 1500);
    } catch (err) {
      setError(err.message);
      setStarting(false);
    }
  }

  return (
    <section className="setup">
      <div className="setup-grid">
        <div className="setup-col">
          <h2>1 · Start a vendor file</h2>
          <p className="hint">
            One file per vendor: their contract, its amendments, and every invoice billed against
            it. Name it after the vendor.
          </p>
          <form className="inline-form" onSubmit={createPile}>
            <input
              type="text"
              value={name}
              placeholder="Northwind Logistics"
              onChange={(e) => setName(e.target.value)}
              aria-label="New pile name"
            />
            <button className="btn small" type="submit" disabled={!name.trim()}>Start file</button>
          </form>
        </div>

        <div className="setup-col">
          <h2>2 · Add the paperwork</h2>
          <p className="hint">
            Contracts, amendments, SOWs, purchase orders, invoices, notices. PDF, DOCX, EML, XLSX,
            TXT or MD. Adding the same file twice does nothing, so you cannot double it up.
          </p>
          <div
            className={`drop ${dragging ? "over" : ""} ${!pileId ? "disabled" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              if (pileId) ingest(Array.from(e.dataTransfer.files));
            }}
            onClick={() => pileId && fileInput.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => { if (e.key === "Enter" && pileId) fileInput.current?.click(); }}
          >
            {uploading
              ? "Reading."
              : pileId
                ? "Drop files here, or click to choose"
                : "Start a vendor file first"}
          </div>
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            onChange={(e) => { ingest(Array.from(e.target.files)); e.target.value = ""; }}
          />
          {documents.length > 0 && (
            <ul className="doclist">
              {documents.map((doc) => (
                <li key={doc.document_id}>
                  <span className="docname">{doc.filename}</span>
                  <span className="doctype">
                    {doc.doc_type ?? "unclassified"}
                    {doc.confidence != null && ` · ${(doc.confidence * 100).toFixed(0)}%`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="setup-col">
          <h2>3 · Read it all</h2>
          <p className="hint">
            Works out what each document is, pulls out the terms that matter with a quote for every
            one, finds where the documents contradict each other, checks the lot against your
            playbook — then stops and waits for you. It writes nothing on its own.
          </p>
          <button
            className="btn primary"
            onClick={startRun}
            disabled={!pileId || !documents.length || starting}
          >
            {starting ? "Reading the file." : "Read the documents"}
          </button>
          {status && (
            <p className="runstate">
              <code>{status.run_id?.slice(0, 8)}</code> — {String(status.status).replace(/_/g, " ")}
            </p>
          )}
          {status?.status === "failed" && status.error && (
            <p className="runerror">{status.error}</p>
          )}
          {pile && (
            <p className="hint" style={{ marginTop: "10px" }}>
              {pile.documents} document{pile.documents === 1 ? "" : "s"} · {pile.pending} waiting on
              you · {pile.committed_fields} fact{pile.committed_fields === 1 ? "" : "s"} accepted so far
            </p>
          )}
        </div>
      </div>

      {error && <div className="banner error" role="alert"><strong>{error}</strong></div>}
      {notice && !error && <div className="banner ok" role="status"><strong>{notice}</strong></div>}
    </section>
  );
}
