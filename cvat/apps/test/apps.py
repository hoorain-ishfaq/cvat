# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from django.apps import AppConfig


class TestConfig(AppConfig):
    name = "cvat.apps.test"
    # Avoid clashing with the "test" label that Django's own tooling may expect.
    label = "cvat_test"

    def ready(self) -> None:
        from cvat.apps.iam.permissions import load_app_iam_rules

        # Registers this app's rules/ directory so the OPA bundle served at
        # /api/auth/rules includes test.rego. Without this every request is denied.
        load_app_iam_rules(self)
