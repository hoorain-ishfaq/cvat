# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

"""A native ASGI WebSocket endpoint for live class-count updates.

CVAT does not ship Django Channels, and adding it would mean a new dependency
and a server image rebuild. uvicorn already speaks the WebSocket protocol and
redis-py already provides an async client, so the endpoint is implemented
directly against the ASGI interface instead.

Messages only announce that a task's counts changed; clients re-read the REST
endpoint to get the numbers.
"""

import asyncio
import logging
from urllib.parse import parse_qs

import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.http.cookie import parse_cookie

from cvat.apps.test.notifications import channel_for_task

logger = logging.getLogger(__name__)

WS_PATH = "/api/test/ws/class-counts"

# Application-defined close codes (4000-4999 is the range reserved for apps).
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_INTERNAL = 4500


def _header(scope, name: bytes) -> bytes:
    for key, value in scope.get("headers", []):
        if key == name:
            return value
    return b""


@sync_to_async
def _user_from_session(session_key: str):
    """Resolve the signed-in user from the handshake cookies.

    The UI authenticates with session cookies (axios runs with
    withCredentials), and browsers attach cookies to same-origin WebSocket
    handshakes, so no token needs to travel in the URL where it would end up
    in access logs.
    """
    if not session_key:
        return None

    session = SessionStore(session_key=session_key)
    user_id = session.get("_auth_user_id")
    if not user_id:
        return None

    try:
        return get_user_model().objects.get(pk=user_id, is_active=True)
    except get_user_model().DoesNotExist:
        return None


@sync_to_async
def _may_view_task(user, task_id: int) -> bool:
    """Run the same OPA check the REST endpoint performs.

    There is no DRF request here, so the IAM context is assembled explicitly
    from the user and the task's organization rather than faked onto a request
    object.
    """
    from cvat.apps.engine.models import Task
    from cvat.apps.engine.permissions import TaskPermission
    from cvat.apps.organizations.models import Membership

    try:
        task = Task.objects.select_related("organization").get(pk=task_id)
    except Task.DoesNotExist:
        return False

    roles = {role: priority for priority, role in enumerate(settings.IAM_ROLES)}
    groups = sorted(
        user.groups.filter(name__in=list(roles.keys())),
        key=lambda group: roles[group.name],
    )
    privilege = groups[0].name if groups else None

    organization = task.organization
    membership = None
    if organization is not None:
        membership = Membership.objects.filter(
            organization=organization, user=user, is_active=True
        ).first()

    permission = TaskPermission(
        scope=TaskPermission.Scopes.VIEW,
        obj=task,
        user_id=user.id,
        group_name=privilege,
        org_specified=False,
        org_id=getattr(organization, "id", None),
        org_slug=getattr(organization, "slug", None),
        org_owner_id=organization.owner_id if organization else None,
        org_role=getattr(membership, "role", None),
    )

    return permission.check_access().allow


class ClassCountsConsumer:
    """Streams class-count change events for a single task."""

    def __init__(self, scope, receive, send):
        self.scope = scope
        self.receive = receive
        self.send = send
        self.task_id: int | None = None

    async def __call__(self) -> None:
        message = await self.receive()
        if message["type"] != "websocket.connect":
            return

        task_id = self._parse_task_id()
        if task_id is None:
            await self._close(CLOSE_BAD_REQUEST)
            return

        cookies = parse_cookie(_header(self.scope, b"cookie").decode("latin-1"))
        user = await _user_from_session(cookies.get(settings.SESSION_COOKIE_NAME, ""))
        if user is None:
            await self._close(CLOSE_UNAUTHENTICATED)
            return

        if not await _may_view_task(user, task_id):
            await self._close(CLOSE_FORBIDDEN)
            return

        self.task_id = task_id
        await self.send({"type": "websocket.accept"})
        await self._stream()

    def _parse_task_id(self) -> int | None:
        raw = parse_qs(self.scope.get("query_string", b"").decode()).get("task_id", [None])[0]
        try:
            task_id = int(raw)
        except (TypeError, ValueError):
            return None

        return task_id if task_id >= 1 else None

    async def _close(self, code: int) -> None:
        # Closing before accepting makes the handshake fail with a plain HTTP
        # 403 and the application close code never reaches the browser, which
        # would see an indistinguishable 1006. Completing the handshake first
        # is what lets the client tell "not allowed, stop retrying" apart from
        # "connection dropped, retry". Nothing is ever sent on these sockets.
        await self.send({"type": "websocket.accept"})
        await self.send({"type": "websocket.close", "code": code})

    async def _stream(self) -> None:
        """Pump Redis messages to the client until either side goes away."""
        client = aioredis.Redis(
            host=settings.REDIS_INMEM_SETTINGS["HOST"],
            port=int(settings.REDIS_INMEM_SETTINGS["PORT"]),
            password=settings.REDIS_INMEM_SETTINGS["PASSWORD"] or None,
        )
        pubsub = client.pubsub()

        reader = None
        try:
            await pubsub.subscribe(channel_for_task(self.task_id))

            # The client half only has to notice websocket.disconnect; the
            # Redis half does the actual work. Racing them lets either end
            # terminate the connection promptly.
            reader = asyncio.create_task(self._forward(pubsub))
            closer = asyncio.create_task(self._wait_for_disconnect())
            done, pending = await asyncio.wait(
                {reader, closer}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.exception()
        except Exception:  # pylint: disable=broad-except
            logger.warning("class-counts socket failed for task %s", self.task_id, exc_info=True)
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
            try:
                await pubsub.aclose()
            except Exception:  # pylint: disable=broad-except
                pass
            try:
                await client.aclose()
            except Exception:  # pylint: disable=broad-except
                pass

    async def _forward(self, pubsub) -> None:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue

            payload = message["data"]
            if isinstance(payload, bytes):
                payload = payload.decode()

            await self.send({"type": "websocket.send", "text": payload})

    async def _wait_for_disconnect(self) -> None:
        while True:
            message = await self.receive()
            if message["type"] == "websocket.disconnect":
                return


async def websocket_application(scope, receive, send) -> None:
    """Entry point used by cvat/asgi.py for scope['type'] == 'websocket'."""
    if scope.get("path", "").rstrip("/") != WS_PATH:
        # Consume the connect message first, then accept/close so the code is
        # delivered (see ClassCountsConsumer._close).
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4404})
        return

    try:
        await ClassCountsConsumer(scope, receive, send)()
    except Exception:  # pylint: disable=broad-except
        logger.exception("unhandled error in the class-counts websocket")
        try:
            await send({"type": "websocket.close", "code": CLOSE_INTERNAL})
        except Exception:  # pylint: disable=broad-except
            pass
