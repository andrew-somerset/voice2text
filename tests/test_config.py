"""Sanity checks for config constants."""

from voice2text import config


def test_sample_rate_is_whisper_native() -> None:
    assert config.SAMPLE_RATE == 16_000


def test_mono_capture() -> None:
    assert config.CHANNELS == 1
    assert config.DTYPE == "float32"


def test_model_name_is_nonempty_constant() -> None:
    assert isinstance(config.MODEL_NAME, str)
    assert config.MODEL_NAME


def test_min_utterance_threshold_reasonable() -> None:
    assert 0.05 <= config.MIN_UTTERANCE_SECONDS <= 1.0


def test_fn_flag_mask_is_secondary_fn() -> None:
    assert config.FN_FLAG_MASK == 0x800000


def test_performance_core_count_positive() -> None:
    assert config.performance_core_count() >= 1
