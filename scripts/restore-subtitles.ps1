<#
.SYNOPSIS
把链接目录里的字幕回迁到库内视频目录.

.DESCRIPTION
整理按库的 subtitle_template (默认 {link_dir}/{raw_srt_name}.{ext}) 把视频同级字幕搬到链接目录.
元数据变化导致链接目录改变后, 字幕留在旧目录, 而再次整理只扫描库内视频的同级目录, 因此发现不到它.
本脚本按链接目录里的入口 (软链接 / .strm) 反查视频位置, 把字幕搬回视频同级, 供下一次整理认领.

默认只读; -Apply 才移动文件. 库内已存在同名文件时跳过并记录, 不覆盖.
多个库可以写入同一链接根 (如 AV1 / AV2 / AV3 都写 D:\vedio\av), 该目录只遍历一次.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\restore-subtitles.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\restore-subtitles.ps1 -Apply -Limit 20 -Report D:\restore.csv

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\restore-subtitles.ps1 -Pair 'H:\av-part1=D:\vedio\av'

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\restore-subtitles.ps1 -Pair 'H:\av-part1=D:\vedio\av;G:\av-part2=D:\vedio\av'
#>

[CmdletBinding()]
param(
    # 实际移动文件; 缺省只输出报告
    [switch]$Apply,
    # 本次最多移动多少个文件, 0 表示不限制
    [int]$Limit = 0,
    # 字幕扩展名
    [string[]]$Ext = @('.srt', '.ass', '.ssa', '.vtt', '.sub'),
    # 视频扩展名, 用于入口失效时按名称在库内反查
    [string[]]$VideoExt = @('.mp4', '.mkv', '.avi', '.wmv', '.flv', '.mov', '.ts', '.iso', '.strm'),
    # 报告输出路径
    [string]$Report = 'restore_subtitles.csv',
    # 只处理指定分组, 形式 库根=链接根, 多组用分号分隔; 缺省使用下面的 $Libraries
    [string[]]$Pair = @()
)

$ErrorActionPreference = 'Stop'

# 每个库一组: 库根与它写出链接的链接根. 多个库可以写到同一链接根.
$Libraries = @(
    @{ Root = 'F:\giga';     Link = 'D:\vedio\giga' },
    @{ Root = 'H:\av-part1'; Link = 'D:\vedio\av' },
    @{ Root = 'G:\av-part2'; Link = 'D:\vedio\av' },
    @{ Root = 'F:\av-part3'; Link = 'D:\vedio\av' }
)

$SubtitleExtensions = $Ext | ForEach-Object { $_.ToLowerInvariant() }
$TrashPattern = '[\\/]\.amane_trash([\\/]|$)'

function Remove-ExtendedPrefix {
    # Path.symlink_to 写入绝对目标时系统存的是 \\?\ 形式, 该前缀下的路径与普通绝对路径不属于同一目录树.
    param([string]$Path)
    if ($Path.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) {
        return '\\' + $Path.Substring(8)
    }
    if ($Path.StartsWith('\\?\', [System.StringComparison]::Ordinal)) {
        return $Path.Substring(4)
    }
    return $Path
}

function Get-FirstLine {
    param([string]$Path)
    $reader = [System.IO.File]::OpenText($Path)
    try {
        while ($null -ne ($line = $reader.ReadLine())) {
            $text = $line.Trim()
            if ($text) { return $text }
        }
    } finally {
        $reader.Dispose()
    }
    return ''
}

function Get-PointerTarget {
    # 目录内的入口指向: 软链接目标优先, 其次 .strm 正文
    param([System.IO.DirectoryInfo]$Dir)
    foreach ($item in (Get-ChildItem -LiteralPath $Dir.FullName -Force -File -ErrorAction SilentlyContinue | Sort-Object Name)) {
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            $target = @($item.Target)[0]
            if (-not $target) { continue }
            if (-not [System.IO.Path]::IsPathRooted($target)) {
                $target = Join-Path $item.DirectoryName $target
            }
            return @{ Target = (Remove-ExtendedPrefix $target); Method = 'symlink' }
        }
        if ($item.Extension.ToLowerInvariant() -eq '.strm') {
            $text = Get-FirstLine $item.FullName
            if ($text -and [System.IO.Path]::IsPathRooted($text)) {
                return @{ Target = (Remove-ExtendedPrefix $text); Method = 'strm' }
            }
        }
    }
    return $null
}

