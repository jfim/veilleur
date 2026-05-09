"""Background scheduler.

Polls Postgres for feeds that are due (``status='active'`` and
``last_scraped_at + poll_interval_seconds < now()``) and invokes
:func:`veilleur.pipeline.run_scrape` on them. Started from the FastAPI
lifespan in :mod:`veilleur.app`.
"""

from veilleur.scheduler.loop import SchedulerLoop, pick_due_feed

__all__ = ["SchedulerLoop", "pick_due_feed"]
