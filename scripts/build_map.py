#!/usr/bin/env python3
"""Build map images with guarded upstream optimizations; do not start FastAPI."""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from patch_upstream import apply_patches

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = REPO_ROOT / "fastapi_genshin_map"
IMAGE_ROOT = PROJECT_DIR / "GetMapImage"
GENERATED_DIRS = ("map_data", "resource_data", "icon_data", "slice_data")


def count_files(path: Path) -> tuple[int, int]:
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


def _read_proc_memory() -> tuple[str, str]:
    rss = peak = "unknown"
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                rss = line.split(":", 1)[1].strip()
            elif line.startswith("VmHWM:"):
                peak = line.split(":", 1)[1].strip()
    except OSError:
        pass
    return rss, peak


def _start_resource_monitor(stop: threading.Event) -> threading.Thread:
    def monitor() -> None:
        while not stop.wait(60):
            rss, peak = _read_proc_memory()
            disk = shutil.disk_usage(REPO_ROOT)
            print(
                f"[RESOURCE] RSS={rss}; peak_RSS={peak}; "
                f"workspace_free={disk.free / (1024 ** 3):.2f} GiB",
                flush=True,
            )

    thread = threading.Thread(target=monitor, name="resource-monitor", daemon=True)
    thread.start()
    return thread


def main() -> int:
    if not PROJECT_DIR.is_dir():
        raise RuntimeError(f"Project directory not found: {PROJECT_DIR}")

    submodule_dir = IMAGE_ROOT / "GenshinMap"
    if not submodule_dir.is_dir() or not any(submodule_dir.iterdir()):
        raise RuntimeError(
            "GenshinMap submodule is missing or empty. Check checkout submodules: recursive."
        )

    print("Applying guarded upstream performance fixes...", flush=True)
    apply_patches(REPO_ROOT)

    # The hosted runner is fresh for each workflow run. Delete only generated
    # directories so create_genshin_map() cannot early-exit on stale map PNGs.
    for name in GENERATED_DIRS:
        target = IMAGE_ROOT / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    os.chdir(PROJECT_DIR)
    sys.path.insert(0, str(PROJECT_DIR))

    from GetMapImage import get_map_image  # noqa: E402

    print("Starting create_genshin_map() directly; FastAPI/Uvicorn will not start.", flush=True)
    started = time.monotonic()
    stop_monitor = threading.Event()
    monitor_thread = _start_resource_monitor(stop_monitor)
    try:
        asyncio.run(get_map_image.create_genshin_map())
    finally:
        stop_monitor.set()
        monitor_thread.join(timeout=2)
    elapsed = time.monotonic() - started

    expected_map_files = [
        IMAGE_ROOT / "map_data" / f"{map_id.name}.png"
        for map_id in get_map_image.MAP_ID_DICT.values()
    ]
    missing = [
        str(path.relative_to(REPO_ROOT))
        for path in expected_map_files
        if not path.is_file() or path.stat().st_size == 0
    ]
    if missing:
        raise RuntimeError("Map generation incomplete; missing/empty maps:\n  " + "\n  ".join(missing))

    stats: dict[str, tuple[int, int]] = {}
    for name in ("map_data", "resource_data", "icon_data"):
        directory = IMAGE_ROOT / name
        if not directory.is_dir():
            raise RuntimeError(f"Required output directory is missing: {directory}")
        stats[name] = count_files(directory)
        if stats[name][0] == 0:
            raise RuntimeError(f"Required output directory is empty: {directory}")

    print(f"Generation completed in {elapsed / 60:.1f} minutes.", flush=True)
    for name, (file_count, total_bytes) in stats.items():
        print(f"{name}: {file_count} files, {total_bytes / (1024 ** 3):.3f} GiB", flush=True)
    total_bytes = sum(size for _, size in stats.values())
    print(f"Total deployment payload: {total_bytes / (1024 ** 3):.3f} GiB", flush=True)
    disk = shutil.disk_usage(REPO_ROOT)
    rss, peak = _read_proc_memory()
    print(f"Runner RSS at finish: {rss}; peak RSS: {peak}", flush=True)
    print(f"Runner disk free after build: {disk.free / (1024 ** 3):.2f} GiB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