function Test-UnderRoot {
    param([string]$Path, [string[]]$Roots)
    $full = [System.IO.Path]::GetFullPath($Path)
    foreach ($root in $Roots) {
        $base = [System.IO.Path]::GetFullPath($root).TrimEnd('\')
        if ($full.Equals($base, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($full.StartsWith($base + '\', [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Build-VideoIndex {
    # 库内视频按文件名主干索引, 供入口失效时反查当前位置
    param([string[]]$Roots)
    $index = @{}
    foreach ($root in $Roots) {
        $files = Get-ChildItem -LiteralPath $root -Recurse -Force -File -ErrorAction SilentlyContinue
        foreach ($file in $files) {
            if ($file.FullName -match $TrashPattern) { continue }
            if ($VideoExt -notcontains $file.Extension.ToLowerInvariant()) { continue }
            $key = [System.IO.Path]::GetFileNameWithoutExtension($file.Name).ToLowerInvariant()
            if (-not $index.ContainsKey($key)) { $index[$key] = @() }
            $index[$key] += $file.FullName
        }
    }
    return $index
}

function New-Record {
    param([string]$Status, [string]$Method, [string]$Source, [string]$Dest, [string]$Note)
    return [pscustomobject]@{
        状态 = $Status
        方式 = $Method
        字幕 = $Source
        目标 = $Dest
        备注 = $Note
    }
}

function Get-Groups {
    # 按链接根分组: 多个库写入同一链接根时, 该目录只遍历一次
    $pairs = New-Object System.Collections.Generic.List[object]
    if ($Pair.Count -gt 0) {
        foreach ($entry in ($Pair -split ';')) {
            $item = $entry.Trim()
            if (-not $item) { continue }
            $parts = $item -split '=', 2
            if ($parts.Count -ne 2 -or -not $parts[0] -or -not $parts[1]) {
                throw "-Pair 需要 库根=链接根 形式: $item"
            }
            $pairs.Add(@{ Root = $parts[0]; Link = $parts[1] })
        }
    } else {
        foreach ($item in $Libraries) { $pairs.Add($item) }
    }
    $groups = New-Object System.Collections.Generic.List[object]
    $rootsByLink = @{}
    foreach ($item in $pairs) {
        $link = [System.IO.Path]::GetFullPath($item.Link)
        if (-not $rootsByLink.ContainsKey($link)) {
            $rootsByLink[$link] = New-Object System.Collections.Generic.List[string]
            $groups.Add(@{ Link = $link; Roots = $rootsByLink[$link] })
        }
        $root = [System.IO.Path]::GetFullPath($item.Root)
        if (-not $rootsByLink[$link].Contains($root)) { $rootsByLink[$link].Add($root) }
    }
    return $groups
}

function Get-Plan {
    param([string]$LinkRoot, [string[]]$Roots)
    $index = Build-VideoIndex -Roots $Roots
    $records = New-Object System.Collections.Generic.List[object]
    $dirs = New-Object System.Collections.Generic.List[object]
    $dirs.Add((Get-Item -LiteralPath $LinkRoot -Force))
    foreach ($dir in (Get-ChildItem -LiteralPath $LinkRoot -Recurse -Force -Directory -ErrorAction SilentlyContinue)) {
        $dirs.Add($dir)
    }
    foreach ($dir in $dirs) {
        if ($dir.FullName -match $TrashPattern) { continue }
        $subtitles = @(
            Get-ChildItem -LiteralPath $dir.FullName -Force -File -ErrorAction SilentlyContinue |
                Where-Object { $SubtitleExtensions -contains $_.Extension.ToLowerInvariant() } |
                Sort-Object Name
        )
        if ($subtitles.Count -eq 0) { continue }

        $video = ''
        $method = '-'
        $note = '目录内没有找到软链接或 .strm 入口'
        $pointer = Get-PointerTarget -Dir $dir
        if ($pointer) {
            $method = $pointer.Method
            $target = $pointer.Target
            if ((Test-Path -LiteralPath $target -PathType Leaf) -and (Test-UnderRoot -Path $target -Roots $Roots)) {
                $video = $target
                $note = ''
            } else {
                $key = [System.IO.Path]::GetFileNameWithoutExtension($target).ToLowerInvariant()
                if (Test-Path -LiteralPath $target) {
                    $note = "入口指向库外: $target"
                } else {
                    $note = "入口目标不存在: $target"
                }
                $candidates = @($index[$key])
                if ($candidates.Count -eq 1) {
                    $video = $candidates[0]
                    $method = "$method+库内定位"
                } elseif ($candidates.Count -gt 1) {
                    $note = "库内同名视频不唯一: " + ($candidates -join ', ')
                } else {
                    $note = "$note; 库内也找不到该视频"
                }
            }
        }

        foreach ($subtitle in $subtitles) {
            if (-not $video) {
                $records.Add((New-Record -Status 'unresolved' -Method $method -Source $subtitle.FullName -Dest '' -Note $note))
                continue
            }
            $dest = Join-Path (Split-Path -Parent $video) $subtitle.Name
            if (Test-Path -LiteralPath $dest) {
                $records.Add((New-Record -Status 'skipped' -Method $method -Source $subtitle.FullName -Dest $dest -Note '库内已存在同名文件'))
            } else {
                $records.Add((New-Record -Status 'planned' -Method $method -Source $subtitle.FullName -Dest $dest -Note $note))
            }
        }
    }
    return $records
}

$groups = Get-Groups
if ($groups.Count -eq 0) {
    Write-Error "未配置媒体库: 请填写脚本顶部的 \$Libraries, 或用 -Pair 指定"
    exit 2
}

$all = New-Object System.Collections.Generic.List[object]
foreach ($group in $groups) {
    Write-Host "[链接] $($group.Link)  库: $($group.Roots -join ', ')"
    $available = @($group.Roots | Where-Object { Test-Path -LiteralPath $_ -PathType Container })
    $missing = @($group.Roots | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Container) })
    if ($missing.Count -gt 0) { Write-Host "  跳过不存在的库根: $($missing -join ', ')" }
    if ($available.Count -eq 0 -or -not (Test-Path -LiteralPath $group.Link -PathType Container)) {
        Write-Host "  跳过: 链接根或全部库根不存在 — $($group.Link)"
        continue
    }
    $found = Get-Plan -LinkRoot $group.Link -Roots $available
    $planned = @($found | Where-Object { $_.状态 -eq 'planned' }).Count
    $skipped = @($found | Where-Object { $_.状态 -eq 'skipped' }).Count
    $unresolved = @($found | Where-Object { $_.状态 -eq 'unresolved' }).Count
    Write-Host "  planned=$planned, skipped=$skipped, unresolved=$unresolved"
    foreach ($record in $found) { $all.Add($record) }
}

$targets = @($all | Where-Object { $_.状态 -eq 'planned' })
if ($Limit -gt 0 -and $targets.Count -gt $Limit) { $targets = $targets[0..($Limit - 1)] }

if ($Apply) {
    foreach ($record in $targets) {
        try {
            Move-Item -LiteralPath $record.字幕 -Destination $record.目标 -ErrorAction Stop
            $record.状态 = 'moved'
        } catch {
            $record.状态 = 'skipped'
            $record.备注 = "移动失败: $($_.Exception.Message)"
        }
    }
} else {
    Write-Host '只读模式: 未移动任何文件 (加 -Apply 执行)'
}

$all | Export-Csv -LiteralPath $Report -NoTypeInformation -Encoding UTF8
$moved = @($all | Where-Object { $_.状态 -eq 'moved' }).Count
$skipped = @($all | Where-Object { $_.状态 -eq 'skipped' }).Count
$unresolved = @($all | Where-Object { $_.状态 -eq 'unresolved' }).Count
Write-Host "总计: moved=$moved, skipped=$skipped, unresolved=$unresolved; 报告: $((Resolve-Path -LiteralPath $Report).Path)"
foreach ($record in ($all | Where-Object { $_.状态 -in @('unresolved', 'skipped') })) {
    Write-Host "  [$($record.状态)] $($record.字幕) — $($record.备注)"
}
if ($Apply) {
    Write-Host '后续: 到 Amane 点整理, 字幕会按新的链接模板落盘; 之后清理旧链接目录'
}
