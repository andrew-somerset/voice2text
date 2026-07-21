"""Application entry point for persistent local dictation and explicit setup routes."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

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
        "--list-models",
        action="store_true",
        help="list the reviewed Whisper models that setup can fetch and verify",
    )
    setup_actions.add_argument(
        "--setup-model",
        nargs="?",
        const="",
        metavar="MODEL",
        help="download and checksum-verify a reviewed model (default base.en) for local use",
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
    setup_actions.add_argument(
        "--test-local-dictation",
        action="store_true",
        help="transcribe locally and paste into the text box focused when recording began",
    )
    setup_actions.add_argument(
        "--start-background",
        action="store_true",
        help="start the persistent listener without a terminal window",
    )
    setup_actions.add_argument(
        "--stop-background",
        action="store_true",
        help="ask the persistent listener to shut down cleanly",
    )
    setup_actions.add_argument(
        "--background-status",
        action="store_true",
        help="show listener and current-user startup status",
    )
    setup_actions.add_argument(
        "--install-startup",
        action="store_true",
        help="start now and register the listener for this user's sign-in",
    )
    setup_actions.add_argument(
        "--uninstall-startup",
        action="store_true",
        help="remove sign-in startup and stop the current listener",
    )
    parser.add_argument(
        "--test-seconds",
        type=float,
        default=None,
        help="optional bounded duration for --test-recording-pill; default runs until stopped",
    )
    parser.add_argument(
        "--model-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="register an already-downloaded model file with --setup-model instead of fetching it",
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

    if args.list_models:
        from voice2text.model_settings import DEFAULT_MODEL_ID, managed_models

        print("Reviewed Whisper models (checksum-pinned):")
        for model in managed_models():
            marker = " (default)" if model.model_id == DEFAULT_MODEL_ID else ""
            print(f"  {model.model_id:<10} {model.display_name}{marker}")
            print(f"             {model.description}")
        print(
            "  Set up the default with: voice2text --setup-model. To register a file you already "
            "have: voice2text --setup-model --model-file <path>."
        )
        return 0

    if args.setup_model is not None:
        from voice2text.model_settings import DEFAULT_MODEL_ID, ModelSettingsError
        from voice2text.model_setup import ModelSetupError, setup_managed_model

        try:
            requested_model = args.setup_model.strip() or DEFAULT_MODEL_ID
            result = setup_managed_model(requested_model, source_file=args.model_file)
        except (ModelSetupError, ModelSettingsError) as exc:
            logging.basicConfig(level=logging.ERROR)
            logging.error("Could not set up the model: %s", exc)
            return 2
        verb = {
            "downloaded": "Downloaded and verified",
            "reused": "Verified existing",
            "registered": "Registered and verified",
        }[result.action]
        print(f"{verb} {result.model.display_name} at {result.settings.path}.")
        print("Local dictation will use this model automatically; run: voice2text")
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

    if any(
        (
            args.start_background,
            args.stop_background,
            args.background_status,
            args.install_startup,
            args.uninstall_startup,
        )
    ):
        from voice2text.background import (
            BackgroundError,
            LaunchResult,
            background_status,
            install_startup,
            launch_background,
            request_background_stop,
            uninstall_startup,
        )

        try:
            if args.background_status:
                status = background_status()
                print(f"Listener running: {'yes' if status.running else 'no'}")
                print(
                    "Start at sign-in: "
                    + (
                        "yes"
                        if status.startup_current
                        else "outdated"
                        if status.startup_installed
                        else "no"
                    )
                )
                return 0
            if args.stop_background:
                signaled = request_background_stop()
                print("Shutdown requested." if signaled else "No background listener is running.")
                return 0
            if args.uninstall_startup:
                uninstall_startup()
                request_background_stop()
                print("Start-at-sign-in registration removed; shutdown requested if running.")
                return 0
            if args.install_startup:
                install_startup()
                result = launch_background()
                if result is LaunchResult.FAILED:
                    uninstall_startup()
                    logging.basicConfig(level=logging.ERROR)
                    logging.error(
                        "Background listener did not become ready; startup was rolled back"
                    )
                    return 1
                print("Background listener is ready and will start at user sign-in.")
                return 0
            result = launch_background()
            if result is LaunchResult.FAILED:
                logging.basicConfig(level=logging.ERROR)
                logging.error("Background listener did not become ready")
                return 1
            print(
                "Background listener is already running."
                if result is LaunchResult.ALREADY_RUNNING
                else "Background listener is ready."
            )
            return 0
        except BackgroundError as exc:
            logging.basicConfig(level=logging.ERROR)
            logging.error("Background lifecycle operation failed: %s", exc)
            return 1

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

    from voice2text.local_runtime import LocalDictationError, run_local_dictation

    duration_seconds = args.test_seconds if args.test_local_dictation else None
    try:
        run_local_dictation(config, duration_seconds=duration_seconds)
    except (LocalDictationError, ValueError) as exc:
        logging.error("Local dictation failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
