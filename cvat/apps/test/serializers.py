# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from rest_framework import serializers


class PingSerializer(serializers.Serializer):
    status = serializers.CharField()
    app = serializers.CharField()


class ClassCountSerializer(serializers.Serializer):
    label_id = serializers.IntegerField()
    label_name = serializers.CharField()
    color = serializers.CharField()
    image_count = serializers.IntegerField(
        help_text="Number of distinct images (frames) containing at least one annotation "
        "with this label. An image with several annotations of the same label counts once."
    )


class ClassCountsSerializer(serializers.Serializer):
    task_id = serializers.IntegerField()
    total_images = serializers.IntegerField(help_text="Total number of frames in the task")
    annotated_images = serializers.IntegerField(
        help_text="Number of distinct frames carrying at least one annotation"
    )
    classes = ClassCountSerializer(many=True)


class ClassCountsQuerySerializer(serializers.Serializer):
    task_id = serializers.IntegerField(min_value=1, help_text="ID of the task to aggregate")
