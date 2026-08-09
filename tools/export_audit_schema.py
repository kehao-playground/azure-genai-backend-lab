import json
from pathlib import Path

from azgenai_lab.core.audit import AUDIT_EVENT_ADAPTER

OUTPUT_PATH = Path("docs/audit/audit-events.json")


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    schema = AUDIT_EVENT_ADAPTER.json_schema()
    OUTPUT_PATH.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Exported audit event schema to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
