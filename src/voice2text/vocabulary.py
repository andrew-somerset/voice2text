"""Persistent custom-vocabulary and learned-corrections store.

Two things live here, both persisted as JSON at ``config.VOCAB_PATH``:

* **terms** — words/phrases (software names, jargon) fed to whisper as an
  ``initial_prompt`` so it biases toward those spellings. Probabilistic: it
  nudges, it does not guarantee.
* **substitutions** — literal ``wrong -> right`` fixes applied to the
  transcript after inference. Deterministic: once learned, always applied, in
  every app.

Pure stdlib (no macOS deps) so it is unit-testable on CI. The store is
thread-safe; the worker thread reads it every utterance and the correction
watcher writes to it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from voice2text import config

logger = logging.getLogger(__name__)

# Keep the biasing prompt bounded — whisper only reads ~224 prompt tokens.
_MAX_PROMPT_CHARS = 800


def _normalize(phrase: str) -> str:
    """Lowercase + collapse whitespace, for case-insensitive matching/dedup."""
    return re.sub(r"\s+", " ", phrase).strip().lower()


class Vocabulary:
    """Load/save custom terms and learned substitutions."""

    def __init__(self, path: Path = config.VOCAB_PATH) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._terms: list[str] = []
        self._subs: dict[str, str] = {}  # normalized wrong -> right (display form)
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            terms = data.get("terms", [])
            subs = data.get("substitutions", {})
            if isinstance(terms, list):
                self._terms = [str(t) for t in terms if str(t).strip()]
            if isinstance(subs, dict):
                self._subs = {_normalize(k): str(v) for k, v in subs.items() if str(v).strip()}

    def _save(self) -> None:
        with self._lock:
            payload = {"terms": self._terms, "substitutions": self._subs}
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self._path)
            except OSError:
                logger.exception("could not write vocabulary to %s", self._path)

    def terms(self) -> list[str]:
        with self._lock:
            return list(self._terms)

    def substitutions(self) -> dict[str, str]:
        with self._lock:
            return dict(self._subs)

    def add_term(self, term: str) -> bool:
        """Add a biasing term. Returns True if it was new."""
        term = re.sub(r"\s+", " ", term).strip()
        if not term:
            return False
        with self._lock:
            if any(_normalize(t) == _normalize(term) for t in self._terms):
                return False
            self._terms.append(term)
            self._save()
        logger.info("vocabulary: added term %r", term)
        return True

    def learn(self, wrong: str, right: str) -> bool:
        """Learn a ``wrong -> right`` correction and add ``right`` as a term.

        Returns True if anything changed. No-ops on empty/identical input.
        """
        wrong_n = _normalize(wrong)
        right = re.sub(r"\s+", " ", right).strip()
        if not wrong_n or not right or wrong_n == _normalize(right):
            return False
        with self._lock:
            sub_changed = self._subs.get(wrong_n) != right
            self._subs[wrong_n] = right
            term_new = not any(_normalize(t) == _normalize(right) for t in self._terms)
            if term_new:
                self._terms.append(right)
            if sub_changed or term_new:
                self._save()
        if sub_changed:
            logger.info("vocabulary: learned %r -> %r", wrong, right)
        if term_new:
            logger.info("vocabulary: added term %r", right)
        return sub_changed or term_new

    def forget(self, wrong: str) -> bool:
        """Remove a learned ``wrong -> right`` substitution. Returns True if found."""
        wrong_n = _normalize(wrong)
        with self._lock:
            if wrong_n not in self._subs:
                return False
            del self._subs[wrong_n]
            self._save()
        logger.info("vocabulary: forgot correction for %r", wrong)
        return True

    def initial_prompt(self) -> str:
        """A biasing prompt built from the terms, or "" if there are none."""
        with self._lock:
            if not self._terms:
                return ""
            prompt = "Vocabulary: " + ", ".join(self._terms) + "."
        return prompt[:_MAX_PROMPT_CHARS]

    def apply_substitutions(self, text: str) -> str:
        """Apply learned ``wrong -> right`` fixes to ``text`` (case-insensitive)."""
        if not text:
            return text
        with self._lock:
            items = sorted(self._subs.items(), key=lambda kv: len(kv[0]), reverse=True)
        for wrong_n, right in items:
            pattern = re.compile(rf"\b{re.escape(wrong_n)}\b", re.IGNORECASE)
            text = pattern.sub(lambda _m, r=right: r, text)
        return text


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    vocab = Vocabulary()

    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage:\n"
            "  python -m voice2text.vocabulary list\n"
            "  python -m voice2text.vocabulary add <term>\n"
            "  python -m voice2text.vocabulary learn <wrong words> :: <right words>\n"
            "  python -m voice2text.vocabulary forget <wrong words>\n"
            f"\nstore: {config.VOCAB_PATH}"
        )
        raise SystemExit(0)

    cmd = argv[0]
    if cmd == "list":
        print("terms:", ", ".join(vocab.terms()) or "(none)")
        subs = vocab.substitutions()
        print("substitutions:", ", ".join(f"{k} -> {v}" for k, v in subs.items()) or "(none)")
    elif cmd == "add" and len(argv) >= 2:
        term = " ".join(argv[1:])
        print(f"added {term!r}" if vocab.add_term(term) else f"{term!r} already present")
    elif cmd == "learn" and "::" in argv:
        i = argv.index("::")
        wrong, right = " ".join(argv[1:i]), " ".join(argv[i + 1 :])
        print(
            f"learned {wrong!r} -> {right!r}" if vocab.learn(wrong, right) else "nothing to learn"
        )
    elif cmd == "forget" and len(argv) >= 2:
        wrong = " ".join(argv[1:])
        print(f"forgot {wrong!r}" if vocab.forget(wrong) else f"no correction for {wrong!r}")
    else:
        print("bad usage; run with --help", file=sys.stderr)
        raise SystemExit(2)
