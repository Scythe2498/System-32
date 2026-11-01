#!/usr/bin/env python3
"""
Image Index Builder — written by System 32
Scans the generated images output folder and builds image_index.json
so it can search its library by attributes.

Usage:
    python image_indexer.py --scan
    python image_indexer.py --scan --dir "<drive>:\\System 32's Output\\generated images"
"""

import argparse
import json
import re
from pathlib import Path
from datetime import datetime


INDEX_PATH = None  # Set at runtime from config
OUTPUT_DIR = None  # Set at runtime from config

FILE_PATTERN = re.compile(r'image_(\d{8})_(\d{6})\.(png|jpg|jpeg|webp)')


def _parse_filename_timestamp(date_str, time_str):
    """Convert YYYYMMDD + HHMMSS to ISO 8601 string."""
    return (
        f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}T"
        f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:]}"
    )


def load_index(index_path):
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        print(f"Warning: {index_path} was corrupted — starting fresh.")
        return {}


def scan_folder(output_dir, index_path):
    """Scan output folder and add new images to the index."""
    output_dir = Path(output_dir)
    if not output_dir.exists():
        print(f"Error: output folder not found: {output_dir}")
        return 0

    existing = load_index(index_path)
    new_entries = {}
    skipped = 0

    for file_path in sorted(output_dir.glob("*")):
        if not file_path.is_file():
            continue
        match = FILE_PATTERN.search(file_path.name)
        if not match:
            continue

        date_str, time_str, _ = match.groups()
        iso_timestamp = _parse_filename_timestamp(date_str, time_str)
        key = file_path.stem  # e.g. image_20260521_042443

        if key in existing:
            skipped += 1
            continue

        new_entries[key] = {
            "path":       str(file_path),
            "filename":   file_path.name,
            "timestamp":  iso_timestamp,
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
        print(f"[indexer] Added {len(new_entries)} new images. Skipped {skipped} already indexed.")
    else:
        print(f"[indexer] No new images found. Index has {len(existing)} entries.")

    return len(new_entries)


def main():
    # Try to read config.json for paths
    config = {}
    for config_candidate in [
        "config.json",
        str(Path(__file__).parent / "config.json"),
    ]:
        try:
            with open(config_candidate, encoding="utf-8") as f:
                config = json.load(f)
            break
        except Exception:
            pass

    default_output = config.get("out_images", "output/generated images")
    default_index  = str(Path(config.get("out_images", ".")).parent / "image_index.json")

    parser = argparse.ArgumentParser(description="System 32 Image Index Builder")
    parser.add_argument("--scan",  action="store_true", help="Scan output folder and update index")
    parser.add_argument("--dir",   default=default_output, help="Output folder to scan")
    parser.add_argument("--index", default=default_index,  help="Path to image_index.json")
    args = parser.parse_args()

    if args.scan:
        scan_folder(args.dir, args.index)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
