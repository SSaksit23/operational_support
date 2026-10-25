"""Group database from the command line.

  export OPS_DB_DSN="host=localhost dbname=opsagent user=..."  OPS_AGENT_KEY=<Fernet key>
  python -m ops_agent.store.cli init
  python -m ops_agent.store.cli save FILE [FILE ...]        # read, check and save one group
  python -m ops_agent.store.cli settlement FILE.json        # import a rag-service settlement
  python -m ops_agent.store.cli search "query" [--group CODE] [--kind hotel]
"""

import argparse
import os
from pathlib import Path

from ..bundle import read_group
from ..verify import verify
from .crypto import IdVault
from .embed import HashingEmbedder, OpenAIEmbedder
from .repo import GroupStore


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["init", "save", "settlement", "search"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--group")
    ap.add_argument("--kind")
    ap.add_argument("-k", type=int, default=5)
    a = ap.parse_args()
    embedder = OpenAIEmbedder() if os.environ.get("OPS_EMBEDDER") == "openai" else HashingEmbedder()
    store = GroupStore(os.environ["OPS_DB_DSN"], embedder, IdVault())
    try:
        if a.command == "init":
            store.init_schema()
        elif a.command == "save":
            files = [(p, Path(p).name) for p in a.args]
            bundle = read_group(files)
            gid = store.save_group(bundle, verify(bundle), files)
            print(f"saved group {bundle.name_list.tour_code} (id {gid})")
        elif a.command == "settlement":
            for p in a.args:
                print("imported", p, "-> group id", store.import_settlement(p))
        else:
            for h in store.search(" ".join(a.args), k=a.k, tour_code=a.group, kind=a.kind):
                print(f"{h.score:.2f}  {h.tour_code}  {h.kind}  {h.file_name or ''} {h.location or ''}\n      {h.content[:200]}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
