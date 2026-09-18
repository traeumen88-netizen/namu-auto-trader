$WshShell = New-Object -ComObject WScript.Shell
$StartupPath = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Startup)
$ShortcutPath = Join-Path $StartupPath "AI_Quant_Live_Trader.lnk"
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "C:\Users\DAPCHC-071\namu-auto-trader\run_live_trader.bat"
$Shortcut.Arguments = "--auto"
$Shortcut.WorkingDirectory = "C:\Users\DAPCHC-071\namu-auto-trader"
$Shortcut.Description = "Self-Improving Quant AI 실전투자 자동 실행"
$Shortcut.Save()
Write-Host "REGISTERED: $ShortcutPath"
