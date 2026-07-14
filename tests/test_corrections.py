"""Tests for the pure correction-diff logic (CI-safe, no Accessibility)."""

from __future__ import annotations

from voice2text.corrections import diff_corrections


def test_single_word_correction() -> None:
    # A genuine spelling fix (not just capitalization) is learned.
    assert diff_corrections(
        "open kubernetis file", "open kubernetes file", "open kubernetis file"
    ) == [("kubernetis", "kubernetes")]


def test_multiword_to_single() -> None:
    assert diff_corrections(
        "Deploy with cube control now.",
        "Deploy with kubectl now.",
        "Deploy with cube control now.",
    ) == [("cube control", "kubectl")]


def test_ignores_edits_to_non_pasted_text() -> None:
    # "apple" was not in what we pasted, so changing it is not a correction.
    assert diff_corrections("apple cube control", "orange cube control", "cube control") == []


def test_no_change_returns_empty() -> None:
    assert diff_corrections("hello world", "hello world", "hello world") == []


def test_pure_insertion_is_not_a_correction() -> None:
    assert diff_corrections("hello world", "hello there world", "hello world") == []


def test_large_rewrite_ignored() -> None:
    before = "one two three"
    after = "a b c d e f"
    assert diff_corrections(before, after, before) == []


def test_case_only_change_ignored() -> None:
    # core words identical ignoring case -> not learned (substitution is
    # already case-insensitive, so there is nothing to fix).
    assert diff_corrections("the api", "the API", "the api") == []


def test_empty_pasted_returns_empty() -> None:
    assert diff_corrections("a b", "a c", "") == []
