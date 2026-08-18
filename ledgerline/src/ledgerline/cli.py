"""Command line entry points. One command per thing a person actually needs to do."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer

from ledgerline.agent.graph import bootstrap
from ledgerline.config import get_settings

app = typer.Typer(add_completion=False, help="Ledgerline: vendor document file reconciliation.")


@app.command("init-db")
def init_db() -> None:
    """Create every table, application and checkpointer alike. Idempotent. Run before the worker."""
    settings = get_settings()
    bootstrap(settings.database_url)
    typer.echo(f"schema ready: {json.dumps(settings.safe_dump())}")


@app.command("worker")
def worker() -> None:
    """Claim and execute queued runs until stopped."""
    from ledgerline.worker import run_forever

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_forever()


@app.command("seed")
def seed(
    pile: str = typer.Option("northwind", help="Name for the pile."),
    out: Path = typer.Option(
        Path("./corpus"),
        help="Where to write the generated documents. Each variant gets its own subfolder here.",
    ),
    variant: str = typer.Option(
        "a", help="Corpus variant: 'a', 'b' or 'c' — three different vendors, three sizes."
    ),
) -> None:
    """Generate a synthetic vendor file and load it. No real third-party data, ever."""
    from ledgerline.corpus.generate import generate_and_load

    result = generate_and_load(pile_name=pile, out_dir=out, variant=variant)
    typer.echo(json.dumps(result, indent=2))


@app.command("run-latest")
def run_latest(pile: str = typer.Option(..., help="Name of the pile to run.")) -> None:
    """Queue and execute a run against a pile chosen by name rather than by id.

    Exists so scripts do not have to parse a pile id out of the seed command's JSON. A shell
    pipeline that greps an id out of JSON is a shell pipeline that breaks the first time the JSON
    changes shape.
    """
    from sqlalchemy import select

    from ledgerline.db import session_scope
    from ledgerline.models import Pile, Run, RunStatus
    from ledgerline.service import execute_run

    with session_scope() as session:
        found = session.scalars(select(Pile).where(Pile.name == pile)).first()
        if found is None:
            typer.echo(f"no pile named '{pile}'. Run `ledgerline seed --pile {pile}` first.")
            raise typer.Exit(code=1)
        queued = Run(pile_id=found.id, status=RunStatus.queued, new_document_ids=[])
        session.add(queued)
        session.flush()
        run_id = queued.id

    outcome = execute_run(run_id)
    typer.echo(
        json.dumps(
            {
                "run_id": outcome.run_id,
                "status": outcome.status.value,
                "stages": [entry["stage"] for entry in outcome.stage_log],
                "cost": outcome.cost_report,
            },
            indent=2,
            default=str,
        )
    )


@app.command("watch")
def watch(
    pile: str = typer.Option(..., help="Name of the pile new documents belong to."),
    directory: Path = typer.Option(Path("./inbox"), help="Folder to watch."),
    interval: float = typer.Option(2.0, help="Seconds between polls."),
) -> None:
    """Watch a folder. Each arrival queues a focused update run."""
    from ledgerline.watcher import watch_forever

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    watch_forever(pile, directory, interval)


@app.command("run")
def run(pile_id: str) -> None:
    """Queue a run and execute it in this process. Convenience for demos, not for production."""
    from ledgerline.db import session_scope
    from ledgerline.models import Run, RunStatus
    from ledgerline.service import execute_run

    with session_scope() as session:
        queued = Run(pile_id=pile_id, status=RunStatus.queued, new_document_ids=[])
        session.add(queued)
        session.flush()
        run_id = queued.id

    outcome = execute_run(run_id)
    typer.echo(
        json.dumps(
            {
                "run_id": outcome.run_id,
                "status": outcome.status.value,
                "degraded": outcome.degraded,
                "stages": [entry["stage"] for entry in outcome.stage_log],
                "cost": outcome.cost_report,
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    app()
