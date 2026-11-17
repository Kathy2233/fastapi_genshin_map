# DIRECT_OPTIMIZED_MAP_GENERATOR
# 地图生成器：直接在本文件内完成性能优化与 Pillow 透明度兼容。

import asyncio
import random
from pathlib import Path
from typing import Optional, Union

import yaml
from fastapi import APIRouter
from fastapi.responses import FileResponse
from PIL import Image

from .GenshinMap.genshinmap import img, models, request, utils
from .logger import logger
from .download import download_file, make_P0_map, update_world

# 当前项目使用的是可信地图资源，底图像素数超过 Pillow 默认安全阈值。
Image.MAX_IMAGE_PIXELS = None

router = APIRouter(prefix='/get_map')
TEXT_PATH = Path(__file__).parent / 'texture2d'
MAP = Path(__file__).parent / 'map_data'
RESOURCE_PATH = Path(__file__).parent / 'resource_data'
ICON_PATH = Path(__file__).parent / 'icon_data'

_path = Path(__file__).parent / 'map.yaml'
with open(_path, 'r', encoding='utf-8') as ymlfile:
    resource_aliases = yaml.load(ymlfile, Loader=yaml.SafeLoader)

MAP_ID_DICT = {
    '2': models.MapID.teyvat,  # 提瓦特
    '9': models.MapID.chasm,  # 层岩巨渊
    '7': models.MapID.enkanomiya,  # 渊下宫
    '34': models.MapID.sea_of_bygone_eras,  # 旧日之海
    '36': models.MapID.holy_mountain,  # 远古圣山
    # MapID.golden_apple_archipelago,  # 金苹果群岛
    '40': models.MapID.frost_moon,  # 霜月
}

for directory in (MAP, RESOURCE_PATH, ICON_PATH):
    directory.mkdir(parents=True, exist_ok=True)

# 小型纹理缓存：每个进程只解码一次，调用方使用 copy() 获取可修改副本。
_TEMPLATE_CACHE: dict[str, Image.Image] = {}


def _load_rgba(path: Path) -> Image.Image:
    """以独立 RGBA 图像读取纹理，兼容 RGB/RGBA/P/LA/L 等 PNG 模式。"""
    with Image.open(path) as source:
        return source.convert('RGBA')


def _get_template(filename: str) -> Image.Image:
    if filename not in _TEMPLATE_CACHE:
        _TEMPLATE_CACHE[filename] = _load_rgba(TEXT_PATH / filename)
    return _TEMPLATE_CACHE[filename]


def paste_with_alpha(destination: Image.Image, source: Image.Image, position: tuple[int, int]) -> None:
    """兼容各种 Pillow 图像模式，统一使用 L 模式 Alpha 通道作为 mask。"""
    owns_rgba = source.mode != 'RGBA'
    rgba = source.convert('RGBA') if owns_rgba else source
    alpha = rgba.getchannel('A')
    try:
        destination.paste(rgba, position, alpha)
    finally:
        alpha.close()
        if owns_rgba:
            rgba.close()


def check_map_file() -> bool:
    for map_id in MAP_ID_DICT.values():
        map_path = MAP / f'{map_id.name}.png'
        if not map_path.is_file() or map_path.stat().st_size == 0:
            logger.info(f'地图文件 {map_path} 不存在或为空')
            return False
    return True


