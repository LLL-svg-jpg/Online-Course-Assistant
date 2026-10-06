# 重新生成指向正式发布版的启动快捷方式。
$ErrorActionPreference = "Stop"
$scriptPath = $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($scriptPath)) {
    throw "无法确定脚本目录，未改动快捷方式。"
}
$root = Split-Path -Parent (Split-Path -Parent $scriptPath)
$exe = Join-Path $root "dist\OnlineCourseAssistant-v1.0.3-windows-x64\OnlineCourseAssistant\OnlineCourseAssistant.exe"
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "没有找到正式发布版：$exe"
}
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $root "Online Course Assistant.lnk"))
$lnk.TargetPath = $exe
$lnk.Arguments = ""
$lnk.WorkingDirectory = Split-Path -Parent $exe
$lnk.IconLocation = "$exe,0"
$lnk.Description = "Online Course Assistant"
$lnk.Save()
Write-Output "快捷方式已生成: $root\Online Course Assistant.lnk"
