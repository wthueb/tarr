import argparse
import json
from typing import Any

import yaml

from tarr.config import Config

SCHEMA_DIRECTIVE = "# yaml-language-server: $schema=./config.schema.json"


def config_json_schema() -> dict[str, Any]:
    """Generate the documented editor schema with minimal and full configuration examples."""
    schema = Config.model_json_schema()
    full_example = {name: field["examples"][0] for name, field in schema["properties"].items()}
    schema["examples"] = [
        {name: full_example[name] for name in schema["required"]},
        full_example,
    ]
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def config_yaml_example(*, full: bool = False) -> str:
    """Render a YAML starter from schema examples; the minimal starter disables all jobs."""
    example = config_json_schema()["examples"][int(full)]
    return f"{SCHEMA_DIRECTIVE}\n\n{yaml.safe_dump(example, sort_keys=False)}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate tarr's schema or a YAML starter config.")
    parser.add_argument(
        "--example",
        nargs="?",
        const="minimal",
        choices=["minimal", "full"],
        help="emit YAML instead of JSON Schema (default: minimal, with all jobs disabled)",
    )
    args = parser.parse_args()
    if args.example:
        print(config_yaml_example(full=args.example == "full"), end="")
    else:
        print(json.dumps(config_json_schema(), indent=2))


if __name__ == "__main__":
    main()
