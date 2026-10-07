"""测试刮削期资源管线 materialize_images (选源/裁剪/就地超分)."""

from io import BytesIO
from typing import TYPE_CHECKING, cast

import pytest
from PIL import Image

from amane.config import HotSettings, ScrapingConfig
from amane.enums import DownloadableResource
from amane.media import ResourceStore, derived_locator, materialize_images
from amane.media import pipeline as pipeline_mod
from amane.net.errors import RequestError

if TYPE_CHECKING:
    from pathlib import Path

    from amane.net.http import WebClient


class FakeClient:
    """伪 WebClient: download 按 url 写出预设尺寸的图片 (或视频占位); fail 集合内 URL 下载失败.

    get_prefix 供未下载候选探尺寸 (不落盘); sizes 未收录或 fail 内的 URL 视为取不到.
    """

    def __init__(self, sizes: dict[str, tuple[int, int]], fail: set[str] | None = None):
        self._sizes = sizes
        self.fail = fail or set()
        self.downloaded: list[str] = []
        self.probed: list[str] = []

    async def download(self, url: str, dest: Path, **kwargs) -> bool:
        self.downloaded.append(url)
        if url in self.fail:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = self._sizes.get(url)
        if size is None:
            dest.write_bytes(b"video-bytes")  # trailer 等非图
            return True
        Image.new("RGB", size, "blue").save(dest)
        return True

    async def get_prefix(self, url: str, *, size: int, **kwargs) -> bytes:
        self.probed.append(url)
        dims = self._sizes.get(url)
        if url in self.fail or dims is None:
            raise RequestError(url, "probe failed")
        buffer = BytesIO()
        Image.new("RGB", dims, "blue").save(buffer, format="PNG")
        return buffer.getvalue()

    async def get_filesize(self, url: str, **kwargs) -> int | None:
        return None

    async def resolve_final_url(self, url: str, **kwargs) -> str:
        return url


@pytest.mark.asyncio
async def test_poster_cropped_when_candidate_small(resource_store: ResourceStore, tmp_path: Path):
    """poster 候选偏矮 → 从 thumb 按配置 poster_ratio 靠右裁, 不用候选长宽比."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (300, 420),  # b/h=0.78 < 0.9 → 裁; 候选比 ≈0.714 ≠ 配置 0.7
        }
    )
    cfg = HotSettings()  # sr.enabled 默认 False; poster_ratio 默认 0.7
    out = await materialize_images(
        ["https://s/p.jpg"], ["https://s/t.jpg"], [], resource_store, cast("WebClient", client), cfg, tmp_path
    )
    assert out.thumb_urls == ["https://s/t.jpg"]  # 外部列表原样
    assert len(out.poster_urls) == 1
    assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)  # 内部派生
    # 派生 args 为配置比例, 非候选 300/420
    url_hash = out.poster_urls[0].rsplit("/", 1)[-1]
    got = await resource_store.get_by_url_hash(url_hash)
    assert got is not None
    crop_res, crop_path = got
    assert crop_res.meta is not None
    assert crop_res.meta.get("args") == "0.7000"
    poster = Image.open(crop_path)
    assert poster.size == (376, 538)  # int(538 * 0.7), 非 int(538 * 300/420)=384


@pytest.mark.asyncio
async def test_poster_external_when_candidate_tall(resource_store: ResourceStore, tmp_path: Path):
    """poster 候选够高 → 不裁剪, poster 记外部 URL."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (379, 538),  # b/h=1.0 ≥ 0.9 → 不裁
        }
    )
    out = await materialize_images(
        ["https://s/p.jpg"], ["https://s/t.jpg"], [], resource_store, cast("WebClient", client), HotSettings(), tmp_path
    )
    assert out.poster_urls == ["https://s/p.jpg"]  # 外部列表原样


@pytest.mark.asyncio
async def test_no_poster_source_crops_from_thumb(resource_store: ResourceStore, tmp_path: Path):
    """无 poster 候选 → 从 thumb 裁剪 (内部 URL)."""
    client = FakeClient({"https://s/t.jpg": (800, 538)})
    out = await materialize_images(
        [], ["https://s/t.jpg"], [], resource_store, cast("WebClient", client), HotSettings(), tmp_path
    )
    assert len(out.poster_urls) == 1
    assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)


@pytest.mark.asyncio
async def test_trailer_downloaded_external_url(resource_store: ResourceStore, tmp_path: Path):
    client = FakeClient({"https://s/t.jpg": (800, 538)})
    settings = HotSettings()
    settings.scraping.download_resources.append(DownloadableResource.trailer)
    out = await materialize_images(
        [],
        ["https://s/t.jpg"],
        ["https://s/trailer.mp4"],
        resource_store,
        cast("WebClient", client),
        settings,
        tmp_path,
    )
    assert out.trailer_urls == ["https://s/trailer.mp4"]
    assert "https://s/trailer.mp4" in client.downloaded  # 已下载到本地


