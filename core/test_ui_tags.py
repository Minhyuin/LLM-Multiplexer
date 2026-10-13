from decimal import Decimal

from django.template import Context, Template
from django.test import SimpleTestCase

from core.templatetags.ui_tags import at_most


class AtMostFilterTests(SimpleTestCase):
    """Number displays keep at most four decimal places (no float tails, no zero padding)."""

    def test_accumulated_float_artefacts_are_trimmed(self):
        self.assertEqual(at_most(Decimal("4.354000000000000"), 4), "4.354")
        self.assertEqual(at_most(0.30000000000000004, 4), "0.3")
        self.assertEqual(at_most(Decimal("4.354000"), 4), "4.354")

    def test_short_values_are_unchanged_and_empty_inputs_read_zero(self):
        for value, expected in ((0, "0"), (0.0, "0"), (12, "12"), (46.5, "46.5"), (None, "0"), ("", "0")):
            with self.subTest(value=value):
                self.assertEqual(at_most(value, 4), expected)

    def test_rounding_is_half_up_and_capped_at_the_limit(self):
        self.assertEqual(at_most(Decimal("0.00005"), 4), "0.0001")
        self.assertEqual(at_most(Decimal("0.000049"), 4), "0")
        self.assertEqual(at_most(1234.56789, 4), "1234.5679")
        self.assertEqual(at_most(Decimal("999999.999999"), 4), "1000000")

    def test_non_numeric_and_unparseable_arguments_pass_through(self):
        self.assertEqual(at_most("abc", 4), "abc")
        self.assertEqual(at_most(Decimal("1.234567"), "nonsense"), "1.2346")

    def test_template_usage_goes_through_the_filter(self):
        template = Template("{% load ui_tags %}${{ value|at_most:4 }}")
        rendered = template.render(Context({"value": Decimal("4.354000000000000")}))
        self.assertEqual(rendered, "$4.354")
