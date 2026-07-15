"""Application entry point. Integration is added in the ordered milestones."""

from __future__ import annotations

import argparse
import logging

from voice2text.config import AppConfig, ConfigError
from voice2text.trigger_settings import (
    TriggerSettingsError,
    load_trigger_settings,
    save_trigger_settings,
    trigger_choice,
    trigger_choices,
)
from voice2text.trigger_setup import TriggerSetupError, choose_trigger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Windows dictation and Ask Glean")
    setup_actions = parser.add_mutually_exclusive_group()
    setup_actions.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and exit without opening the microphone",
    )
    setup_actions.add_argument(
        "--list-triggers",
        action="store_true",
        help="list the reviewed trigger choices and explain Fn-key limitations",
    )
    setup_actions.add_argument(
        "--configure-trigger",
        nargs="?",
        const="",
        metavar="CHOICE",
        help="open the trigger picker, or save a preset such as right-alt",
    )
    setup_actions.add_argument(
        "--test-recording-pill",
        action="store_true",
        help="test the selected trigger, microphone, and volume pill without transcription",
    )
    parser.add_argument(
        "--test-seconds",
        type=float,
        default=None,
        help="optional bounded duration for --test-recording-pill; default runs until stopped",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_triggers:
        print("Available trigger choices:")
        for choice in trigger_choices():
            print(f"  {choice.choice_id:<12} {choice.display_name}: {choice.description}")
        print(
            "  Fn is hardware-dependent and is not a universal choice: most laptop firmware "
            "does not expose Fn to Windows Raw Input."
        )
        return 0

    if args.configure_trigger is not None:
        try:
            requested_choice = args.configure_trigger.strip()
            if not requested_choice:
                saved_trigger = load_trigger_settings()
                initial_choice = (
                    saved_trigger.choice_id if saved_trigger is not None else "right-ctrl"
                )
                requested_choice = choose_trigger(initial_choice) or ""
                if not requested_choice:
                    print("Trigger setup cancelled; no setting was changed.")
                    return 1
            choice = trigger_choice(requested_choice)
            save_trigger_settings(choice.choice_id, suppress_chords=True)
        except (TriggerSettingsError, TriggerSetupError) as exc:
            logging.basicConfig(level=logging.ERROR)
            logging.error("Could not configure trigger: %s", exc)
            return 2
        print(
            f"Trigger saved: {choice.display_name}. Standalone-key grace and chord "
            "suppression are enabled."
        )
        return 0

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

    if args.test_recording_pill:
        from voice2text.recording_test import RecordingPillTestError, run_recording_pill_test

        try:
            run_recording_pill_test(config, duration_seconds=args.test_seconds)
        except (RecordingPillTestError, ValueError) as exc:
            logging.error("Recording pill test failed: %s", exc)
            return 1
        return 0

    print(
        f"voice2text Windows skeleton is ready with {config.trigger.display_name}; "
        "integration milestones are not wired yet."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
