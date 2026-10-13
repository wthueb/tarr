import json
import pathlib
import subprocess
import sys

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

from tarr.config import Config
from tarr.schema import SCHEMA_DIRECTIVE, config_json_schema, config_yaml_example

ROOT = pathlib.Path(__file__).parents[1]


@pytest.fixture
def validator():
    schema = json.loads((ROOT / "config.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture
def raw_config():
    return config_json_schema()["examples"][1]


def test_schema_matches_configuration_models():
    assert json.loads((ROOT / "config.schema.json").read_text()) == config_json_schema()


@pytest.mark.parametrize("full", [False, True])
def test_yaml_examples_match_schema_and_runtime(validator, full):
    document = config_yaml_example(full=full)
    assert document.splitlines()[0] == SCHEMA_DIRECTIVE
    raw = yaml.safe_load(document)
    validator.validate(raw)
    config = Config.model_validate(raw)
    if not full:
        assert set(raw) == {"qbittorrent", "trackers"}
        for job in (
            "remove_unregistered",
            "remove_stopped",
            "maintain_free_space",
            "set_seed_limits",
            "cleanup_empty_dirs",
        ):
            assert getattr(config, job).enabled is False


def test_all_schema_fields_have_documentation_and_valid_examples():
    schema = config_json_schema()
    for definition in [schema, *schema["$defs"].values()]:
        assert definition["description"].strip()
        for field in definition["properties"].values():
            assert field["description"].strip()
            assert field["examples"]
            validator = Draft202012Validator({"$defs": schema["$defs"], **field})
            for example in field["examples"]:
                validator.validate(example)


@pytest.mark.parametrize("args", [[], ["--example"], ["--example", "full"]])
def test_schema_cli_output(validator, args):
    result = subprocess.run(
        [sys.executable, "-m", "tarr.schema", *args],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if args:
        assert result.stdout.splitlines()[0] == SCHEMA_DIRECTIVE
        raw = yaml.safe_load(result.stdout)
        validator.validate(raw)
        Config.model_validate(raw)
    else:
        assert json.loads(result.stdout) == config_json_schema()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "interval_seconds", 0),
        (None, "interval_seconds", 1.5),
        (None, "trackers", []),
        (None, "unknown", True),
        ("qbittorrent", "enabled", True),
        ("logging", "format", "text"),
        ("logging", "file_count", 0),
        ("remove_stopped", "on_delete", "Stop"),
        ("remove_unregistered", "delay_minutes", -1),
        ("cleanup_empty_dirs", "min_age_minutes", -1),
    ],
)
def test_schema_rejects_invalid_fields(validator, raw_config, section, field, value):
    target = raw_config if section is None else raw_config[section]
    target[field] = value

    with pytest.raises(ValidationError):
        validator.validate(raw_config)


@pytest.mark.parametrize("field", ["seed_time_minutes", "ratio"])
@pytest.mark.parametrize("value", [-2, -0.5])
def test_schema_rejects_invalid_seed_limits(validator, raw_config, field, value):
    raw_config["trackers"][0]["required"][field] = value

    with pytest.raises(ValidationError):
        validator.validate(raw_config)


@pytest.mark.parametrize("value", [-1, 0, 1])
def test_schema_accepts_seed_limits(validator, raw_config, value):
    raw_config["trackers"][0]["required"] = {"seed_time_minutes": value, "ratio": value}
    validator.validate(raw_config)


@pytest.mark.parametrize("job", ["maintain_free_space", "cleanup_empty_dirs"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("omit", [False, True])
def test_schema_checks_enabled_job_requirements(validator, raw_config, job, enabled, omit):
    field = "free_space_threshold_gibi" if job == "maintain_free_space" else "directories"
    raw_config[job] = {"enabled": enabled}
    if not omit:
        raw_config[job][field] = None if job == "maintain_free_space" else []

    if enabled:
        with pytest.raises(ValidationError):
            validator.validate(raw_config)
    else:
        validator.validate(raw_config)


def test_schema_requires_category_action(validator, raw_config):
    del raw_config["set_seed_limits"]["categories"][0]["action"]

    with pytest.raises(ValidationError):
        validator.validate(raw_config)


def test_schema_accepts_removal_category_forms(validator, raw_config):
    raw_config["arrs"] = {"radarr": {"host": "http://radarr:7878", "api_key": "secret"}}
    raw_config["remove_stopped"]["categories"] = [
        None,
        "upload",
        {"name": "radarr", "arr_verify_removal": ["radarr"]},
        {"name": None},
    ]
    validator.validate(raw_config)
