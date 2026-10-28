import argparse
import json
import os
from pathlib import Path

from .config import load_env
from .pipeline import answer, extract
from .server import serve
from .store import Store


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Evidence-first GraphRAG Lite")
    parser.add_argument("--db", default=os.getenv("GRAPHRAG_DB", "data/graphrag.sqlite"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve").add_argument("--port", type=int, default=8000)
    sub.add_parser("seed")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("file", help="JSON with title, text and optional facts")
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--mode", choices=["auto", "lexical", "graph", "hybrid"], default="auto")
    sub.add_parser("eval")
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.db, port=args.port)
        return
    if args.command == "eval":
        from .evaluation import run
        print(json.dumps(run(args.db), ensure_ascii=False, indent=2))
        return
    store = Store(args.db)
    try:
        if args.command == "seed":
            file = Path(__file__).parent.parent / "evals" / "corpus.json"
            records = json.loads(file.read_text())
            result = [store.ingest(item["title"], item["text"], item["facts"]) for item in records]
        elif args.command == "ingest":
            item = json.loads(Path(args.file).read_text())
            facts = item["facts"] if "facts" in item else extract(item["text"])
            result = store.ingest(item["title"], item["text"], facts)
        else:
            result = answer(store, args.question, args.mode)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
