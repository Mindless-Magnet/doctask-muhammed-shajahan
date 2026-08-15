"""Graph construction and the checkpointer.

Three conditional edges, each of which changes the path for a reason:

  classify -> extract | gate      nothing confidently classified, so there is nothing to extract
  extract  -> extract_retry | reconcile   spans that did not support their values get one tighter
                                          attempt, then become unsupported claims
  reconcile -> examine            degradation routes through the same node in deterministic-only
                                  mode rather than failing the run

The retry edge is a genuine cycle. It is bounded at one pass by `retry_count`, because a retry is a
path change and an unbounded loop is a bug.

The checkpointer is chosen from the database URL. Postgres in production and in the concurrency
tests; SQLite for local runs and for CI, where it still persists to a file and therefore still
survives a process being killed.

Neither backend commits a step's channel values and the writes that schedule its next task
atomically together — `langgraph`'s pregel loop calls `checkpointer.put()` and
`checkpointer.put_writes()` as two separate calls regardless of dialect, and both `SqliteSaver`
and `PostgresSaver` commit each one on its own. A process killed between them leaves a checkpoint
whose values show real progress but whose scheduled-next-task info is missing — this is a property
of both backends, not a SQLite shortcut, so `service.resume_strategy` does not trust `next` being
empty as proof of completion; it also checks that the stage log actually reached the gate. See its
docstring for how this was found and why the fix does not distinguish by dialect.
"""

from __future__ import annotations

import operator
from contextlib import contextmanager
from functools import partial
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph

from ledgerline.agent.stages import (
    StageContext,
    route_after_classify,
    route_after_extract,
    stage_classify,
    stage_examine,
    stage_extract,
    stage_extract_retry,
    stage_gate,
    stage_intake,
    stage_reconcile,
)
from ledgerline.agent.state import RunState


class GraphState(RunState):
    # Only the log accumulates across nodes; everything else is last-write-wins by design, so a
    # resumed run replaces a partial stage's output rather than appending to it twice.
    stage_log: Annotated[list[dict[str, Any]], operator.add]


def build_graph(context: StageContext, checkpointer=None):
    graph = StateGraph(GraphState)

    graph.add_node("intake", partial(stage_intake, context))
    graph.add_node("classify", partial(stage_classify, context))
    graph.add_node("extract", partial(stage_extract, context))
    graph.add_node("extract_retry", partial(stage_extract_retry, context))
    graph.add_node("reconcile", partial(stage_reconcile, context))
    graph.add_node("examine", partial(stage_examine, context))
    graph.add_node("gate", partial(stage_gate, context))

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "classify")
    graph.add_conditional_edges(
        "classify", route_after_classify, {"extract": "extract", "gate": "gate"}
    )
    graph.add_conditional_edges(
        "extract",
        route_after_extract,
        {"extract_retry": "extract_retry", "reconcile": "reconcile"},
    )
    graph.add_edge("extract_retry", "reconcile")
    graph.add_edge("reconcile", "examine")
    graph.add_edge("examine", "gate")
    graph.add_edge("gate", END)

    return graph.compile(checkpointer=checkpointer)


@contextmanager
def checkpointer_for(database_url: str, *, setup: bool = False):
    """Yield a checkpointer for this database.

    `setup` creates the checkpointer's own tables and defaults to False on purpose. It used to run
    on every run, which is a `CREATE TABLE IF NOT EXISTS` in the hot path: harmless single
    threaded, and a unique-violation race the moment two runs start at once. Schema creation is a
    migration step. Call `bootstrap()` once at deploy time or in a test fixture.
    """
    if database_url.startswith("sqlite"):
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        path = database_url.split("///", 1)[-1]
        connection = sqlite3.connect(path, check_same_thread=False)
        try:
            saver = SqliteSaver(connection)
            if setup:
                saver.setup()
            yield saver
        finally:
            connection.close()
        return

    from langgraph.checkpoint.postgres import PostgresSaver

    dsn = database_url.replace("postgresql+psycopg://", "postgresql://")
    with PostgresSaver.from_conn_string(dsn) as saver:
        if setup:
            saver.setup()
        yield saver


def bootstrap(database_url: str) -> None:
    """Create every table this system needs, application and checkpointer alike. Idempotent, and
    the only place schema creation happens. Run it before starting a worker."""
    from ledgerline.db import engine
    from ledgerline.models import Base

    Base.metadata.create_all(engine())
    with checkpointer_for(database_url, setup=True):
        pass
