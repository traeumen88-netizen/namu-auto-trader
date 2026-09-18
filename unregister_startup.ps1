$StartupPath = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Startup)
$ShortcutPath = Join-Path $StartupPath "AI_Quant_Live_Trader.lnk"
if (Test-Path $ShortcutPath) {
    Remove-Item -Force $ShortcutPath
    Write-Host "UNREGISTERED: Removed $ShortcutPath"
} else {
    Write-Host "NOT_FOUND: No startup shortcut found."
}
