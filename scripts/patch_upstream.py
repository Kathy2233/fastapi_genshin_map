#!/usr/bin/env python3
"""Compatibility helper: suppress oversized API payload logs from the pinned submodule.

Map generation and Pillow fixes now live directly in GetMapImage/get_map_image.py.
"""
from __future__ import annotations

from pathlib import Path


def apply_patches(repo_root: Path) -> None:
    request_path = (
        repo_root
        / "fastapi_genshin_map"
        / "GetMapImage"
        / "GenshinMap"
        / "genshinmap"
        / "request.py"
    )
    if not request_path.is_file():
        raise FileNotFoundError(f"GenshinMap request module not found: {request_path}")

    source = request_path.read_text(encoding="utf-8")
    old = '            logger.info(f"[API] {data}")'
    new = '            logger.info(f"[API] response received: {endpoint} (retcode={data.get(\'retcode\')})")'

    if old in source:
        source = source.replace(old, new, 1)
        request_path.write_text(source, encoding="utf-8")
        print("Suppressed full API response payload logging.")
    elif new in source:
        print("API response payload logging is already suppressed.")
    else:
        # Some pinned revisions do not log full payloads. Keep going safely.
        print("No full API payload logger found; no request.py change needed.")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    apply_patches(args.repo_root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
