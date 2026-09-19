"""Custom test runner.

Defaults unittest discovery to the ``routes.tests`` package so that
``python manage.py test`` always works, even in environments where an
unrelated top-level ``tests`` module (for example a leaked namespace package
in site-packages) would otherwise confuse generic test discovery.

Explicit labels still work as usual, e.g. ``python manage.py test routes``.
"""

from django.test.runner import DiscoverRunner


class RoutesTestRunner(DiscoverRunner):
    default_test_labels = ["routes.tests"]

    def build_suite(self, test_labels=None, **kwargs):
        labels = list(test_labels) if test_labels else list(self.default_test_labels)
        return super().build_suite(test_labels=labels, **kwargs)
