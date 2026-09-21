# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from enum import StrEnum
from typing import Any

from django.conf import settings
from rest_framework.exceptions import NotFound
from rest_framework.viewsets import ViewSet

from cvat.apps.engine.models import Task
from cvat.apps.engine.permissions import TaskPermission
from cvat.apps.engine.types import ExtendedRequest
from cvat.apps.iam.permissions import IamContext, OpenPolicyAgentPermission


class TestPermission(OpenPolicyAgentPermission):
    class Scopes(StrEnum):
        VIEW = "view"

    @classmethod
    def create(
        cls,
        request: ExtendedRequest,
        view: ViewSet,
        obj: Any | None,
        iam_context: IamContext | None,
    ) -> list[OpenPolicyAgentPermission]:
        permissions = []
        for scope in cls.get_scopes(request, view, obj):
            permissions.append(cls.create_base_perm(request, view, scope, iam_context, obj))

        # Reading class counts exposes information about a task's annotations, so the
        # caller must also be allowed to view that task. Delegating to the engine's
        # TaskPermission keeps sandbox/organization rules consistent with the rest of
        # CVAT instead of reimplementing them here.
        if view.action == "class_counts":
            task_id = request.query_params.get("task_id")
            if task_id is not None:
                try:
                    task_id = int(task_id)
                except (TypeError, ValueError):
                    # Malformed input is reported by the view as a 400; skip the
                    # object-level check rather than raising an opaque error here.
                    task_id = None

                if task_id is not None and task_id >= 1:
                    # The task is resolved here rather than handing the id to
                    # TaskPermission, which raises ValidationError (400) for a
                    # missing task. Other task-scoped CVAT endpoints answer 404,
                    # so look it up and raise NotFound to stay consistent.
                    try:
                        task = Task.objects.select_related("organization").get(pk=task_id)
                    except Task.DoesNotExist as ex:
                        raise NotFound(f"Task {task_id} does not exist") from ex

                    permissions.append(TaskPermission.create_scope_view(request, task))

        return permissions

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Resolves to data.test.allow inside OPA (see rules/test.rego).
        self.url = settings.IAM_OPA_DATA_URL + "/test/allow"

    @classmethod
    def _get_scopes(cls, request: ExtendedRequest, view: ViewSet, obj: Any | None) -> list:
        Scopes = cls.Scopes
        return [
            {
                ("ping", "GET"): Scopes.VIEW,
                ("class_counts", "GET"): Scopes.VIEW,
            }[(view.action, request.method)]
        ]

    def get_resource(self) -> None:
        # These endpoints are not tied to an object of this app; object-level access
        # is delegated to TaskPermission above.
        return None
