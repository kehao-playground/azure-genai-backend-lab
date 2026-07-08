from pathlib import Path

import yaml

from azgenai_lab.main import app

OUTPUT_PATH = Path("docs/openapi/openapi.yaml")


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    schema = app.openapi()
    OUTPUT_PATH.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")
    print(f"Exported OpenAPI schema to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
