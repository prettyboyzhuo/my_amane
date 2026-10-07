"""就地超分低质图. 去重依据 Resource.meta 的 sr 键 (覆盖原文件, URL 不变).
单个失败不阻断整批.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..media import needs_upscale, probe_size
from ..media.pipeline import sr_args_dict
from ..sr import run_SR
from .models import UpscalePayload, UpscaleResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config import HotSettings
    from ..media import ResourceStore

logger = structlog.get_logger()

_IMAGE_MIME_PREFIX = "image/"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _is_image(mime: str | None, file_path: str) -> bool:
    if mime:
        return mime.startswith(_IMAGE_MIME_PREFIX)
    return Path(file_path).suffix.lower() in _IMAGE_EXTS


class UpscaleHandler(TaskHandler[UpscalePayload, UpscaleResult]):
    def __init__(self, resource_store: ResourceStore, config: HotSettings):
        super().__init__(payload_t=UpscalePayload, result_t=UpscaleResult)
        self._store = resource_store
        self._config = config

    async def _sr_producer(self, src: Path, out: Path) -> bool:
        result = await run_SR(src, out, self._config.sr, self._store.data_dir)
        return result.success

    async def handle(self, payload: UpscalePayload) -> TaskResult[UpscaleResult]:
        max_dim = payload.max_dim_threshold or self._config.sr.max_dim_threshold
        max_bytes = payload.max_bytes_threshold or self._config.sr.max_bytes_threshold

        resources = await self._store.list_all()
        scanned = upscaled = skipped = failed = 0

        for res in resources:
            if upscaled >= payload.limit:
                break
            scanned += 1

            # 已超分或非图则跳过.
            if res.meta and "sr" in res.meta:
                skipped += 1
                continue
            if not _is_image(res.mime_type, res.file_path):
                skipped += 1
                continue

            full = self._store.full_path(res)
            size = probe_size(full)
            file_bytes = full.stat().st_size if full.exists() else 0
            # 未达阈值则跳过.
            if not needs_upscale(size, file_bytes, max_dim_threshold=max_dim, max_bytes_threshold=max_bytes):
                skipped += 1
                continue

            ok = await self._store.upscale_in_place(res, sr_args_dict(self._config.sr), self._sr_producer)
            if ok:
                upscaled += 1
            else:
                failed += 1

        logger.info("upscale completed", scanned=scanned, upscaled=upscaled, skipped=skipped, failed=failed)
        return TaskResult(
            success=failed == 0,
            result=UpscaleResult(scanned=scanned, upscaled=upscaled, skipped=skipped, failed=failed),
        )
