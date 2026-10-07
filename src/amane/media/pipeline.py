"""刮削期按 ``download_resources`` 下载 / 裁剪 / 就地超分, 返回应写入 metadata 的 URL.

与整理到媒体库路径无关. 任一步失败不抛, 仅降级.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..config import DownloadableResource
from ..net.errors import RequestError
from ..sr import get_preset_meta, run_SR
from .images import (
    crop_box,
    crop_poster,
    format_crop_box_args,
    needs_upscale,
    probe_size,
    probe_size_from_bytes,
    should_crop_poster,
    validate_crop_box,
)
from .resource_store import RESOURCE_URL_PREFIX, download_headers

if TYPE_CHECKING:
    from ..config import HotSettings, SrConfig
    from ..db.models import Resource
    from ..net.http import WebClient
    from .resource_store import ResourceStore

logger = structlog.get_logger()


@dataclass
class MaterializedImages:
    # fanart 不是 metadata 字段 (整理时按 JAV 约定取 thumb), 不在此返回.

    poster_urls: list[str]
    thumb_urls: list[str]
    trailer_urls: list[str]


def _internal_url(store: ResourceStore, locator_url: str) -> str:
    return f"{RESOURCE_URL_PREFIX}/{store.url_hash(locator_url)}"


def _success_first(urls: list[str], succeeded: set[str]) -> list[str]:
    # 成功者保序前置, 失败者保序置于尾部. succeeded 为空时幂等返回原序.
    return [u for u in urls if u in succeeded] + [u for u in urls if u not in succeeded]


def _resolution_first(urls: list[str], sizes: dict[str, tuple[int, int] | None]) -> list[str]:
    """成功候选按像素面积降序排列; 相同或无法读取时保留来源顺序."""
    order = {url: index for index, url in enumerate(urls)}

    def key(url: str) -> tuple[int, int, int]:
        size = sizes.get(url)
        area = size[0] * size[1] if size else -1
        return (0 if size else 1, -area, order[url])

    known = [url for url in urls if url in sizes]
    unknown = [url for url in urls if url not in sizes]
    return sorted(known, key=key) + unknown


def _image_area(size: tuple[int, int] | None) -> int:
    return size[0] * size[1] if size else -1


_SIZE_PROBE_BYTES = 64 * 1024


async def _probe_size(client: WebClient, url: str) -> tuple[int, int] | None:
    try:
        data = await client.get_prefix(url, size=_SIZE_PROBE_BYTES, headers=download_headers(url))
    except RequestError:
        return None
    return probe_size_from_bytes(data)


async def _probe_sizes(client: WebClient, urls: list[str]) -> dict[str, tuple[int, int] | None]:
    """未下载候选取头部字节测尺寸; 不可达或非图像记 ``None``."""
    return {url: await _probe_size(client, url) for url in urls}


def sr_args_dict(cfg: SrConfig) -> dict:
    pm = get_preset_meta(cfg.preset)
    return {"preset": cfg.preset, "tool": pm.tool, "model": pm.model, "scale": pm.scale}


async def _maybe_upscale(
    store: ResourceStore,
    resource: Resource,
    config: HotSettings,
    data_dir: Path,
) -> None:
    if not config.sr.enabled:
        return
    full = store.full_path(resource)
    size = probe_size(full)
    file_bytes = full.stat().st_size if full.exists() else 0
    if not needs_upscale(
        size,
        file_bytes,
        max_dim_threshold=config.sr.max_dim_threshold,
        max_bytes_threshold=config.sr.max_bytes_threshold,
    ):
        return

    async def producer(src: Path, out: Path) -> bool:
        result = await run_SR(src, out, config.sr, data_dir)
        return result.success

    await store.upscale_in_place(resource, sr_args_dict(config.sr), producer)


async def materialize_images(
    poster_urls: list[str],
    thumb_urls: list[str],
    trailer_urls: list[str],
    store: ResourceStore,
    client: WebClient,
    config: HotSettings,
    data_dir: Path,
    *,
    extrafanart_urls: dict[str, list[str]] | None = None,
) -> MaterializedImages:
    """仅下载集合内的类型; 未选中的保留聚合 URL.

    未下载的类型只探头部尺寸, 同样按分辨率排序.
    死 URL 保序置于尾部, 供来源恢复后重试.
    extrafanart 仅下载到 Resource, 站点分组结构不重排.
    """
    kinds = set(config.scraping.download_resources)

    # 下载 thumb.
    thumb_ok: set[str] = set()
    thumb_sizes: dict[str, tuple[int, int] | None] = {}
    thumb_local: Path | None = None
    thumb_src: str | None = None
    if DownloadableResource.thumb in kinds:
        for url in thumb_urls:
            local = await store.acquire(url, client)
            if local:
                thumb_ok.add(url)
                thumb_sizes[url] = probe_size(local)
                if thumb_local is None or _image_area(thumb_sizes[url]) > _image_area(thumb_sizes.get(thumb_src or "")):
                    thumb_local, thumb_src = local, url
                    res = await store.get_by_url(url)
                    if res:
                        await _maybe_upscale(store, res, config, data_dir)
    else:
        thumb_sizes = await _probe_sizes(client, thumb_urls)

    # 下载 poster; 候选偏矮则从 thumb 裁剪.
    poster_ok: set[str] = set()
    poster_sizes: dict[str, tuple[int, int] | None] = {}
    poster_candidate_local: Path | None = None
    poster_candidate_url: str | None = None
    result_poster_urls = list(poster_urls)
    if DownloadableResource.poster in kinds:
        for url in poster_urls:
            local = await store.acquire(url, client)
            if local:
                poster_ok.add(url)
                poster_sizes[url] = probe_size(local)
                if poster_candidate_local is None or _image_area(poster_sizes[url]) > _image_area(
                    poster_sizes.get(poster_candidate_url or "")
                ):
                    poster_candidate_local = local
                    poster_candidate_url = url

        # 裁剪需要 thumb 本地文件; 若未选 thumb 下载, 为裁剪临时 acquire 分辨率最高的 thumb.
        if thumb_local is None and thumb_urls and config.scraping.crop_poster:
            for url in _resolution_first(thumb_urls, thumb_sizes):
                local = await store.acquire(url, client)
                if local:
                    thumb_local, thumb_src = local, url
                    break

        thumb_size = probe_size(thumb_local) if thumb_local else None
        cand_size = probe_size(poster_candidate_local) if poster_candidate_local else None

        if (
            config.scraping.crop_poster
            and should_crop_poster(thumb_size, cand_size, skip_ratio=config.scraping.poster_crop_skip_ratio)
            and thumb_local
        ):
            ratio = config.scraping.poster_ratio
            args = f"{ratio:.4f}"
            local_thumb = thumb_local

            async def crop_producer(dest: Path) -> bool:
                return crop_poster(local_thumb, dest, poster_ratio=ratio, jpeg_quality=config.scraping.jpeg_quality)

            crop_res = await store.acquire_derived(thumb_src or "", "crop", args, crop_producer)
            if crop_res:
                await _maybe_upscale(store, crop_res, config, data_dir)
                result_poster_urls = [_internal_url(store, crop_res.url)]
            else:
                result_poster_urls = _resolution_first(poster_urls, poster_sizes)
        else:
            result_poster_urls = _resolution_first(poster_urls, poster_sizes)
            if poster_candidate_local:
                for url in result_poster_urls:
                    res = await store.get_by_url(url)
                    if res:
                        await _maybe_upscale(store, res, config, data_dir)
                        break
    else:
        # 未下载 poster: 只探尺寸, 让列表首位仍是分辨率最高的候选.
        poster_sizes = await _probe_sizes(client, poster_urls)
        result_poster_urls = _resolution_first(poster_urls, poster_sizes)

    # 下载预告片.
    trailer_ok: set[str] = set()
    if DownloadableResource.trailer in kinds:
        for url in trailer_urls:
            local = await store.acquire(url, client)
            if local:
                trailer_ok.add(url)

    # 下载剧照: 按站点优先级, 有结果即停.
    if DownloadableResource.extrafanart in kinds and extrafanart_urls:
        priority = list(extrafanart_urls.keys())
        await store.acquire_extrafanart(extrafanart_urls, priority, client)

    out = MaterializedImages(
        poster_urls=result_poster_urls,
        thumb_urls=_resolution_first(thumb_urls, thumb_sizes) if thumb_sizes else _success_first(thumb_urls, thumb_ok),
        trailer_urls=_success_first(trailer_urls, trailer_ok),
    )
    logger.debug(
        "images materialized",
        kinds=sorted(kinds),
        poster=out.poster_urls[:1],
        thumb=out.thumb_urls[:1],
        trailer=out.trailer_urls[:1],
    )
    return out


async def manual_crop_image(
    source_url: str,
    box: tuple[int, int, int, int],
    store: ResourceStore,
    client: WebClient,
    config: HotSettings,
    data_dir: Path,
    *,
    subject: str,
) -> str:
    """手动裁切源图, 返回内部 URL. 源可为外部 http(s) 或 ``/api/resources/{hash}``.

    失败抛 ``ValueError`` (消息可直接作 API detail); ``subject`` 组装提示文案 (如「封面图」「头像」).
    """
    source = await store.resolve_source(source_url, client)
    if source is None:
        raise ValueError(f"无法获取{subject}")

    size = probe_size(source.path)
    if size is None:
        raise ValueError(f"{subject}无法读取")
    if not validate_crop_box(box, size):
        raise ValueError("裁切区域无效")

    args = format_crop_box_args(*box)
    jpeg_quality = config.scraping.jpeg_quality

    async def producer(dest: Path) -> bool:
        return crop_box(source.path, dest, box, jpeg_quality=jpeg_quality)

    crop_res = await store.acquire_derived(source.locator, "crop", args, producer)
    if crop_res is None:
        raise ValueError("裁切失败")

    await _maybe_upscale(store, crop_res, config, data_dir)
    return _internal_url(store, crop_res.url)
