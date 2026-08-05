"""Sentinel files coordinating the matcher and the web UI.

The two containers share /data and talk through two files:

  * .run-now          — webui asks the matcher's schedule loop to wake up
  * .matcher-running  — matcher signals a run is in progress

This module is deliberately dependency-free (no plexapi, no httpx) so the
webui can import it without dragging in the whole matcher graph.

The running flag is self-expiring: the matcher refreshes its mtime while a
run is active, and readers treat a flag whose mtime is older than
``RUNNING_FLAG_STALE_SECONDS`` as dead. This is what recovers the UI after a
SIGKILL / OOM-kill / `docker compose down` mid-run, none of which execute the
matcher's `finally` cleanup.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# Must be comfortably larger than the heartbeat interval so a busy event loop
# never lets a live run be declared dead.
RUNNING_FLAG_STALE_SECONDS = 10 * 60
HEARTBEAT_INTERVAL_SECONDS = 60.0


def _data_dir(config) -> Path:
    return Path(config.output.path).parent


def wake_file(config) -> Path:
    return _data_dir(config) / ".run-now"


def running_flag(config) -> Path:
    return _data_dir(config) / ".matcher-running"


def matcher_running(config) -> bool:
    """True only while the flag exists *and* is fresh. Both the webui status
    display and the /api/run guard must use this, never a bare exists()."""
    try:
        mtime = running_flag(config).stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) <= RUNNING_FLAG_STALE_SECONDS


def write_running_flag(config) -> Path:
    """Create the flag with a diagnostic payload (who/when), returning it.
    The payload is informational; liveness is judged by mtime alone."""
    flag = running_flag(config)
    try:
        flag.write_text(json.dumps({
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }))
    except OSError:
        pass
    return flag


def clear_running_flag(config) -> None:
    try:
        running_flag(config).unlink(missing_ok=True)
    except OSError:
        pass


async def heartbeat(flag: Path) -> None:
    """Refresh the flag's mtime forever; run as a task and cancel when done."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            os.utime(flag, None)
        except OSError:
            pass
