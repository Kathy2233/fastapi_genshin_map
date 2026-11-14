#!/usr/bin/env python3
"""Build the repository's map images without starting the FastAPI server."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = REPO_ROOT / "fastapi_genshin_map"
IMAGE_ROOT = PROJECT_DIR / "GetMapImage"
GENERATED_DIRS = ("map_data", "resource_data", "icon_data", "slice_data")


def count_files(path: Path) -> tuple[int, int]:
    """Return (file count, total bytes) recursively."""
    count = 0
    total_bytes = 0
    for item in path.rglob("*"):
        if item.is_file():
            count += 1
            try:
                total_bytes += item.stat().st_size
            except OSError:
                pass
    return count, total_bytes


def main() -> int:
    if not PROJECT_DIR.is_dir():
        raise RuntimeError(f"Project directory not found: {PROJECT_DIR}")
    submodule_dir = IMAGE_ROOT / "GenshinMap"
    if not submodule_dir.is_dir() or not any(submodule_dir.iterdir()):
        raise RuntimeError(
            "GenshinMap submodule is missing or empty. "
            "Check that checkout uses submodules: recursive."
        )

    # This workflow uses a fresh runner. Remove any generated files included in
    # the fork so create_genshin_map() cannot hit its 'all map PNGs exist' early exit.
    for name in GENERATED_DIRS:
        target = IMAGE_ROOT / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    os.chdir(PROJECT_DIR)
    sys.path.insert(0, str(PROJECT_DIR))

    # Import only after deleting slice_data: download.py creates that directory
    # at import time and keeps its Path object for the duration of the build.
    from GetMapImage import get_map_image  # noqa: E402

    print("Starting create_genshin_map() directly; Uvicorn/API will not be started.")
    started = time.monotonic()
    asyncio.run(get_map_image.create_genshin_map())
    elapsed = time.monotonic() - started

    # Validate every map configured by this repository before any deployment.
    expected_map_files = [
        IMAGE_ROOT / "map_data" / f"{map_id.name}.png"
        for map_id in get_map_image.MAP_ID_DICT.values()
    ]
    missing = [str(path.relative_to(REPO_ROOT)) for path in expected_map_files if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError("Map generation incomplete; missing/empty maps:\n  " + "\n  ".join(missing))

    required_dirs = ("map_data", "resource_data", "icon_data")
    stats: dict[str, tuple[int, int]] = {}
    for name in required_dirs:
        directory = IMAGE_ROOT / name
        if not directory.is_dir():
            raise RuntimeError(f"Required output directory is missing: {directory}")
        stats[name] = count_files(directory)
        if stats[name][0] == 0:
            raise RuntimeError(f"Required output directory is empty: {directory}")

    print(f"Generation completed in {elapsed / 60:.1f} minutes.")
    for name, (file_count, total_bytes) in stats.items():
        print(f"{name}: {file_count} files, {total_bytes / (1024 ** 3):.3f} GiB")

    total_bytes = sum(size for _, size in stats.values())
    print(f"Total deployment payload: {total_bytes / (1024 ** 3):.3f} GiB")
    disk = shutil.disk_usage(REPO_ROOT)
    print(f"Runner disk free after build: {disk.free / (1024 ** 3):.2f} GiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
