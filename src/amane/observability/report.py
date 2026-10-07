"""从 summary.json 直接投影, 非记录导出."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ..db.models import TaskStatus, TaskType
from ..net.errors import FailureReason
from .models import SiteOutcomeKind, SiteOutcomeRecord, TaskSummary
from .recorder import task_dir_for

if TYPE_CHECKING:
    from ..db.models import Task

_SITE_OUTCOME_TYPES = frozenset({TaskType.SCRAPE, TaskType.ACTOR_SCRAPE})


class TaskReport(BaseModel):
    """outcomes 与 summary.json 同一结构."""

    headline: str | None = None
    metadata_id: int | None = None
    actor_id: int | None = None
    """ACTOR_SCRAPE 缺 result.actor_id 时回退 payload.actor_id."""
    outcomes: list[SiteOutcomeRecord] = Field(default_factory=list)


def build_task_report(log_dir: Path, task: Task) -> TaskReport:
    """与落盘 summary.json 同源, 不做二次组装."""
    headline = task.error if task.status == TaskStatus.FAILED else None
    metadata_id = _positive_id(task.result, "metadata_id")
    actor_id = _actor_id_from_task(task)
    if task.type not in _SITE_OUTCOME_TYPES:
        return TaskReport(headline=headline, metadata_id=metadata_id, actor_id=actor_id, outcomes=[])

    assert task.id is not None
    root = task_dir_for(log_dir, task.id)
    summary = _load_summary(root)
    if summary is None:
        return TaskReport(headline=headline, metadata_id=metadata_id, actor_id=actor_id, outcomes=[])

    outcomes = [summary.outcomes[site] for site in summary.sites_queried if site in summary.outcomes]
    if not outcomes and (summary.failed_sites or summary.cache_hits):
        # RECORD_VERSION 1 降级: 纯字段映射, 不解析任何文本.
        outcomes = _legacy_outcomes(summary)

    return TaskReport(headline=headline, metadata_id=metadata_id, actor_id=actor_id, outcomes=outcomes)


def _positive_id(value: object, key: str) -> int | None:
    if not isinstance(value, dict):
        return None
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw > 0 else None


def _actor_id_from_task(task: Task) -> int | None:
    return _positive_id(task.result, "actor_id") or _positive_id(task.payload, "actor_id")


def _load_summary(root: Path) -> TaskSummary | None:
    path = root / "summary.json"
    if not path.is_file():
        return None
    try:
        return TaskSummary.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None


def _legacy_outcomes(summary: TaskSummary) -> list[SiteOutcomeRecord]:
    """RECORD_VERSION 1 降级: failed_sites / cache_hits 列表 → 站点结果记录 (无失败原因粒度)."""
    failed = set(summary.failed_sites or [])
    cache_hits = set(summary.cache_hits or [])
    out: list[SiteOutcomeRecord] = []
    for site in summary.sites_queried:
        if site in cache_hits:
            out.append(SiteOutcomeRecord(site=site, outcome=SiteOutcomeKind.CACHE_HIT))
        elif site in failed:
            out.append(
                SiteOutcomeRecord(site=site, outcome=SiteOutcomeKind.FAILED, reason=FailureReason.NO_USABLE_METADATA)
            )
        else:
            out.append(SiteOutcomeRecord(site=site, outcome=SiteOutcomeKind.OK))
    out.extend(
        SiteOutcomeRecord(site=site, outcome=SiteOutcomeKind.CACHE_HIT)
        for site in cache_hits
        if site not in summary.sites_queried
    )
    return out
