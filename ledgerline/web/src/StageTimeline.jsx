/**
 * What the run did, stage by stage.
 *
 * This is behaviour 1 made visible. The graph already records a decision per stage; without a
 * surface that shows it, "the system moves through visible stages and shows what it decided" is
 * true of the code and false of the product.
 *
 * The distinction the display has to carry is between a stage that ran and a stage that *changed
 * the path*. A list of seven stage names with green ticks is a fixed script with labels, which is
 * the exact thing the brief rules out. So decisions that rerouted execution are named and marked,
 * and stages that appear only sometimes are called out as such.
 */

// Decisions that mean the run took a different path than it otherwise would have. Keyed by the
// decision string the stage records, not by stage name, because the same stage can either reroute
// or not depending on what it found.
const REROUTING = {
  escalated_low_confidence: "Escalated to a person instead of guessing",
  blocked_out_of_scope_writes: "Refused writes outside the document's own fields",
  conflicts_surfaced: "Surfaced a contradiction instead of overwriting",
  retried_with_tighter_prompt: "Retried with a tighter prompt after verification failed",
  judge_unavailable_deterministic_only: "Model tier unavailable — continued on deterministic rules",
  deterministic_only: "Deterministic rules only",
};

const STAGE_BLURB = {
  intake: "Hashed and read every document, and scanned each for instructions aimed at the system.",
  classify: "Worked out what each document is, with a confidence.",
  extract: "Pulled the fields each document type owns, each with a quote, then verified them.",
  extract_retry: "A second, narrower attempt at the fields whose quotes did not support them.",
  reconcile: "Merged verified facts into the register, considering only fields the new documents could affect.",
  examine: "Checked the register against the playbook.",
  gate: "Wrote everything as pending items and stopped. Nothing committed.",
};

// Stages that only appear when something made them appear. Naming them is how a reader can tell a
// path change from a step that always runs.
const CONDITIONAL = new Set(["extract_retry"]);

export default function StageTimeline({ stages, timings }) {
  if (!stages?.length) return null;

  const reroutes = stages.filter(
    (s) => REROUTING[s.decision] || CONDITIONAL.has(s.stage),
  ).length;

  return (
    <details className="timeline" open>
      <summary>
        The path this run took — {stages.length} stages
        {reroutes > 0 && (
          <span className="reroute-count">
            {reroutes} decision{reroutes === 1 ? "" : "s"} changed it
          </span>
        )}
      </summary>

      <ol className="stages-list">
        {stages.map((entry, i) => {
          const rerouted = Boolean(REROUTING[entry.decision]);
          const conditional = CONDITIONAL.has(entry.stage);
          const seconds = timings?.[entry.stage];
          const facts = Object.entries(entry).filter(
            ([k]) => !["stage", "decision", "at"].includes(k),
          );

          return (
            <li key={i} className={rerouted || conditional ? "rerouted" : ""}>
              <div className="stage-head">
                <code className="stage-name">{entry.stage}</code>
                {conditional && <span className="tag conditional">only ran because it had to</span>}
                {rerouted && <span className="tag reroute">changed the path</span>}
                {seconds != null && <span className="stage-time">{Number(seconds).toFixed(2)}s</span>}
              </div>

              <p className="stage-blurb">{STAGE_BLURB[entry.stage] ?? ""}</p>

              <p className="stage-decision">
                <span className="decision-label">decided</span>
                <code>{entry.decision}</code>
                {REROUTING[entry.decision] && (
                  <span className="decision-why">{REROUTING[entry.decision]}</span>
                )}
              </p>

              {facts.length > 0 && (
                <ul className="stage-facts">
                  {facts.map(([key, value]) => (
                    <li key={key}>
                      <span>{key.replace(/_/g, " ")}</span>
                      <b>{Array.isArray(value) ? value.join(", ") || "none" : String(value)}</b>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ol>
    </details>
  );
}
