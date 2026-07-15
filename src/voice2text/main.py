"""Application entry point. Integration is added in the ordered milestones."""

from __future__ import annotations

import argparse
import logging

from voice2text.config import AppConfig, ConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Windows dictation and Ask Glean")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and exit without opening the microphone",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = AppConfig.from_environment()
    except (ConfigError, ValueError) as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.error("Invalid configuration: %s", exc)
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose or config.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.check_config:
        print("voice2text configuration is valid")
        return 0

    print("voice2text Windows skeleton is ready; integration milestones are not wired yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
