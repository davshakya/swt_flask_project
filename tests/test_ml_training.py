from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from train_level_forecast_model import build_training_config, parse_args


def test_parse_args_defaults_to_short_pilot_min_samples():
    args = parse_args([])
    assert args.min_samples == 50


def test_build_training_config_uses_small_window_profile():
    config = build_training_config(49)

    assert config.profile == "small_window"
    assert config.params["max_depth"] == 3
    assert config.params["min_samples_leaf"] == 5
    assert config.params["early_stopping"] is False


def test_build_training_config_uses_medium_window_profile():
    config = build_training_config(100)

    assert config.profile == "medium_window"
    assert config.params["max_depth"] == 4
    assert config.params["min_samples_leaf"] == 10
    assert config.params["early_stopping"] is False


def test_build_training_config_uses_standard_profile():
    config = build_training_config(120)

    assert config.profile == "standard"
    assert config.params["max_depth"] == 6
    assert config.params["min_samples_leaf"] == 20
    assert config.params["early_stopping"] is True
