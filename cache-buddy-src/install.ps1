# Cache Buddy install: ccb on the user PATH + a one-click desktop shortcut.
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$ico = Join-Path $src "cache_buddy.ico"
$desk = [Environment]::GetFolderPath("Desktop")

# 1) user PATH — read/write the registry value directly so nothing is
#    truncated (setx caps at 1024 chars)
$p = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not (($p -split ";") -contains $src)) {
    [Environment]::SetEnvironmentVariable("Path", ($p.TrimEnd(";") + ";" + $src), "User")
    Write-Host "PATH += $src"
} else {
    Write-Host "PATH already has $src"
}

$sh = New-Object -ComObject WScript.Shell

# 2) the old per-launch "Claude Code (Cache Buddy)" shortcut is gone: the
#    panel now routes every claude launch itself while it's open
$old = Join-Path $desk "Claude Code (Cache Buddy).lnk"
if (Test-Path $old) { Remove-Item $old; Write-Host "removed: Claude Code (Cache Buddy)" }

# 3) the one-click panel: proxy + routing + Claude Code/Devin guard. Ctrl+Alt+B
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe).Source }
$lnk = $sh.CreateShortcut((Join-Path $desk "Cache Buddy.lnk"))
$lnk.TargetPath = $py
$lnk.Arguments = "`"$src\cc_buddy.py`""
$lnk.WorkingDirectory = $src
$lnk.IconLocation = "$ico,0"
$lnk.Hotkey = "CTRL+ALT+B"
$lnk.Description = "Cache Buddy: keep Claude Code / Devin prompt caches warm"
$lnk.Save()
Write-Host "shortcut: Cache Buddy  [Ctrl+Alt+B]"

