# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

"""Fan-out of "class counts changed" events over the existing Redis instance.

The payload deliberately carries no counts. Clients treat a message as a hint
to re-read /api/test/class-counts, which keeps the REST endpoint the single
source of truth and its permission checks the single gate on the data.
"""

import json
import logging

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "cvat:class-counts:task:"

# A bulk annotation save touches many rows; without a cooldown a single save
# would publish a burst of identical events. The key expires on its own, so no
# cleanup is needed.
_DEBOUNCE_KEY_PREFIX = "cvat:class-counts:debounce:task:"
_DEBOUNCE_SECONDS = 1


def channel_for_task(task_id: int) -> str:
    return f"{CHANNEL_PREFIX}{task_id}"


def _build_client() -> redis.Redis:
    return redis.Redis(
        host=settings.REDIS_INMEM_SETTINGS["HOST"],
        port=int(settings.REDIS_INMEM_SETTINGS["PORT"]),
        password=settings.REDIS_INMEM_SETTINGS["PASSWORD"] or None,
        # Never let a slow or unreachable Redis hold up an annotation save.
        socket_timeout=2,
        socket_connect_timeout=2,
    )


def publish_class_counts_changed(task_id: int) -> None:
    """Announce that a task's annotations changed.

    Safe to call from anywhere: every failure is swallowed and logged, because
    a missed notification must never turn into a failed annotation save. The
    worst case is a client showing stale numbers until its next refresh.
    """
    try:
        client = _build_client()

        # set(nx=True) is atomic, so only the first caller within the window
        # publishes even when several uvicorn workers save at once.
        if not client.set(f"{_DEBOUNCE_KEY_PREFIX}{task_id}", 1, nx=True, ex=_DEBOUNCE_SECONDS):
            return

        client.publish(
            channel_for_task(task_id),
            json.dumps({"type": "class_counts_changed", "task_id": task_id}),
        )
    except Exception:  # pylint: disable=broad-except
        logger.warning("Could not publish class-counts update for task %s", task_id, exc_info=True)