@pytest.mark.asyncio
async def test_empty_sources(resource_store: ResourceStore, tmp_path: Path):
    out = await materialize_images(
        [], [], [], resource_store, cast("WebClient", FakeClient({})), HotSettings(), tmp_path
    )
    assert out.poster_urls == []
    assert out.thumb_urls == []
    assert out.trailer_urls == []


@pytest.mark.asyncio
async def test_eager_sr_overwrites(resource_store: ResourceStore, tmp_path: Path, monkeypatch):
    """sr.enabled 时, 低质图被就地超分 (meta 打 sr 标记)."""
    from amane.sr.run import SrResult

    async def fake_run_sr(inp, out, cfg, data_dir, **kwargs):
        Image.new("RGB", (1600, 1076), "green").save(out)
        return SrResult(success=True, output=out)

    monkeypatch.setattr(pipeline_mod, "run_SR", fake_run_sr)

    client = FakeClient({"https://s/t.jpg": (800, 538)})  # max=800 < 1200 → 超分
    cfg = HotSettings()
    cfg.sr.enabled = True
    out = await materialize_images(
        [], ["https://s/t.jpg"], [], resource_store, cast("WebClient", client), cfg, tmp_path
    )

    thumb_res = await resource_store.get_by_url("https://s/t.jpg")
    assert thumb_res is not None
    assert thumb_res.meta is not None and "sr" in thumb_res.meta
    assert out.thumb_urls == ["https://s/t.jpg"]  # URL 不变 (就地覆盖)


