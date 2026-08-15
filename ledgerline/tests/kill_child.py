"""Runs a graph in a child process and dies hard partway through.

Invoked by test_durability. Uses a real SIGKILL to itself rather than an exception or a clean exit,
because an exception unwinds and a clean exit flushes. Neither is a crash. SIGKILL cannot be caught,
cannot run a finally block, and cannot flush anything the checkpointer has not already written,
which is exactly the failure the resumption claim is about.

usage: python -m tests.kill_child <database_url> <fixtures_dir> <run_id> <kill_after_stage>
"""

from __future__ import annotations

import os
import signal
import sys


def main() -> None:
    database_url, fixtures_dir, run_id, kill_after = sys.argv[1:5]

    os.environ["LEDGERLINE_DATABASE_URL"] = database_url
    os.environ["LEDGERLINE_FIXTURES_DIR"] = fixtures_dir
    os.environ["LEDGERLINE_PLAYBOOK_PATH"] = "src/ledgerline/rules/playbook.yaml"
    os.environ["LEDGERLINE_MODEL_CLIENT"] = "replay"

    from ledgerline.agent.graph import build_graph, checkpointer_for
    from ledgerline.agent.stages import StageContext
    from ledgerline.agent.state import empty_state
    from ledgerline.config import get_settings
    from ledgerline.db import session_factory
    from ledgerline.llm.client import TieredModelClient, build_client
    from ledgerline.models import Run
    from ledgerline.rules.engine import load_playbook

    settings = get_settings()
    sessions = session_factory()

    with sessions() as session:
        run = session.get(Run, run_id)
        pile_id = run.pile_id
        document_ids = list(run.new_document_ids or [])

    context = StageContext(
        settings=settings,
        client=TieredModelClient(build_client(settings), settings),
        playbook=load_playbook(settings.playbook_path),
        sessions=sessions,
    )

    with checkpointer_for(settings.database_url) as checkpointer:
        graph = build_graph(context, checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        for event in graph.stream(
            empty_state(run_id, pile_id, document_ids), config, stream_mode="updates"
        ):
            for node in event:
                if node == kill_after:
                    # The checkpoint for this node is written by the time the stream yields it.
                    sys.stdout.flush()
                    os.kill(os.getpid(), signal.SIGKILL)

    sys.exit(0)


if __name__ == "__main__":
    main()