@router.on_event('startup')
async def create_genshin_map():
    if check_map_file():
        logger.info('****************** 开始地图API服务 *****************')
        return

    logger.info('****************** 地图API服务进行初始化 *****************')
    await update_world()
    await asyncio.sleep(2)

    mark_god_template = _get_template('mark_god.png')
    mark_trans_template = _get_template('mark_trans.png')

    for map_id in models.MapID:
        logger.info(f'[BUILD] {map_id.name}: fetching map metadata, points and labels')

        # 每张地图只请求一次地图信息、坐标点和标签树。
        maps = await request.get_maps(map_id)
        points = await request.get_points(map_id)
        trees = await request.get_labels(map_id)

        # 先按 label_id 分桶，再复用原版 utils.get_points_by_id 转换为 XYZSPoint 四元组。
        raw_points_by_label: dict[int, list] = {}
        for point in points:
            raw_points_by_label.setdefault(point.label_id, []).append(point)
        points_by_label_id: dict[int, list] = {
            label_id: utils.get_points_by_id(label_id, group)
            for label_id, group in raw_points_by_label.items()
        }
        del raw_points_by_label

        mark_god = utils.get_points_by_id(2, points)
        mark_trans = utils.get_points_by_id(3, points)
        mark_god_converted = utils.convert_pos(mark_god, maps.get_detail.origin)
        mark_trans_converted = utils.convert_pos(mark_trans, maps.get_detail.origin)

        # 生成底图。转换为 RGB 后保存并复用同一个对象，降低常驻内存约 25%。
        generated_map = await make_P0_map(maps.id, maps.get_detail)
        if generated_map.mode == 'RGB':
            # 已经是 RGB 时直接复用，避免再复制一份数 GiB 的底图。
            base_map = generated_map
            generated_map = None
        else:
            base_map = generated_map.convert('RGB')
            generated_map.close()
            generated_map = None

        try:
            for point in mark_god_converted:
                paste_with_alpha(
                    base_map,
                    mark_god_template,
                    (int(point.x) - 32, int(point.y) - 64),
                )
            for point in mark_trans_converted:
                paste_with_alpha(
                    base_map,
                    mark_trans_template,
                    (int(point.x) - 32, int(point.y) - 64),
                )
        except Exception:
            base_map.close()
            raise

        try:
            map_path = MAP / f'{map_id.name}.png'
            base_map.save(map_path, format='PNG', optimize=False)
            logger.info('****************** 开始绘制 *****************')

            total = sum(len(tree.children) for tree in trees)
            completed = 0
            logger.info(f'[BUILD] {map_id.name}: {total} resource categories; render concurrency=1')

            # 顺序生成，避免多个 768M 像素底图裁切任务同时占用内存。
            for tree in trees:
                for label in tree.children:
                    await get_map_response(
                        'PRE-START',
                        label.name,
                        map_id,
                        False,
                        maps_data=maps,
                        trees_data=trees,
                        points_by_label_id=points_by_label_id,
                        base_map_data=base_map,
                    )
                    completed += 1
                    if completed % 25 == 0 or completed == total:
                        logger.info(f'[BUILD] {map_id.name}: {completed}/{total} categories processed')

            logger.info(f'[BUILD] {map_id.name}: resource image generation finished ({completed}/{total})')
        finally:
            base_map.close()

        # 释放当前地图的大型 API 数据引用，再进入下一张地图。
        del points_by_label_id, points, trees, maps

    logger.info('****************** 开始地图API服务 *****************')