@pytest.mark.asyncio
async def test_download_resources_gates_acquire(resource_store: ResourceStore, tmp_path: Path):
    """未选中的类型不写入 Resource."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (400, 600),
            "https://s/trailer.mp4": (1, 1),
        }
    )
    cfg = HotSettings(scraping=ScrapingConfig(download_resources=[DownloadableResource.thumb], field_priority={}))
    await materialize_images(
        ["https://s/p.jpg"],
        ["https://s/t.jpg"],
        ["https://s/trailer.mp4"],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        extrafanart_urls={"javdb": ["https://s/e1.jpg"]},
    )
    assert await resource_store.get_by_url("https://s/t.jpg") is not None
    assert await resource_store.get_by_url("https://s/p.jpg") is None
    assert await resource_store.get_by_url("https://s/trailer.mp4") is None
    assert "https://s/t.jpg" in client.downloaded
    assert "https://s/p.jpg" not in client.downloaded
    assert "https://s/trailer.mp4" not in client.downloaded


def test_success_first_partition():
    """稳定分区: 成功保序前置, 失败保序排到末尾; 空成功集幂等."""
    cases = [
        (["d1", "l1", "d2", "l2"], {"l1", "l2"}, ["l1", "l2", "d1", "d2"]),
        (["a", "b"], {"a", "b"}, ["a", "b"]),  # 全成功
        (["a", "b"], set(), ["a", "b"]),  # 全失败
        (["a", "b", "c"], {"c"}, ["c", "a", "b"]),  # 尾部成功提至首位
        (["a", "b", "c"], {"b"}, ["b", "a", "c"]),  # 中部成功前置
        ([], set(), []),
    ]
    for urls, ok, expected in cases:
        assert pipeline_mod._success_first(urls, ok) == expected


@pytest.mark.asyncio
async def test_poster_reordered_when_first_url_dead(resource_store: ResourceStore, tmp_path: Path):
    """poster 首位 URL 下载失败 → 成功者前置 (无 thumb 不触发裁剪)."""
    client = FakeClient(sizes={"https://s/live.jpg": (400, 600)}, fail={"https://s/dead.jpg"})
    out = await materialize_images(
        ["https://s/dead.jpg", "https://s/live.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    assert out.poster_urls == ["https://s/live.jpg", "https://s/dead.jpg"]
    assert "https://s/dead.jpg" in client.downloaded  # 死 URL 仍尝试下载


@pytest.mark.asyncio
async def test_thumb_trailer_reordered_when_first_dead(resource_store: ResourceStore, tmp_path: Path):
    """thumb/trailer 首位 URL 下载失败 → 成功者前置."""
    client = FakeClient(sizes={"https://s/live.jpg": (800, 538)}, fail={"https://s/dead.jpg", "https://s/dead.mp4"})
    settings = HotSettings()
    settings.scraping.download_resources.append(DownloadableResource.trailer)
    out = await materialize_images(
        [],
        ["https://s/dead.jpg", "https://s/live.jpg"],
        ["https://s/dead.mp4", "https://s/trailer.mp4"],
        resource_store,
        cast("WebClient", client),
        settings,
        tmp_path,
    )
    assert out.thumb_urls == ["https://s/live.jpg", "https://s/dead.jpg"]
    assert out.trailer_urls == ["https://s/trailer.mp4", "https://s/dead.mp4"]


@pytest.mark.asyncio
async def test_all_urls_fail_keeps_order(resource_store: ResourceStore, tmp_path: Path):
    """全部 URL 下载失败 → 列表保持原序 (死 URL 留尾供来源恢复后重试)."""
    client = FakeClient(sizes={}, fail={"https://s/a.jpg", "https://s/b.jpg"})
    out = await materialize_images(
        ["https://s/a.jpg", "https://s/b.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    assert out.poster_urls == ["https://s/a.jpg", "https://s/b.jpg"]


@pytest.mark.asyncio
async def test_unselected_kind_ranked_by_probed_size(resource_store: ResourceStore, tmp_path: Path):
    """未选中下载的 poster 只探头部尺寸: 仍按分辨率降序, 且不落盘."""
    cfg = HotSettings(scraping=ScrapingConfig(download_resources=[DownloadableResource.thumb], field_priority={}))
    client = FakeClient(sizes={"https://s/small.jpg": (200, 300), "https://s/big.jpg": (800, 1200)})
    out = await materialize_images(
        ["https://s/small.jpg", "https://s/big.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
    )
    assert out.poster_urls == ["https://s/big.jpg", "https://s/small.jpg"]
    assert client.downloaded == []  # 未选中下载则不落盘
    assert client.probed == ["https://s/small.jpg", "https://s/big.jpg"]


@pytest.mark.asyncio
async def test_unselected_kind_probe_failure_ordered_last(resource_store: ResourceStore, tmp_path: Path):
    """探尺寸失败的候选 (不可达 / 非图像) 保序垫底, 可读候选按分辨率降序."""
    cfg = HotSettings(scraping=ScrapingConfig(download_resources=[], field_priority={}))
    client = FakeClient({"https://s/mid.jpg": (400, 600)}, fail={"https://s/dead.jpg"})
    urls = ["https://s/dead.jpg", "https://s/mid.jpg", "https://s/notimage.png"]
    out = await materialize_images(list(urls), list(urls), [], resource_store, cast("WebClient", client), cfg, tmp_path)
    assert out.poster_urls == ["https://s/mid.jpg", "https://s/dead.jpg", "https://s/notimage.png"]
    assert out.thumb_urls == ["https://s/mid.jpg", "https://s/dead.jpg", "https://s/notimage.png"]


@pytest.mark.asyncio
async def test_downloaded_kind_not_probed(resource_store: ResourceStore, tmp_path: Path):
    """已选中下载的类型用本地文件尺寸排序, 不发探尺寸请求."""
    client = FakeClient({"https://s/small.jpg": (200, 300), "https://s/big.jpg": (800, 1200)})
    out = await materialize_images(
        ["https://s/small.jpg", "https://s/big.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    assert out.poster_urls == ["https://s/big.jpg", "https://s/small.jpg"]
    assert client.probed == []
    assert client.downloaded == ["https://s/small.jpg", "https://s/big.jpg"]


@pytest.mark.asyncio
async def test_crop_prefers_highest_resolution_thumb_without_thumb_download(
    resource_store: ResourceStore, tmp_path: Path
):
    """poster 选中下载而 thumb 未选: 触发裁剪时临时 acquire 分辨率最高的 thumb, 非列表首个."""
    cfg = HotSettings(scraping=ScrapingConfig(download_resources=[DownloadableResource.poster], field_priority={}))
    client = FakeClient(
        {
            "https://s/small_cover.jpg": (800, 538),
            "https://s/big_cover.jpg": (1600, 1076),
            "https://s/poster.jpg": (300, 420),  # b/h=0.39 < 0.9 → 裁
        }
    )
    out = await materialize_images(
        ["https://s/poster.jpg"],
        ["https://s/small_cover.jpg", "https://s/big_cover.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
    )
    assert "https://s/big_cover.jpg" in client.downloaded
    assert "https://s/small_cover.jpg" not in client.downloaded
    got = await resource_store.get_by_url_hash(out.poster_urls[0].rsplit("/", 1)[-1])
    assert got is not None
    crop_res, crop_path = got
    assert crop_res.url == derived_locator("https://s/big_cover.jpg", "crop", "0.7000")
    assert Image.open(crop_path).size == (753, 1076)  # int(1076 * 0.7)


@pytest.mark.asyncio
async def test_cache_hit_counts_as_success(resource_store: ResourceStore, tmp_path: Path):
    """Resource 缓存命中计成功: 已缓存 URL 前置, 且不重复下载."""
    seed = FakeClient(sizes={"https://s/live.jpg": (800, 538)})
    assert await resource_store.acquire("https://s/live.jpg", cast("WebClient", seed)) is not None

    client = FakeClient(sizes={}, fail={"https://s/dead.jpg"})
    out = await materialize_images(
        ["https://s/dead.jpg", "https://s/live.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    assert out.poster_urls == ["https://s/live.jpg", "https://s/dead.jpg"]
    assert "https://s/live.jpg" not in client.downloaded  # 缓存命中, 无网络请求
    assert "https://s/dead.jpg" in client.downloaded
