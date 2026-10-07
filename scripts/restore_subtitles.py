"""把链接目录里的字幕回迁到库内视频目录.

整理按库的 `subtitle_template` (默认 `{link_dir}/{raw_srt_name}.{ext}`) 把视频同级字幕搬到链接目录.
元数据变化导致链接目录改变后, 字幕留在旧目录, 而再次整理只扫描库内视频的同级目录, 因此发现不到它.
本脚本按链接目录里的入口 (软链接 / `.strm`) 反查视频位置, 把字幕搬回视频同级, 供下一次整理认领.

默认只读; `--apply` 才移动文件. 库内已存在同名文件时跳过并记录, 不覆盖.

多个库可以写入同一链接根 (如 AV1 / AV2 / AV3 都写 `D:\vedio\av`), 该目录只遍历一次, 每条记录按入口指向
判定归属库.

用法:

    uv run python scripts/restore_subtitles.py                # 只读报告 + 输出 CSV
    uv run python scripts/restore_subtitles.py --apply        # 执行移动
    uv run python scripts/restore_subtitles.py --apply --limit 20
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_SUBTITLE_EXTENSIONS: tuple[str, ...] = (".srt", ".ass", ".ssa", ".vtt", ".sub")
DEFAULT_VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".mkv",
    ".avi",
    ".wmv",
    ".flv",
    ".mov",
    ".ts",
    ".iso",
    ".strm",
)
TRASH_DIRNAME = ".amane_trash"

# 每个库一组: (库根, 链接根). 链接根是 link_template 渲染出的目录树.
# 多个库可以写到同一链接根 (AV1 / AV2 / AV3 都在 D:\vedio\av), 该目录只遍历一次.
LIBRARIES: tuple[tuple[str, str], ...] = (
    (r"F:\giga", r"D:\vedio\giga"),
    (r"H:\av-part1", r"D:\vedio\av"),
    (r"G:\av-part2", r"D:\vedio\av"),
    (r"F:\av-part3", r"D:\vedio\av"),
)


@dataclass(frozen=True, slots=True)
class Record:
    """一条搬迁记录. ``status`` 取值: moved / planned / skipped / unresolved."""

    status: str
    method: str
    source: Path
    dest: Path | None = None
    note: str = ""


def _first_line(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if text:
                return text
    return ""


def _in_trash(path: Path) -> bool:
    return TRASH_DIRNAME in path.parts


def _strip_extended(path: Path) -> Path:
    """去掉 Windows 扩展路径前缀.

    ``Path.symlink_to`` 写入绝对目标时, 系统存的是 ``\\\\?\\`` 形式; 该前缀下的路径与普通绝对路径
    比较时不被视为同一目录树.
    """
    text = os.fspath(path)
    if text[:4] != "\\\\?\\":
        return path
    rest = text[4:]
    return Path(f"\\\\{rest[4:]}") if rest[:4].upper() == "UNC\\" else Path(rest)


def _pointer_target(dir_path: Path) -> tuple[Path | None, str]:
    """目录内的入口指向: 软链接目标优先, 其次 `.strm` 正文. 返回 (目标, 方式)."""
    for child in sorted(dir_path.iterdir(), key=lambda path: path.name.casefold()):
        if child.is_symlink():
            target = _strip_extended(child.readlink())
            return (target if target.is_absolute() else child.parent / target), "symlink"
        if child.is_file() and child.suffix.lower() == ".strm":
            text = _first_line(child)
            if text and Path(text).is_absolute():
                return _strip_extended(Path(text)), "strm"
    return None, "-"


def _index_library(library_root: Path, video_extensions: set[str]) -> dict[str, list[Path]]:
    """库内视频按文件名主干索引, 供入口失效时反查当前位置."""
    index: dict[str, list[Path]] = {}
    for current, dirnames, filenames in os.walk(library_root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name != TRASH_DIRNAME]
        for name in filenames:
            path = Path(current) / name
            if path.suffix.lower() not in video_extensions:
                continue
            index.setdefault(path.stem.casefold(), []).append(path)
    return index


def _index_libraries(library_roots: Sequence[Path], video_extensions: set[str]) -> dict[str, list[Path]]:
    """共用一个链接根的多个库合并索引: 入口失效时按名称反查, 命中多个即视为歧义."""
    index: dict[str, list[Path]] = {}
    for library_root in library_roots:
        for stem, paths in _index_library(library_root, video_extensions).items():
            index.setdefault(stem, []).extend(paths)
    return index


def _group_libraries(pairs: Sequence[tuple[Path, Path]]) -> list[tuple[Path, list[Path]]]:
    """按链接根分组: 多个库写入同一链接根时, 该目录只遍历一次."""
    grouped: dict[Path, list[Path]] = {}
    for library_root, link_root in pairs:
        roots = grouped.setdefault(link_root, [])
        if library_root not in roots:
            roots.append(library_root)
    return list(grouped.items())


def _resolve_video(
    dir_path: Path, library_roots: Sequence[Path], index: dict[str, list[Path]]
) -> tuple[Path | None, str, str]:
    """反查该链接目录对应的库内视频: 入口指向有效则直接采用, 否则按文件名在库内定位."""
    target, method = _pointer_target(dir_path)
    if target is not None:
        if target.exists() and any(target.is_relative_to(root) for root in library_roots):
            return target, method, ""
        candidates = index.get(target.stem.casefold(), [])
        note = f"入口目标不存在: {target}" if not target.exists() else f"入口指向库外: {target}"
        if len(candidates) == 1:
            return candidates[0], f"{method}+库内定位", note
        if len(candidates) > 1:
            return None, method, f"库内同名视频不唯一: {', '.join(str(item) for item in candidates)}"
        return None, method, f"{note}; 库内也找不到该视频"
    return None, "-", "目录内没有找到软链接或 .strm 入口"


def _plan_link_root(
    link_root: Path, library_roots: Sequence[Path], subtitle_extensions: set[str], index: dict[str, list[Path]]
) -> list[Record]:
    records: list[Record] = []
    for current, _dirnames, filenames in os.walk(link_root, followlinks=False):
        dir_path = Path(current)
        if _in_trash(dir_path):
            continue
        subtitles = sorted(
            (dir_path / name for name in filenames if Path(name).suffix.lower() in subtitle_extensions),
            key=lambda path: path.name.casefold(),
        )
        if not subtitles:
            continue
        video, method, note = _resolve_video(dir_path, library_roots, index)
        if video is None:
            records.extend(Record("unresolved", method, item, note=note) for item in subtitles)
            continue
        for subtitle in subtitles:
            dest = video.parent / subtitle.name
            if dest.exists():
                records.append(Record("skipped", method, subtitle, dest, "库内已存在同名文件"))
            else:
                records.append(Record("planned", method, subtitle, dest, note))
    return records


def _move(record: Record) -> Record:
    assert record.dest is not None
    try:
        record.dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(record.source), str(record.dest))
    except OSError as e:
        return Record("skipped", record.method, record.source, record.dest, f"移动失败: {e}")
    return Record("moved", record.method, record.source, record.dest, record.note)


def _write_report(path: Path, records: Sequence[Record]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["状态", "方式", "字幕", "目标", "备注"])
        for record in records:
            writer.writerow([record.status, record.method, str(record.source), str(record.dest or ""), record.note])


def _summary(records: Sequence[Record]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    return ", ".join(f"{status}={count}" for status, count in sorted(counts.items())) or "无字幕需要处理"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把链接目录里的字幕回迁到库内视频目录")
    parser.add_argument("--apply", action="store_true", help="实际移动文件; 缺省只输出报告")
    parser.add_argument("--limit", type=int, default=0, help="本次最多移动多少个文件 (0 表示不限制)")
    parser.add_argument("--ext", nargs="+", default=list(DEFAULT_SUBTITLE_EXTENSIONS), help="字幕扩展名")
    parser.add_argument("--video-ext", nargs="+", default=list(DEFAULT_VIDEO_EXTENSIONS), help="视频扩展名")
    parser.add_argument("--report", default="restore_subtitles.csv", help="报告输出路径")
    parser.add_argument("--library-root", help="只处理这一个库根")
    parser.add_argument("--link-root", help="只处理这一个链接根")
    parser.add_argument("--pair", action="append", default=[], help="只处理这一组, 形式 库根=链接根 (可重复)")
    return parser.parse_args(argv)


def _libraries(args: argparse.Namespace) -> list[tuple[Path, Path]]:
    """命令行给出的分组优先于脚本顶部配置; 两者都没有则返回空列表."""
    pairs: list[tuple[Path, Path]] = []
    if args.library_root and args.link_root:
        pairs.append((Path(args.library_root), Path(args.link_root)))
    for item in args.pair:
        library, separator, link = item.partition("=")
        if not separator or not library or not link:
            raise SystemExit(f"--pair 需要 库根=链接根 形式: {item}")
        pairs.append((Path(library), Path(link)))
    if pairs:
        return pairs
    return [(Path(library), Path(link)) for library, link in LIBRARIES if library and link]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    subtitle_extensions = {extension.lower() for extension in args.ext}
    video_extensions = {extension.lower() for extension in args.video_ext}
    groups = _group_libraries(_libraries(args))
    if not groups:
        print(f"未配置媒体库: 请填写 {__file__} 顶部的 LIBRARIES", file=sys.stderr)  # noqa: T201
        return 2

    records: list[Record] = []
    for link_root, library_roots in groups:
        print(f"[链接] {link_root}  库: {', '.join(str(root) for root in library_roots)}")  # noqa: T201
        available = [root for root in library_roots if root.is_dir()]
        missing = [root for root in library_roots if not root.is_dir()]
        if missing:
            print(f"  跳过不存在的库根: {', '.join(str(root) for root in missing)}")  # noqa: T201
        if not available or not link_root.is_dir():
            print(f"  跳过: 链接根或全部库根不存在 — {link_root}")  # noqa: T201
            continue
        found = _plan_link_root(
            link_root, available, subtitle_extensions, _index_libraries(available, video_extensions)
        )
        print(f"  {_summary(found)}")  # noqa: T201
        records.extend(found)

    planned = [record for record in records if record.status == "planned"]
    if args.limit > 0:
        planned = planned[: args.limit]
    if args.apply:
        done = {record.source: _move(record) for record in planned}
        records = [done.get(record.source, record) for record in records]
    else:
        print("只读模式: 未移动任何文件 (加 --apply 执行)")  # noqa: T201

    report = Path(args.report)
    _write_report(report, records)
    print(f"总计 {_summary(records)}; 报告: {report.resolve()}")  # noqa: T201
    for record in records:
        if record.status in {"unresolved", "skipped"}:
            print(f"  [{record.status}] {record.source} — {record.note}")  # noqa: T201
    if args.apply:
        print("后续: 到 Amane 点整理, 字幕会按新的链接模板落盘; 之后清理旧链接目录")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
