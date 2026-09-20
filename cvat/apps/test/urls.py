# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from django.urls import include, path
from rest_framework import routers

from cvat.apps.test import views

router = routers.DefaultRouter(trailing_slash=False)
router.register("test", views.TestViewSet, basename="test")

urlpatterns = [
    # entry point for API
    path("", include(router.urls)),
]
