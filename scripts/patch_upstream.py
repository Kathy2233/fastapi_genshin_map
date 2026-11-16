#!/usr/bin/env python3
"""Apply guarded performance/logging fixes to the pinned GenshinMap source."""
from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if new in text:
        return text  # already patched; safe to run more than once
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"Cannot apply patch {label}: expected exactly one source match, found {count}. "
            "The upstream submodule may have changed; refusing an unsafe edit."
        )
    return text.replace(old, new, 1)


def apply_patches(repo_root: Path) -> None:
    image_root = repo_root / "fastapi_genshin_map" / "GetMapImage"
    request_path = image_root / "GenshinMap" / "genshinmap" / "request.py"
    generator_path = image_root / "get_map_image.py"

    for path in (request_path, generator_path):
        if not path.is_file():
            raise FileNotFoundError(f"Expected upstream source file not found: {path}")

    request_source = request_path.read_text(encoding="utf-8")
    request_source = replace_once(
        request_source,
        '            logger.info(f"[API] {data}")',
        '            logger.info(f"[API] response received: {endpoint} (retcode={data.get(\'retcode\')})")',
        label="suppress full API response logging",
    )
    request_path.write_text(request_source, encoding="utf-8")

    source = generator_path.read_text(encoding="utf-8")

    # Reuse the already-fetched map metadata instead of requesting it twice.
    source = replace_once(
        source,
        '        maps = await request.get_maps(map_id)\n        # map_img = await utils.make_map(maps.detail)',
        '        # Reuse maps fetched above; avoid a duplicate API request.\n        # map_img = await utils.make_map(maps.detail)',
        label="reuse map metadata",
    )

    # Build one index of points per label for this map. The Point model objects
    # are shared; we do not duplicate the full point payload in memory.
    source = replace_once(
        source,
        '        trees = await request.get_labels(map_id)\n        \'\'\'\n',
        '        trees = await request.get_labels(map_id)\n'
        '        points_by_label_id = {}\n'
        '        for point in points:\n'
        '            points_by_label_id.setdefault(point.label_id, []).append(point)\n'
        '        total_resource_labels = sum(len(tree.children) for tree in trees)\n'
        '        processed_resource_labels = 0\n'
        '        logger.info(f"[BUILD] {map_id.name}: {total_resource_labels} resource categories")\n'
        "        '''\n",
        label="index points once per map",
    )

    source = replace_once(
        source,
        "                    get_map_response(\n"
        "                        'PRE-START',\n"
        "                        label.name,\n"
        "                        map_id,\n"
        "                        False,\n"
        "                    )",
        "                    get_map_response(\n"
        "                        'PRE-START',\n"
        "                        label.name,\n"
        "                        map_id,\n"
        "                        False,\n"
        "                        maps_data=maps,\n"
        "                        trees_data=trees,\n"
        "                        points_by_label_id=points_by_label_id,\n"
        "                        base_map_data=map_img,\n"
        "                    )",
        label="pass cached map data to resource renderer",
    )

    source = replace_once(
        source,
        "            if len(tasks) > 10:\n"
        "                await asyncio.gather(*tasks)\n"
        "                tasks = []\n"
        "        await asyncio.gather(*tasks)",
        "            processed_resource_labels += 1\n"
        "            if len(tasks) >= 1:\n"
        "                await asyncio.gather(*tasks)\n"
        "                tasks = []\n"
        "                logger.info(f\"[BUILD] {map_id.name}: {processed_resource_labels}/{total_resource_labels} categories processed\")\n"
        "        if tasks:\n"
        "            await asyncio.gather(*tasks)\n"
        "        logger.info(f\"[BUILD] {map_id.name}: resource image generation finished ({processed_resource_labels}/{total_resource_labels})\")\n"
        "        map_img.close()\n"
        "        del map_img",
        label="limit concurrent image renders and report progress",
    )

    source = replace_once(
        source,
        "    is_cluster: bool = False,\n"
        ") -> Optional[Path]:",
        "    is_cluster: bool = False,\n"
        "    *,\n"
        "    maps_data=None,\n"
        "    trees_data=None,\n"
        "    points_by_label_id=None,\n"
        "    base_map_data=None,\n"
        ") -> Optional[Path]:",
        label="add optional cached data parameters",
    )

    source = replace_once(
        source,
        "    maps = await request.get_maps(map_id)\n"
        "    trees = await request.get_labels(map_id)",
        "    maps = maps_data if maps_data is not None else await request.get_maps(map_id)\n"
        "    trees = trees_data if trees_data is not None else await request.get_labels(map_id)",
        label="reuse cached map and label requests",
    )

    source = replace_once(
        source,
        "    # 请求坐标点\n"
        "    points = await request.get_points(map_id)\n"
        "    transmittable = utils.get_points_by_id(resource_id, points)",
        "    # Reuse the per-map point index during pre-generation. API requests\n"
        "    # still fetch points normally when this helper is used by the HTTP API.\n"
        "    if points_by_label_id is None:\n"
        "        points = await request.get_points(map_id)\n"
        "        transmittable = utils.get_points_by_id(resource_id, points)\n"
        "    else:\n"
        "        transmittable = utils.get_points_by_id(\n"
        "            resource_id, points_by_label_id.get(resource_id, [])\n"
        "        )",
        label="reuse point index instead of refetching full point list",
    )

    # PIL images were previously opened for every single resource point. Cache
    # the small overlay templates and open the icon only once per resource.
    source = replace_once(
        source,
        "    # 在地图上绘制资源点\n"
        "    for point in group_point[0][2]:\n",
        "    # Load the resource icon once, not once per point.\n"
        "    icon_path = ICON_PATH / f'{resource_name}.png'\n"
        "    if not icon_path.exists():\n"
        "        await download_file(icon, icon_path)\n"
        "    try:\n"
        "        with Image.open(icon_path) as icon_source:\n"
        "            icon_pic = icon_source.convert('RGBA').resize((52, 52))\n"
        "    except Exception:\n"
        "        icon_path.unlink(missing_ok=True)\n"
        "        await download_file(icon, icon_path)\n"
        "        with Image.open(icon_path) as icon_source:\n"
        "            icon_pic = icon_source.convert('RGBA').resize((52, 52))\n"
        "\n"
        "    mark_cache = {}\n"
        "    overlay_cache = {}\n"
        "\n"
        "    def get_mark_template(level):\n"
        "        cache_key = level if level <= 3 else 'B'\n"
        "        if cache_key not in mark_cache:\n"
        "            filename = f'mark_{level}.png' if level <= 3 else 'mark_B.png'\n"
        "            with Image.open(TEXT_PATH / filename) as mark_source:\n"
        "                mark_cache[cache_key] = mark_source.convert('RGBA')\n"
        "        return mark_cache[cache_key].copy()\n"
        "\n"
        "    def get_overlay(name):\n"
        "        if name not in overlay_cache:\n"
        "            with Image.open(TEXT_PATH / f'{name}.png') as overlay_source:\n"
        "                overlay_cache[name] = overlay_source.convert('RGBA')\n"
        "        return overlay_cache[name]\n"
        "\n"
        "    # 在地图上绘制资源点\n"
        "    for point in group_point[0][2]:\n",
        label="cache icon and overlay templates",
    )

    source = replace_once(
        source,
        "        icon_path = ICON_PATH / f'{resource_name}.png'\n"
        "        while True:\n"
        "            try:\n"
        "                if not icon_path.exists():\n"
        "                    await download_file(icon, icon_path)\n"
        "                icon_pic = Image.open(icon_path).resize((52, 52))\n"
        "            except:  # noqa: E722\n"
        "                await download_file(icon, icon_path)\n"
        "                continue\n"
        "            break\n\n",
        "",
        label="remove per-point icon file reads",
    )

    source = replace_once(
        source,
        "        if z <= 3:\n"
        "            mark = Image.open(TEXT_PATH / f'mark_{z}.png')\n"
        "        else:\n"
        "            mark = Image.open(TEXT_PATH / 'mark_B.png')\n\n"
        "        _m = None\n"
        "        if point.s == 1:  # type: ignore\n"
        "            _m = Image.open(TEXT_PATH / 'B.png')\n"
        "        elif point.s == 3:  # type: ignore\n"
        "            _m = Image.open(TEXT_PATH / 'W.png')\n"
        "        if _m is not None:\n"
        "            mark.paste(_m, (13, 50), _m)\n\n"
        "        icon_pic = icon_pic.convert('RGBA')\n",
        "        mark = get_mark_template(z)\n"
        "        if point.s == 1:  # type: ignore\n"
        "            overlay = get_overlay('B')\n"
        "            mark.paste(overlay, (13, 50), overlay)\n"
        "        elif point.s == 3:  # type: ignore\n"
        "            overlay = get_overlay('W')\n"
        "            mark.paste(overlay, (13, 50), overlay)\n\n",
        label="reuse mark and status overlays",
    )

    source = replace_once(
        source,
        "    genshin_map.save(save_path, 'JPEG', quality=95)\n"
        "    logger.info(f'{prefix} [查询成功]：新增缓存 [{save_path.name}]！')\n"
        "    return save_path",
        "    genshin_map.save(save_path, 'JPEG', quality=95)\n"
        "    genshin_map.close()\n"
        "    icon_pic.close()\n"
        "    for cached_image in mark_cache.values():\n"
        "        cached_image.close()\n"
        "    for cached_image in overlay_cache.values():\n"
        "        cached_image.close()\n"
        "    logger.info(f'{prefix} [查询成功]：新增缓存 [{save_path.name}]！')\n"
        "    return save_path",
        label="close generated PIL images after saving",
    )

    # Replace the full-map source open with a context manager so its file handle
    # and source image are released immediately after the crop is materialized.
    source = replace_once(
        source,
        "    # 打开主地图\n"
        "    genshin_map = Image.open(map_path)\n\n"
        "    # 计算裁切点",
        "    # 计算裁切点",
        label="remove unscoped source map open",
    )
    source = replace_once(
        source,
        "    genshin_map = genshin_map.crop(\n"
        "        (int(lt_point.x), int(lt_point.y), int(rb_point.x), int(rb_point.y))\n"
        "    )",
        "    crop_box = (int(lt_point.x), int(lt_point.y), int(rb_point.x), int(rb_point.y))\n"
        "    if base_map_data is not None:\n"
        "        genshin_map = base_map_data.crop(crop_box)\n"
        "    else:\n"
        "        with Image.open(map_path) as base_map:\n"
        "            genshin_map = base_map.crop(crop_box)",
        label="release full-map source after cropping",
    )

    generator_path.write_text(source, encoding="utf-8")
    print("Applied GenshinMap build fixes:")
    print("  - removed full API JSON/point-list logging")
    print("  - fetched map metadata, labels, and points once per map")
    print("  - grouped points by label ID for fast reuse")
    print("  - limited concurrent resource-image renders to 1")
    print("  - reused the in-memory full-resolution base map for all resource crops")
    print("  - closed the full-resolution base map after each map is processed")
    print("  - cached icons/mark overlays and closed PIL images")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    apply_patches(args.repo_root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
