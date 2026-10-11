from __future__ import annotations

from email import policy
from email.headerregistry import BaseHeader
from email.message import Message
from email.parser import BytesParser, Parser
from email.policy import Policy

import pytest

from pip._internal.metadata._json import msg_to_json


@pytest.mark.parametrize("parser_policy", [policy.compat32, policy.default])
def test_repeated_fields_keep_order_and_first_single_value(
    parser_policy: Policy,
) -> None:
    message = Parser(policy=parser_policy).parsestr(
        "Name: demo\n"
        "nAmE: ignored\n"
        "requires-dist: first>=1\n"
        "Requires-Dist: second\n"
        "Project-URL: Docs, https://example.org/docs\n"
        "PROJECT-URL: Source, https://example.org/src\n"
        "Keywords: one, two\n"
        "X-Unused: value\n"
        "Description: header description\n"
        "\n"
        "Body description\n"
    )
    assert msg_to_json(message) == {
        "name": "demo",
        "requires_dist": ["first>=1", "second"],
        "project_url": [
            "Docs, https://example.org/docs",
            "Source, https://example.org/src",
        ],
        "keywords": ["one", "two"],
        "description": "Body description\n",
    }


@pytest.mark.parametrize(
    "raw, expected",
    [(b"\xc3\xbc", "ü"), (b"\xfc", "ü")],
)
def test_raw_non_ascii_headers_use_policy_decoding(raw: bytes, expected: str) -> None:
    message = BytesParser().parsebytes(b"Name: demo\nSummary: " + raw + b"\n")
    assert msg_to_json(message) == {"name": "demo", "summary": expected}


def test_folded_description_and_space_separated_keywords() -> None:
    message = Parser().parsestr(
        "Name: demo\nKeywords: one two\nDescription: first\n second\n"
    )
    assert msg_to_json(message) == {
        "name": "demo",
        "keywords": ["one", "two"],
        "description": "first\n second",
    }


def test_empty_values_are_different_from_missing_fields() -> None:
    message = Parser().parsestr("Name: \nRequires-Dist: \n")
    assert msg_to_json(message) == {"name": "", "requires_dist": [""]}


def test_present_none_value_is_not_missing() -> None:
    message: Message[str, str | None] = Message()
    message["Name"] = None
    assert msg_to_json(message) == {"name": "None"}


def test_ignored_single_use_duplicate_is_not_decoded() -> None:
    def header_factory(name: str, value: str) -> BaseHeader:
        if value == "ignored":
            raise ValueError("Ignored duplicate was decoded")
        header = policy.default.header_factory(name, value)
        assert isinstance(header, BaseHeader)
        return header

    parser_policy = policy.default.clone(header_factory=header_factory)
    message = Parser(policy=parser_policy).parsestr("Name: demo\nName: ignored\n")
    assert msg_to_json(message) == {"name": "demo"}
