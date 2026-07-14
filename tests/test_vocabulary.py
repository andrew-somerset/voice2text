"""Tests for the vocabulary / learned-corrections store (pure, CI-safe)."""

from __future__ import annotations

from pathlib import Path

from voice2text.vocabulary import Vocabulary


def _vocab(tmp_path: Path) -> Vocabulary:
    return Vocabulary(path=tmp_path / "vocab.json")


class TestTerms:
    def test_add_term_is_new_then_duplicate(self, tmp_path: Path) -> None:
        v = _vocab(tmp_path)
        assert v.add_term("kubectl") is True
        assert v.add_term("kubectl") is False
        assert v.add_term("  KUBECTL ") is False  # case/space-insensitive dedup
        assert v.terms() == ["kubectl"]

    def test_empty_term_rejected(self, tmp_path: Path) -> None:
        assert _vocab(tmp_path).add_term("   ") is False

    def test_initial_prompt_includes_terms(self, tmp_path: Path) -> None:
        v = _vocab(tmp_path)
        assert v.initial_prompt() == ""
        v.add_term("kubectl")
        v.add_term("Postgres")
        assert v.initial_prompt() == "Vocabulary: kubectl, Postgres."


class TestSubstitutions:
    def test_learn_adds_sub_and_term(self, tmp_path: Path) -> None:
        v = _vocab(tmp_path)
        assert v.learn("cube control", "kubectl") is True
        assert v.substitutions() == {"cube control": "kubectl"}
        assert "kubectl" in v.terms()

    def test_apply_substitutions_case_insensitive(self, tmp_path: Path) -> None:
        v = _vocab(tmp_path)
        v.learn("cube control", "kubectl")
        assert v.apply_substitutions("Deploy with Cube Control now.") == "Deploy with kubectl now."

    def test_learn_noop_on_identical(self, tmp_path: Path) -> None:
        assert _vocab(tmp_path).learn("kubectl", "kubectl") is False

    def test_learn_noop_on_empty(self, tmp_path: Path) -> None:
        v = _vocab(tmp_path)
        assert v.learn("", "kubectl") is False
        assert v.learn("something", "  ") is False

    def test_longer_substitutions_win(self, tmp_path: Path) -> None:
        v = _vocab(tmp_path)
        v.learn("post", "POST")
        v.learn("post grey s q l", "PostgreSQL")
        # The longer phrase should be tried first so it is not pre-empted.
        assert v.apply_substitutions("use post grey s q l here") == "use PostgreSQL here"


class TestPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "vocab.json"
        v1 = Vocabulary(path=path)
        v1.add_term("kubectl")
        v1.learn("cube control", "kubectl")
        v2 = Vocabulary(path=path)
        assert "kubectl" in v2.terms()
        assert v2.substitutions() == {"cube control": "kubectl"}

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        v = Vocabulary(path=tmp_path / "does-not-exist.json")
        assert v.terms() == []
        assert v.substitutions() == {}

    def test_corrupt_file_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "vocab.json"
        path.write_text("{not valid json", encoding="utf-8")
        v = Vocabulary(path=path)
        assert v.terms() == []
