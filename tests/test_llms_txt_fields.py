#!/usr/bin/env python3
"""
The First Wall — llms.txt ↔ Schema Field Agreement
==================================================

llms.txt is the field guide agents read before submitting a dossier. If its
field list disagrees with `schemas/dossier.schema.json`'s `required` array, an
agent that follows the guide submits a dossier the verifier rejects (the exact
drift behind audit item B-5). This test fails whenever the documented field
list and the enforced schema drift apart.
"""

import json
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))


def documented_fields():
    with open(os.path.join(REPO, "llms.txt"), encoding="utf-8") as fh:
        text = fh.read()
    section = text.split("## Inscription Schema", 1)[1]
    return [m.group(1) for m in re.finditer(r"^- `([a-z_]+)`:", section, re.MULTILINE)]


def required_fields():
    with open(os.path.join(REPO, "schemas", "dossier.schema.json"), encoding="utf-8") as fh:
        schema = json.load(fh)
    return list(schema["required"])


class LlmsTxtFieldAgreement(unittest.TestCase):

    def test_every_required_field_is_documented(self):
        self.assertEqual(set(documented_fields()), set(required_fields()),
                         "llms.txt field list and schema required array disagree")

    def test_documented_fields_follow_schema_order(self):
        self.assertEqual(documented_fields(), required_fields(),
                         "llms.txt field list should follow the schema required order")


if __name__ == "__main__":
    unittest.main()
