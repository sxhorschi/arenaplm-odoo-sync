"""Sync scheduler: thin wrapper around transfer engine.

Exposes start/stop for the background auto-sync scheduler.
All actual sync logic lives in transfer.py.
"""

import logging
import threading
import time

import schedule

from transfer import run_full_sync, load_state, save_state

logger = logging.getLogger(__name__)

# Re-export for backward compat
__all__ = ["load_state", "save_state", "run_full_sync", "start_scheduler", "stop_scheduler"]

_scheduler_active = False
_scheduler_thread: threading.Thread | None = None


def start_scheduler(
    interval_minutes: int,
    arena_factory: callable,
    odoo_factory: callable,
    mapping_config_factory: callable,
    on_activity: callable = None,
) -> None:
    """Start the background auto-sync scheduler.

    Args:
        interval_minutes: How often to run (in minutes)
        arena_factory: Callable that returns an authenticated ArenaClient
        odoo_factory: Callable that returns an authenticated OdooClient
        mapping_config_factory: Callable that returns current mapping config
        on_activity: Optional callback(level, message) for activity logging
    """
    global _scheduler_active, _scheduler_thread

    if _scheduler_active:
        return

    _scheduler_active = True
    schedule.clear()

    def job():
        try:
            arena = arena_factory()
            odoo = odoo_factory()
            mapping_cfg = mapping_config_factory()
            return run_full_sync(arena, odoo, mapping_cfg, on_activity)
        except Exception as e:
            logger.error("Scheduled sync failed: %s", e, exc_info=True)
            if on_activity:
                on_activity("ERROR", f"Scheduled sync failed: {e}")
            return None

    schedule.every(interval_minutes).minutes.do(job)

    def loop():
        while _scheduler_active:
            schedule.run_pending()
            time.sleep(1)

    _scheduler_thread = threading.Thread(target=loop, daemon=True)
    _scheduler_thread.start()
    logger.info("Auto-sync scheduler started: every %d min", interval_minutes)


def stop_scheduler() -> None:
    global _scheduler_active
    _scheduler_active = False
    schedule.clear()
    logger.info("Auto-sync scheduler stopped")


def is_scheduler_active() -> bool:
    return _scheduler_active
