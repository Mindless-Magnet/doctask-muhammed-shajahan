"""Run worker. Claims queued runs, executes them, requeues the ones dead workers left behind.

Deliberately not Celery, not RQ, not a broker. Postgres already gives us `FOR UPDATE SKIP LOCKED`,
which is all a claim loop needs, and a small team should not operate a message broker to run a
handful of jobs a minute. If throughput ever justifies one, the claim function is the only thing
that changes.

Runs execute here rather than in the API process so a crash is a real crash to recover from and an
API request never waits on a model call.
"""

from __future__ import annotations

import logging
import signal
import time

from ledgerline.config import get_settings
from ledgerline.db import session_scope
from ledgerline.models import RunStatus
from ledgerline.service import claim_next_run, execute_run, requeue_stale_runs, worker_id

log = logging.getLogger("ledgerline.worker")

_stopping = False


def _handle_signal(signum, _frame) -> None:
    global _stopping
    _stopping = True
    log.info("received signal %s, finishing current run then stopping", signum)


def run_forever() -> None:
    settings = get_settings()
    worker = worker_id()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    log.info("worker %s started with %s", worker, settings.safe_dump())

    while not _stopping:
        with session_scope() as session:
            requeued = requeue_stale_runs(session)
        if requeued:
            log.warning("requeued %d stale run(s) left by a dead worker", requeued)

        with session_scope() as session:
            run = claim_next_run(session, worker=worker)
            run_id = None if run is None else run.id

        if run_id is None:
            time.sleep(settings.worker_poll_seconds)
            continue

        log.info("claimed run %s", run_id)
        try:
            outcome = execute_run(run_id, settings)
            log.info(
                "run %s reached %s, %d model call(s), $%.4f",
                run_id,
                outcome.status.value,
                outcome.cost_report.get("total_calls", 0),
                outcome.cost_report.get("total_cost_usd", 0.0),
            )
        except Exception as exc:
            log.exception("run %s failed", run_id)
            with session_scope() as session:
                from ledgerline.models import Run

                failed = session.get(Run, run_id)
                if failed is not None:
                    failed.status = RunStatus.failed
                    # The actual cause, stored where every surface can read it. "See the worker
                    # log" is not an error message: it is an instruction to go somewhere the person
                    # reading it usually cannot reach, which for anyone running this in Docker is
                    # every time.
                    failed.error = f"{type(exc).__name__}: {exc}"[:4000]
                    session.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_forever()
