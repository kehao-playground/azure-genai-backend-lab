import json
from pathlib import Path

from azgenai_lab.models.search_index import to_index_definition

OUTPUT_PATH = Path("docs/search/index-schema.json")


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(to_index_definition(), indent=2) + "\n", encoding="utf-8"
    )
    print(f"Exported index schema to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
