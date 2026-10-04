param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference = 'Stop'
$stageDir = [IO.Path]::GetFullPath($PSScriptRoot)
$backupDir = Join-Path $stageDir 'backup'
$packageDir = Join-Path $stageDir 'unpacked\OnlineCourseAssistant'
$oldMoved = [Collections.Generic.List[string]]::new()
$newMoved = [Collections.Generic.List[string]]::new()
$allowed = @('OnlineCourseAssistant.exe', '_internal', 'README.md', 'requirements.txt',
             '安装依赖.bat', 'config.example.toml', 'THIRD_PARTY_NOTICES.txt')
$applicationDir = $null
$canRestart = $false

function Assert-Child([string]$child, [string]$parent) {
    $full = [IO.Path]::GetFullPath($child)
    $prefix = [IO.Path]::GetFullPath($parent).TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw '更新路径超出允许目录。'
    }
    return $full
}

function Write-Result([string]$state, [string]$detail) {
    @{state=$state; detail=$detail; time=(Get-Date).ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stageDir 'result.json') -Encoding UTF8
}

function File-Hash([string]$file) {
    $stream = [IO.File]::OpenRead($file)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $hash.Dispose() }
}

try {
    if ([IO.Path]::GetFullPath($Request) -ne (Join-Path $stageDir 'request.json')) {
        throw '更新请求不在本次暂存目录。'
    }
    $data = Get-Content -Raw -LiteralPath $Request -Encoding UTF8 | ConvertFrom-Json
    $applicationDir = [IO.Path]::GetFullPath($data.application_dir).TrimEnd('\')
    $null = Assert-Child $stageDir (Join-Path $applicationDir 'runtime\updates')
    $null = Assert-Child $backupDir $stageDir
    $null = Assert-Child $packageDir $stageDir
    if ($data.version -notmatch '^\d+\.\d+\.\d+$' -or $data.parent_pid -le 0) {
        throw '更新请求的版本或进程无效。'
    }
    $cursor = $stageDir
    while ($cursor.Length -ge $applicationDir.Length) {
        if ((Get-Item -Force -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw '更新路径包含目录链接。'
        }
        $cursor = Split-Path -Parent $cursor
    }
    if (Test-Path -LiteralPath $backupDir) { throw '本次备份目录已经存在，不能重复安装。' }
    if (-not ($data.entries -contains 'OnlineCourseAssistant.exe') -or
        -not ($data.entries -contains '_internal')) { throw '更新请求缺少程序或依赖目录。' }
    foreach ($entry in $data.entries) {
        if ($allowed -cnotcontains $entry) { throw '更新请求包含非程序文件。' }
    }
    $properties = @($data.files.PSObject.Properties)
    $actualFiles = @(Get-ChildItem -Force -Recurse -LiteralPath $packageDir)
    if (@($actualFiles | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) {
        throw '更新包包含链接。'
    }
    if (@($actualFiles | Where-Object { -not $_.PSIsContainer }).Count -ne $properties.Count) {
        throw '更新包文件清单发生变化。'
    }
    foreach ($property in $properties) {
        $relative = $property.Name
        if ($relative -match '(^|/)\.\.(/|$)|[\\:]' -or
            $allowed -cnotcontains $relative.Split('/')[0]) { throw '文件清单路径无效。' }
        $file = Assert-Child (Join-Path $packageDir $relative) $packageDir
        if ((File-Hash $file) -ne $property.Value) {
            throw '更新暂存文件校验失败。'
        }
    }
    $newExe = Join-Path $packageDir 'OnlineCourseAssistant.exe'
    if ((Get-Item -LiteralPath $newExe).VersionInfo.FileVersion -ne ($data.version + '.0')) {
        throw '更新程序版本不符。'
    }
    $parentProcess = Get-Process -Id $data.parent_pid -ErrorAction SilentlyContinue
    if ($parentProcess -and -not $parentProcess.WaitForExit(60000)) {
        throw '旧程序尚未退出，已取消替换。'
    }
    $canRestart = $true
    $null = New-Item -ItemType Directory -Path $backupDir
    foreach ($entry in $data.entries) {
        $target = Join-Path $applicationDir $entry
        if (Test-Path -LiteralPath $target) {
            Move-Item -LiteralPath $target -Destination (Join-Path $backupDir $entry)
            $oldMoved.Add($entry)
        }
    }
    foreach ($entry in $data.entries) {
        Move-Item -LiteralPath (Join-Path $packageDir $entry) -Destination (Join-Path $applicationDir $entry)
        $newMoved.Add($entry)
    }
    Start-Process -FilePath (Join-Path $applicationDir 'OnlineCourseAssistant.exe') -WorkingDirectory $applicationDir
    Write-Result 'updated' ('已安装 v' + $data.version + '；旧程序备份在 ' + $backupDir)
    exit 0
} catch {
    $failure = $_.Exception.Message
    $rollbackFailed = $false
    try {
        for ($i = $newMoved.Count - 1; $i -ge 0; $i--) {
            $entry = $newMoved[$i]
            Move-Item -LiteralPath (Join-Path $applicationDir $entry) -Destination (Join-Path $packageDir $entry)
        }
        for ($i = $oldMoved.Count - 1; $i -ge 0; $i--) {
            $entry = $oldMoved[$i]
            Move-Item -LiteralPath (Join-Path $backupDir $entry) -Destination (Join-Path $applicationDir $entry)
        }
    } catch {
        $rollbackFailed = $true
        $failure += '；回退未完成：' + $_.Exception.Message
    }
    Write-Result $(if ($rollbackFailed) { 'rollback_failed' } else { 'failed' }) $failure
    if ($canRestart -and -not $rollbackFailed) {
        try {
            Start-Process -FilePath (Join-Path $applicationDir 'OnlineCourseAssistant.exe') -WorkingDirectory $applicationDir
        } catch { }
    }
    Add-Type -AssemblyName System.Windows.Forms
    $message = '更新未完成：' + $failure + "`n记录与原程序备份：" + $stageDir
    $null = [Windows.Forms.MessageBox]::Show($message, 'Online Course Assistant 更新', 'OK', 'Error')
    exit 1
}
