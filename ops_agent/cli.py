"""python -m ops_agent.cli [--json] FILE [FILE ...]

Reads the files, runs the cross-checks and prints the review table.
--json prints the extracted group bundle instead.
"""

import sys
from pathlib import Path

from .bundle import read_group
from .report import to_markdown
from .verify import verify


def main(argv: list[str]) -> None:
    as_json = "--json" in argv
    paths = [a for a in argv if a != "--json"]
    bundle = read_group([(p, Path(p).name) for p in paths])
    if as_json:
        print(bundle.model_dump_json(indent=2, exclude_none=True))
        return
    code = bundle.name_list.tour_code if bundle.name_list else "group"
    print(to_markdown(verify(bundle), f"Document checks: {code}"))


if __name__ == "__main__":
    main(sys.argv[1:])
