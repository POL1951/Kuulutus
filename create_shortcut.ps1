# Creates a desktop shortcut "Kuuluttaja" pointing to start_kuuluttaja.bat
$desktop      = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Kuuluttaja.lnk'
$target       = 'C:\juoksu\start_kuuluttaja.bat'
$workingDir   = 'C:\juoksu'

$shell    = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath       = $target
$shortcut.WorkingDirectory = $workingDir
$shortcut.Description       = 'Kuuluttaja - announcer display'

# Use a built-in stopwatch/clock style icon from shell32.dll; fall back to default if missing.
$iconCandidate = "$env:SystemRoot\System32\imageres.dll,114"
$shortcut.IconLocation = $iconCandidate

$shortcut.Save()

Write-Host "Shortcut created: $shortcutPath"
Write-Host "Target:           $($shortcut.TargetPath)"
Write-Host "WorkingDirectory: $($shortcut.WorkingDirectory)"
Write-Host "IconLocation:     $($shortcut.IconLocation)"
