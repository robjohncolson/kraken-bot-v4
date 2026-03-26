"""CLI entrypoint for research dataset export.

Usage:
    python -m research.cli --pair DOGE/USD --interval 60 --since 1700000000
    python research/cli.py --pair DOGE/USD --output-dir data/research
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from research.dataset_builder import DatasetBuilder, DatasetBuildError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export research dataset from OHLCV history."
    )
    parser.add_argument(
        "--pair", default="DOGE/USD", help="Trading pair (default: DOGE/USD)"
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Candle interval in minutes (default: 60)"
    )
    parser.add_argument(
        "--since", type=int, default=None,
        help="Start timestamp (Unix epoch seconds). Omit for all available."
    )
    parser.add_argument(
        "--until", type=int, default=None,
        help="End timestamp (Unix epoch seconds)."
    )
    parser.add_argument(
        "--output-dir", default="data/research",
        help="Output directory (default: data/research)"
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Path to SQLite database for trade data (optional)."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    db_reader = None
    if args.db_path:
        from persistence.sqlite import open_database
        from research.db_reader import ResearchReader
        conn = open_database(Path(args.db_path))
        db_reader = ResearchReader(conn)

    builder = DatasetBuilder(output_dir=args.output_dir)
    try:
        manifest = builder.build_dataset(
            pair=args.pair,
            interval=args.interval,
            since=args.since,
            until=args.until,
            db_reader=db_reader,
        )
    except DatasetBuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Dataset exported successfully:")
    print(f"  Pair: {manifest['pair']}")
    print(f"  Rows: {manifest['row_count']}")
    print(f"  Range: {manifest['timestamp_range']['start']} -> {manifest['timestamp_range']['end']}")
    print(f"  Output: {args.output_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
