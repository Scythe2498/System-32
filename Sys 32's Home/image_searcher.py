#!/usr/bin/env python3
"""
Image Attribute Searcher — written by System 32, reviewed by Claude
Search your generation library by checkpoint, lora, dimensions, seed, date.

Usage:
    python image_searcher.py search --checkpoint punk --width 1024
    python image_searcher.py search --older-than 7
    python image_searcher.py index --scan
"""

import argparse
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List


FILE_PATTERN = re.compile(r'image_(\d{8})_(\d{6})\.(png|jpg|jpeg|webp)')


def _load_config():
    for candidate in ["config.json", str(Path(__file__).parent / "config.json")]:
        try:
            with open(candidate, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _default_index_path(config):
    out = config.get("out_images", ".")
    return str(Path(out).parent / "image_index.json")


def _parse_timestamp(ts_str):
    """Safely parse ISO 8601 timestamp — never crashes."""
    if not ts_str:
        return datetime.min
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return datetime.min


def load_index(index_path):
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"No index found at {index_path}. Run: python image_searcher.py index --scan")
        return {}
    except json.JSONDecodeError:
        print(f"Index at {index_path} is corrupted.")
        return {}


def search(index, checkpoint=None, lora=None, width=None, height=None,
           min_seed=None, max_seed=None, older_than=None, newer_than=None,
           prompt_contains=None):
    results = []
    for entry in index.values():
        if checkpoint and checkpoint.lower() not in str(entry.get("checkpoint", "")).lower():
            continue
        if lora and lora.lower() not in str(entry.get("lora", "")).lower():
            continue
        if width and entry.get("width") != width:
            continue
        if height and entry.get("height") != height:
            continue
        seed = entry.get("seed") or 0
        if min_seed is not None and seed < min_seed:
            continue
        if max_seed is not None and seed > max_seed:
            continue
        if prompt_contains and prompt_contains.lower() not in str(entry.get("prompt", "")).lower():
            continue

        timestamp = _parse_timestamp(entry.get("timestamp", ""))
        if older_than is not None:
            if timestamp > datetime.now() - timedelta(days=older_than):
                continue
        if newer_than is not None:
            if timestamp < datetime.now() - timedelta(days=newer_than):
                continue

        results.append(entry)

    return sorted(results, key=lambda x: x.get("timestamp", ""), reverse=True)


def _parse_filename_timestamp(date_str, time_str):
    return (
        f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}T"
        f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:]}"
    )


def scan_folder(output_dir, index_path):
    output_dir = Path(output_dir)
    if not output_dir.exists():
        print(f"Error: output folder not found: {output_dir}")
        return 0
    existing = load_index(index_path)
    new_entries = {}
    for file_path in sorted(output_dir.glob("*")):
        if not file_path.is_file():
            continue
        match = FILE_PATTERN.search(file_path.name)
        if not match:
            continue
        date_str, time_str, _ = match.groups()
        key = file_path.stem
        if key in existing:
            continue
        new_entries[key] = {
            "path":       str(file_path),
            "filename":   file_path.name,
            "timestamp":  _parse_filename_timestamp(date_str, time_str),
            "checkpoint": "unknown",
            "lora":       "unknown",
            "prompt":     "unknown",
            "seed":       0,
            "steps":      0,
            "cfg":        0.0,
            "width":      0,
            "height":     0,
        }
    if new_entries:
        existing.update(new_entries)
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        print(f"Added {len(new_entries)} new images. Total: {len(existing)}")
    else:
        print(f"No new images. Index has {len(existing)} entries.")
    return len(new_entries)


def main():
    config = _load_config()
    default_output = config.get("out_images", "output/generated images")
    default_index  = _default_index_path(config)

    parser = argparse.ArgumentParser(description="System 32 Image Searcher — by System 32")
    sub = parser.add_subparsers(dest="command")

    # Index command
    idx = sub.add_parser("index", help="Build or update image index")
    idx.add_argument("--scan", action="store_true")
    idx.add_argument("--dir",  default=default_output)
    idx.add_argument("--index-path", default=default_index)

    # Search command
    srch = sub.add_parser("search", help="Search image library")
    srch.add_argument("--checkpoint",      help="Checkpoint name (partial)")
    srch.add_argument("--lora",            help="LoRA name (partial)")
    srch.add_argument("--width",           type=int)
    srch.add_argument("--height",          type=int)
    srch.add_argument("--min-seed",        type=int)
    srch.add_argument("--max-seed",        type=int)
    srch.add_argument("--older-than",      type=int, help="Days older than N")
    srch.add_argument("--newer-than",      type=int, help="Days newer than N")
    srch.add_argument("--prompt",          help="Keyword in prompt")
    srch.add_argument("--index-path",      default=default_index)
    srch.add_argument("--limit",           type=int, default=20)

    args = parser.parse_args()

    if args.command == "index":
        if args.scan:
            scan_folder(args.dir, args.index_path)
        else:
            idx.print_help()

    elif args.command == "search":
        index = load_index(args.index_path)
        results = search(
            index,
            checkpoint=args.checkpoint,
            lora=args.lora,
            width=args.width,
            height=args.height,
            min_seed=args.min_seed,
            max_seed=args.max_seed,
            older_than=args.older_than,
            newer_than=args.newer_than,
            prompt_contains=args.prompt,
        )
        print(f"\nFound {len(results)} image(s):")
        for r in results[:args.limit]:
            print(f"  • {r['filename']} | date={r.get('timestamp','')[:19]} | seed={r.get('seed')}")
        if len(results) > args.limit:
            print(f"  ... and {len(results) - args.limit} more (use --limit to show more)")
        if results:
            print(f"\nPaths for show_images:\n{[r['path'] for r in results[:args.limit]]}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
