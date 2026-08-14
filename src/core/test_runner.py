from django.test.runner import DiscoverRunner


class CoreDiscoverRunner(DiscoverRunner):
    default_test_labels = ("core.tests",)

    def build_suite(self, test_labels=None, **kwargs):
        return super().build_suite(
            test_labels or self.default_test_labels,
            **kwargs,
        )
