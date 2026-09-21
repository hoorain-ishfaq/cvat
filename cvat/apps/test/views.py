# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from collections import Counter

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from cvat.apps.engine.models import JobType, LabeledImage, LabeledShape, Task
from cvat.apps.engine.types import ExtendedRequest
from cvat.apps.test.permissions import TestPermission
from cvat.apps.test.serializers import (
    ClassCountsQuerySerializer,
    ClassCountsSerializer,
    PingSerializer,
)


class TestViewSet(viewsets.ViewSet):
    # Required by cvat.apps.iam.permissions.PolicyEnforcer, which is installed
    # globally via REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]. Without this
    # attribute the enforcer raises an AssertionError instead of a 403.
    iam_permission_class = TestPermission
    serializer_class = None

    @extend_schema(
        summary="Health check for the test analytics app",
        description="Returns 200 when the test app is installed and its OPA rules are loaded.",
        responses={"200": OpenApiResponse(PingSerializer, description="The app is reachable")},
    )
    @action(detail=False, methods=["GET"], url_path="ping")
    def ping(self, request: ExtendedRequest) -> Response:
        serializer = PingSerializer({"status": "ok", "app": "test"})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Class-wise image counts for a task",
        description=(
            "For each label of the task, returns how many distinct images (frames) contain "
            "at least one annotation with that label.\n\n"
            "An image holding several annotations of the same label is counted once. "
            "Both shapes and image-level tags are taken into account, and a frame carrying "
            "both for the same label is still counted once. Labels without any annotation "
            "are returned with a count of zero.\n\n"
            "Only annotation jobs are considered: ground truth and consensus replica jobs "
            "cover the same frames and would otherwise inflate the counts."
        ),
        parameters=[
            OpenApiParameter(
                "task_id",
                type=int,
                location=OpenApiParameter.QUERY,
                required=True,
                description="ID of the task to aggregate",
            )
        ],
        responses={"200": OpenApiResponse(ClassCountsSerializer, description="Class-wise counts")},
    )
    @action(detail=False, methods=["GET"], url_path="class-counts")
    def class_counts(self, request: ExtendedRequest) -> Response:
        query = ClassCountsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        task_id = query.validated_data["task_id"]

        task = get_object_or_404(Task, pk=task_id)

        # Ground truth and consensus replica jobs annotate the same frames as the
        # annotation jobs they validate, so including them would double-count images.
        annotation_jobs = {
            "job__segment__task_id": task_id,
            "job__type": JobType.ANNOTATION.value,
        }

        # A frame number is only unique within a job, so an image is identified by
        # the (job_id, frame) pair. Selecting the triple and letting the database
        # UNION it collapses duplicates two ways at once:
        #   - several annotations of one label on one frame -> one row
        #   - the same label present as both a shape and a tag -> one row
        shape_rows = LabeledShape.objects.filter(**annotation_jobs).values_list(
            "label_id", "job_id", "frame"
        )
        tag_rows = LabeledImage.objects.filter(**annotation_jobs).values_list(
            "label_id", "job_id", "frame"
        )

        distinct_rows = list(shape_rows.union(tag_rows))

        counts = Counter(label_id for label_id, _, _ in distinct_rows)
        annotated_images = len({(job_id, frame) for _, job_id, frame in distinct_rows})

        # Task.get_labels() resolves labels from the parent project when the task
        # belongs to one, and skips skeleton sublabels. Iterating over it is what
        # includes labels that have no annotations yet.
        classes = [
            {
                "label_id": label.id,
                "label_name": label.name,
                "color": label.color,
                "image_count": counts.get(label.id, 0),
            }
            for label in task.get_labels().order_by("id")
        ]

        serializer = ClassCountsSerializer(
            {
                "task_id": task_id,
                "total_images": task.data.size if task.data else 0,
                "annotated_images": annotated_images,
                "classes": classes,
            }
        )
        return Response(serializer.data, status=status.HTTP_200_OK)