async def get_map_response(
    prefix: str,
    resource_name: str,
    map_id: models.MapID,
    is_cluster: bool = False,
    *,
    maps_data=None,
    trees_data=None,
    points_by_label_id=None,
    base_map_data: Optional[Image.Image] = None,
) -> Optional[Path]:
    # 资源原名用于匹配 API 标签；清洗后的名称仅用于本地文件名。
    lookup_name = resource_name
    safe_name = resource_name.replace('/', '_')

    if is_cluster:
        save_path = RESOURCE_PATH / f'{map_id.name}_{safe_name}_KMEANS.jpg'
    else:
        save_path = RESOURCE_PATH / f'{map_id.name}_{safe_name}.jpg'

    if save_path.is_file() and save_path.stat().st_size > 0:
        logger.info(f'{prefix} [查询成功]：发送缓存 [{save_path.name}]！')
        return save_path

    maps = maps_data if maps_data is not None else await request.get_maps(map_id)
    trees = trees_data if trees_data is not None else await request.get_labels(map_id)

    resource_id = 0
    icon_url = None
    for tree in trees:
        for label in tree.children:
            if lookup_name == label.name:
                resource_id = label.id
                icon_url = label.icon
                break
        if resource_id:
            break
        if lookup_name == tree.name:
            resource_id = tree.id
            icon_url = tree.icon
            break

    if resource_id == 0:
        return None

    if points_by_label_id is None:
        points = await request.get_points(map_id)
        transmittable = utils.get_points_by_id(resource_id, points)
    else:
        transmittable = points_by_label_id.get(resource_id, [])

    transmittable_converted = utils.convert_pos(transmittable, maps.get_detail.origin)
    if not transmittable_converted:
        return None

    if is_cluster:
        group_point = img.k_means_points(transmittable_converted)
        if not group_point:
            return None
    else:
        up = min(point.y for point in transmittable_converted)
        down = max(point.y for point in transmittable_converted)
        left = min(point.x for point in transmittable_converted)
        right = max(point.x for point in transmittable_converted)
        offset = 100
        group_point = [[
            models.XYPoint(left - offset, up - offset),
            models.XYPoint(right + offset, down + offset),
            transmittable_converted,
        ]]

    logger.info(f'{prefix} [新增缓存]：开始绘制 {save_path.name}...')

    lt_point = group_point[0][0]
    rb_point = group_point[0][1]
    width = rb_point.x - lt_point.x
    height = rb_point.y - lt_point.y
    if width < 500 or height < 500:
        lt_point = models.XYPoint(lt_point.x - 400, lt_point.y - 400)
        rb_point = models.XYPoint(rb_point.x + 400, rb_point.y + 400)

    crop_box = (int(lt_point.x), int(lt_point.y), int(rb_point.x), int(rb_point.y))
    if base_map_data is not None:
        genshin_map = base_map_data.crop(crop_box)
    else:
        map_path = MAP / f'{map_id.name}.png'
        with Image.open(map_path) as base_map:
            genshin_map = base_map.crop(crop_box).convert('RGB')

    icon_path = ICON_PATH / f'{safe_name}.png'
    if not icon_path.is_file() or icon_path.stat().st_size == 0:
        if not icon_url:
            genshin_map.close()
            return None
        await download_file(icon_url, icon_path)

    try:
        with Image.open(icon_path) as icon_source:
            icon_pic = icon_source.convert('RGBA').resize((52, 52))
    except Exception:
        icon_path.unlink(missing_ok=True)
        if not icon_url:
            genshin_map.close()
            raise
        await download_file(icon_url, icon_path)
        with Image.open(icon_path) as icon_source:
            icon_pic = icon_source.convert('RGBA').resize((52, 52))

    try:
        for point in group_point[0][2]:
            point_trans = (
                int(point.x) - int(lt_point.x),
                int(point.y) - int(lt_point.y),
            )

            if point.s == 1:  # type: ignore
                z = 1
            else:
                z = point.z  # type: ignore

            mark_filename = f'mark_{z}.png' if z <= 3 else 'mark_B.png'
            mark = _get_template(mark_filename).copy()
            try:
                if point.s == 1:  # type: ignore
                    paste_with_alpha(mark, _get_template('B.png'), (13, 50))
                elif point.s == 3:  # type: ignore
                    paste_with_alpha(mark, _get_template('W.png'), (13, 50))

                paste_with_alpha(mark, icon_pic, (25, 17))
                mark_resized = mark.resize((70, 70), Image.Resampling.LANCZOS)
                try:
                    paste_with_alpha(
                        genshin_map,
                        mark_resized,
                        (
                            point_trans[0] - 35,
                            point_trans[1] - 70,
                        ),
                    )
                finally:
                    mark_resized.close()
            finally:
                mark.close()

        genshin_map.save(save_path, 'JPEG', quality=95)
    finally:
        genshin_map.close()
        icon_pic.close()

    logger.info(f'{prefix} [查询成功]：新增缓存 [{save_path.name}]！')
    return save_path


@router.get('')
async def get_map_by_point(
    resource_name: str = '甜甜花',
    map_id: Union[str, int] = 0,
    is_cluster: bool = False,
):
    req_id = random.randint(10000, 99999)

    def resource_aliases_to_name(name: str) -> str:
        name = name.lower()
        for group in resource_aliases:
            for alias in resource_aliases[group]:
                if name == alias or name in resource_aliases[group][alias]:
                    return alias
        return name

    resource_name = resource_aliases_to_name(resource_name)
    prefix = f'>> [请求序列:{req_id}]'
    logger.info(
        f"{prefix} [查询请求]：在地图 ID {map_id or 'auto'} 内查询 {resource_name}..."
    )

    if map_id:
        map_key = str(map_id)
        if map_key not in MAP_ID_DICT:
            logger.warning(f'{prefix} [失败]：地图 ID - {map_id} 不存在！')
            return {'retcode': -1, 'message': f'地图 ID - {map_id} 不存在！'}
        maps = [MAP_ID_DICT[map_key]]
    else:
        maps = list(MAP_ID_DICT.values())

    for current_map in maps:
        result = await get_map_response(prefix, resource_name, current_map, is_cluster)
        if result:
            return FileResponse(result)
        if len(maps) > 1:
            logger.info(
                f'{prefix} [自动重试]：地图 ID {current_map._value_} 内不存在 {resource_name}...'
            )

    logger.warning(f'{prefix} [失败]：资源点 - {resource_name} 不存在！')
    return {'retcode': -1, 'message': f'资源点 - {resource_name} 不存在！'}
