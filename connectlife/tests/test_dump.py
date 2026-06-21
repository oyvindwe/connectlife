"""Tests for the dump tool's helpers."""

from __future__ import annotations

import unittest

from connectlife.dump import _redact


class TestRedact(unittest.TestCase):
    """The static dump must scrub known identifiers wherever they appear."""

    def test_replaces_known_identifiers_recursively(self) -> None:
        secrets = {"pu000150000000000007395190001202200023730262", "dev-123"}
        payload = {
            "resultCode": 0,
            "puid": "pu000150000000000007395190001202200023730262",
            "dev_type": "015",
            "data": {"Variant_code": "x", "ids": ["dev-123", "keep-me"]},
        }
        self.assertEqual(
            _redact(payload, secrets),
            {
                "resultCode": 0,
                "puid": "<redacted>",
                "dev_type": "015",
                "data": {"Variant_code": "x", "ids": ["<redacted>", "keep-me"]},
            },
        )

    def test_leaves_non_identifier_values_untouched(self) -> None:
        self.assertEqual(
            _redact({"a": "1", "b": [{"c": "2"}]}, {"secret"}),
            {"a": "1", "b": [{"c": "2"}]},
        )

    def test_empty_secrets_is_a_noop(self) -> None:
        payload = {"puid": "pu-123", "data": {"x": "1"}}
        self.assertEqual(_redact(payload, set()), payload)
