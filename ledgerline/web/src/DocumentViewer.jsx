import { useEffect, useState } from "react";
import { api } from "./api.js";

/**
 * The source, with every span anything cites from it marked in place.
 *
 * The evidence panel answers "what does this claim rest on". This answers the question a reviewer
 * asks next: "and what else is in that document, and did the system miss anything". Both are
 * needed, and neither substitutes for the other.
 *
 * Marks come from stored evidence rather than from re-searching the text in the browser, so what
 * is highlighted is exactly what the verifier checked, not an approximation that happens to agree
 * most of the time.
 */
export default function DocumentViewer({ pileId, activeSpan, onClose }) {
  const [documents, setDocuments] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [document_, setDocument] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!pileId) return;
    api.documents(pileId).then((d) => {
      setDocuments(d.documents);
      if (d.documents.length) setOpenId((current) => current ?? d.documents[0].document_id);
    }).catch((err) => setError(err.message));
  }, [pileId]);

  // Selecting a citation elsewhere in the interface opens the document it came from.
  useEffect(() => {
    if (activeSpan?.document_id) setOpenId(activeSpan.document_id);
  }, [activeSpan]);

  useEffect(() => {
    if (!openId) return;
    setDocument(null);
    api.document(openId).then(setDocument).catch((err) => setError(err.message));
  }, [openId]);

  return (
    <section className="viewer">
      <div className="viewer-head">
        <h2>Sources</h2>
        <button className="link" onClick={onClose}>Close</button>
      </div>

      {error && <p className="muted">{error}</p>}

      <div className="viewer-tabs">
        {documents.map((doc) => (
          <button
            key={doc.document_id}
            className={`tab ${openId === doc.document_id ? "on" : ""}`}
            onClick={() => setOpenId(doc.document_id)}
          >
            {doc.filename}
            <em>{doc.doc_type ?? "unclassified"}</em>
          </button>
        ))}
      </div>

      {document_ && (
        <>
          <p className="viewer-meta">
            {document_.citations.length} cited span
            {document_.citations.length === 1 ? "" : "s"} in this document
            {document_.confidence != null && (
              <> · classified {document_.doc_type} at {(document_.confidence * 100).toFixed(0)}%</>
            )}
          </p>
          <div className="viewer-text">
            {renderMarked(document_.text, document_.citations, activeSpan)}
          </div>
        </>
      )}
    </section>
  );
}

/**
 * Split the text at citation boundaries and wrap each cited range.
 *
 * Overlapping citations are collapsed to the outermost range rather than nested: two claims can
 * legitimately cite overlapping text, and nested marks render as darker bands that read like a
 * confidence signal the system is not making.
 */
function renderMarked(text, citations, activeSpan) {
  if (!citations.length) return text;

  const merged = [];
  for (const citation of [...citations].sort((a, b) => a.char_start - b.char_start)) {
    const last = merged[merged.length - 1];
    if (last && citation.char_start < last.char_end) {
      last.char_end = Math.max(last.char_end, citation.char_end);
      last.labels.push(citation.label);
    } else {
      merged.push({ ...citation, labels: [citation.label] });
    }
  }

  const parts = [];
  let cursor = 0;
  merged.forEach((range, i) => {
    if (range.char_start > cursor) parts.push(text.slice(cursor, range.char_start));
    const isActive =
      activeSpan &&
      activeSpan.char_start >= range.char_start &&
      activeSpan.char_end <= range.char_end;
    parts.push(
      <mark
        key={i}
        className={isActive ? "active" : ""}
        title={range.labels.join(" · ")}
      >
        {text.slice(range.char_start, range.char_end)}
      </mark>,
    );
    cursor = range.char_end;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}
