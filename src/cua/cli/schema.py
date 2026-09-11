"""cua-schema: write the JSON Schema of the capability artifact format."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cua.artifact.store import write_json_schema


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Export the capability artifact JSON Schema")
    p.add_argument("--out", type=Path, default=Path("artifacts/schema/capability-artifact.schema.json"))
    args = p.parse_args(argv)
    print(write_json_schema(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
