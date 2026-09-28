<#
Smoke test of the packaged Windows build: the portable zip's command line (versions, the selftest with a
real YouTube download, a Spotify track, an update check against GitHub), a downloaded engine update, then
the installer, the installed window (with a screenshot) closing to the tray, a second start showing it
instead of running twice, Quit from the tray menu, Siphon updating itself from a local test release (not
while the session ends; installed as it quits; and by siphon-cli update, which opens it again) and the
uninstaller. Nothing makes a sound. Every check runs even when an
earlier one failed; the script fails at the end if any did. The workflow runs it with a PATH that holds
only Windows itself and MSYS2 moved away, which proves the app needs nothing else.
#>
param(
    [Parameter(Mandatory)] [string] $Portable,  # Siphon-<ver>-Windows-portable.zip
    [Parameter(Mandatory)] [string] $Setup,     # Siphon-<ver>-Setup.exe
    [Parameter(Mandatory)] [string] $Out,       # screenshot and logs for the workflow artifacts
    [string] $Engine                            # a folder with engine.json and wheels (optional)
)
$ErrorActionPreference = 'Stop'
$env:SIPHON_AO = 'null'
New-Item -ItemType Directory -Force $Out | Out-Null
$failed = [Collections.Generic.List[string]]::new()

function Check([string] $name, [scriptblock] $body) {
    Write-Host "`n=== $name"
    try { & $body; Write-Host "PASS $name" }
    catch { Write-Host "FAIL $name - $($_.Exception.Message)"; $failed.Add($name) }
}

function Run([string] $exe, [string[]] $arguments) {
    Write-Host "> $(Split-Path $exe -Leaf) $arguments"
    $lines = & $exe @arguments 2>&1 | ForEach-Object { "$_" }
    $lines | Write-Host
    if ($LASTEXITCODE -ne 0) { throw "$(Split-Path $exe -Leaf) $arguments exited with $LASTEXITCODE" }
    return $lines -join "`n"
}

function Expect([string] $text, [string] $pattern, [string] $what) {
    if ($text -notmatch $pattern) { throw "expected $what" }
    Write-Host "  ok: $what"
}

function Assert-SiphonWindow([Diagnostics.Process] $process) {
    $process.Refresh()  # a crash on start shows PyInstaller's error box instead, and keeps the process alive
    if ($process.MainWindowTitle -ne 'Siphon') { throw "the main window is '$($process.MainWindowTitle)', not Siphon" }
}

function Screenshot([string] $path) {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
    $bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose(); $bitmap.Dispose()
}

# The tray's hidden window (siphon/wintray.py), found by its class: a second start sends it its command line,
# and this script sends it what the tray menu would. FindWindowW's title is a pointer, passed as IntPtr.Zero (NULL:
# any title): PowerShell hands a .NET string parameter "" for $null, and FindWindow would then look for a window
# with an empty title, which the tray's ("Siphon Tray") is not.
Add-Type -Namespace SiphonSmoke -Name Win32 -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindowW(string cls, IntPtr title);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
[DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam);
[DllImport("user32.dll")] public static extern IntPtr SendMessageTimeoutW(IntPtr hwnd, uint msg, IntPtr wParam,
    IntPtr lParam, uint flags, uint timeout, out IntPtr result);
'@
$TrayClass = 'io.github.ggrrk.Siphon.Tray'
$QuitCommand = 8  # the tray menu's Quit Siphon (siphon/tray.py)

function Get-TrayWindow([Diagnostics.Process] $process) {
    $hwnd = [SiphonSmoke.Win32]::FindWindowW($TrayClass, [IntPtr]::Zero)
    if ($hwnd -eq [IntPtr]::Zero) { throw 'Siphon has no tray window' }
    $owner = [uint32] 0
    [SiphonSmoke.Win32]::GetWindowThreadProcessId($hwnd, [ref] $owner) | Out-Null
    if ($owner -ne $process.Id) { throw "the tray window belongs to process $owner, not Siphon's $($process.Id)" }
    return $hwnd
}

function Close-ToTray([Diagnostics.Process] $process) {
    $process.CloseMainWindow() | Out-Null  # WM_CLOSE, as the window's X sends
    Start-Sleep -Seconds 3
    if ($process.HasExited) { throw "closing the window quit Siphon (exit code $($process.ExitCode))" }
    $process.Refresh()
    if ($process.MainWindowHandle -ne [IntPtr]::Zero) { throw 'the window still shows after it was closed' }
    Get-TrayWindow $process | Out-Null
    Write-Host '  ok: closing the window left Siphon running in the tray'
}

function Stop-SiphonFromTray([Diagnostics.Process] $process) {
    # WM_COMMAND with the menu's Quit Siphon: the app's own quit path, as a click on it takes
    $hwnd = Get-TrayWindow $process
    [SiphonSmoke.Win32]::PostMessageW($hwnd, 0x0111, [IntPtr] $QuitCommand, [IntPtr]::Zero) | Out-Null
    if (-not $process.WaitForExit(15000)) { $process.Kill(); throw 'Siphon did not quit from the tray menu' }
    $left = [SiphonSmoke.Win32]::FindWindowW($TrayClass, [IntPtr]::Zero)
    if ($left -ne [IntPtr]::Zero) { throw 'a tray window outlived Siphon' }
    Write-Host '  ok: Quit Siphon in the tray menu quit it'
}

function Wait-For([scriptblock] $condition, [int] $seconds, [string] $what) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if (& $condition) { return }
        Start-Sleep -Seconds 1
    }
    throw "no $what within $seconds s"
}

function Write-TestRelease([string] $path) {
    # A release in GitHub's JSON, newer than any real one, whose installer is the one this build made;
    # SIPHON_UPDATE_SOURCE (siphon/updater.py) makes Siphon read it instead of GitHub's latest release.
    $file = (Resolve-Path $Setup).Path
    @{
        tag_name = 'v99.0.0'; draft = $false; prerelease = $false
        html_url = 'https://github.com/GGRRK/siphon/releases/tag/v99.0.0'
        assets = @(@{
            name = 'Siphon-99.0.0-Setup.exe'; size = (Get-Item $file).Length
            digest = 'sha256:' + (Get-FileHash -Algorithm SHA256 $file).Hash.ToLower()
            browser_download_url = [Uri]::new($file, [UriKind]::Absolute).AbsoluteUri
        })
    } | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $path
    return [Uri]::new((Resolve-Path $path).Path, [UriKind]::Absolute).AbsoluteUri
}

function Assert-SilentSetup {
    # /VERYSILENT: no wizard, no message box - no visible window of the installer's processes at all
    $shown = @(Get-Process -Name 'Siphon-99.0.0-Setup*' -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
    if ($shown.Count) {
        Screenshot (Join-Path $Out 'setup-window.png')
        throw "the installer showed a window: '$($shown[0].MainWindowTitle)'"
    }
}

function Wait-Setup([string] $setupLog, [string] $copy) {
    Wait-For { Assert-SilentSetup; (Test-Path $setupLog) -and (Select-String -Quiet -SimpleMatch 'Log closed.' $setupLog) } 180 'finished silent install'
    Copy-Item $setupLog (Join-Path $Out $copy)
    Expect (Get-Content -Raw $setupLog) 'Installation process succeeded' 'the silent install succeeded, without a window'
}

# The portable zip, unpacked to a path with a space in it.
$root = Join-Path ([IO.Path]::GetTempPath()) 'Siphon smoke'
Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
Expand-Archive $Portable -DestinationPath $root
$cli = Join-Path $root 'Siphon\siphon-cli.exe'
$probe = Join-Path $root 'Siphon\bin\ffprobe.exe'
$music = [Environment]::GetFolderPath('MyMusic')
$installed = Join-Path $env:LOCALAPPDATA 'Programs\Siphon'
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Siphon.lnk'

Check 'versions' {
    $versions = Run $cli @('--version')
    Expect $versions '(?m)^Siphon \d+\.\d+\.\d+\s+yt-dlp 20\d\d\.' 'Siphon and yt-dlp versions'
    # read from yt-dlp's version module without importing yt-dlp (siphon/core.py); the selftest imports it
    $script:ytdlpRead = [regex]::Match($versions, 'yt-dlp (\S+)').Groups[1].Value
}

Check 'selftest --net' {
    $report = Run $cli @('selftest', '--net')
    Expect $report 'bundle\s+.*Siphon smoke' 'the bundle is the unpacked folder'
    Expect $report 'ok\s+js\s+quickjs \(.*Siphon smoke.*qjs\.exe\)' 'the bundled QuickJS runs'
    Expect $report 'ok\s+libmpv\s+client API 2\.' 'libmpv loads'
    Expect $report 'ok\s+playback' 'silent playback to the end'
    Expect $report 'ok\s+download\s+.*opus, 21\d\.\d s' 'a real YouTube download as Opus'
    Expect $report 'ok\s+challenge\s+.*solved by quickjs' 'QuickJS solved the YouTube challenge'
    Expect $report 'all checks passed' 'every selftest check'
    $imported = [regex]::Match($report, 'engine\s+yt-dlp (\S+) from').Groups[1].Value
    if ($imported -ne $script:ytdlpRead) { throw "--version read yt-dlp $script:ytdlpRead, yt-dlp says $imported" }
    Write-Host "  ok: the yt-dlp version read without importing it is the imported one's"
}

Check 'update check against GitHub' {
    Write-Host '> siphon-cli.exe update'
    $lines = & $cli update 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $lines | Write-Host
    $text = $lines -join "`n"
    if ($text -match "GitHub's rate limit") {  # 60 requests an hour per address, shared by the runners
        Write-Host "::warning::GitHub's API rate limit was reached, so the update check was not tried"
        return
    }
    if ($code -ne 0) { throw "siphon-cli update exited with $code" }
    # This build is no older than the latest release, and a portable copy never changes itself.
    Expect $text '(?m)^Siphon \d+\.\d+\.\d+ is (up to date \(latest release \d+\.\d+\.\d+\)|available: https://github\.com/GGRRK/siphon/releases/tag/v\d+\.\d+\.\d+)' 'the latest release, read and compared'
}

Check 'Spotify track as M4A' {
    # the programs Siphon runs (ffmpeg converting, ffprobe, QuickJS) start below normal priority (siphon/paths.py)
    $watch = Start-Job {
        while ($true) {
            Get-Process ffmpeg, ffprobe, qjs -ErrorAction SilentlyContinue |
                ForEach-Object { "$($_.ProcessName) $($_.PriorityClass)" }
            Start-Sleep -Milliseconds 50
        }
    }
    try {
        Run $cli @('get', 'https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT', '-f', 'm4a', '-o', $music) | Out-Null
    } finally {
        Stop-Job $watch
        $programs = @(Receive-Job $watch | Where-Object { $_ } | Sort-Object -Unique)
        Remove-Job $watch
    }
    Write-Host "  programs seen: $($programs -join ', ')"
    if (-not ($programs -match '^ffmpeg ')) { throw 'no ffmpeg seen converting' }
    $normal = @($programs | Where-Object { $_ -notmatch ' (BelowNormal)?$' })  # an empty class: it had just ended
    if ($normal.Count) { throw "not below normal priority: $($normal -join ', ')" }
    Write-Host '  ok: every program ran below normal priority'
    $song = Get-ChildItem $music -Filter *.m4a -Recurse | Select-Object -First 1
    if (-not $song) { throw "no .m4a in $music" }
    $info = Run $probe @('-v', 'error', '-show_entries', 'stream=codec_name:format=duration', '-of', 'csv=p=0', $song.FullName)
    Expect $info '(?m)^aac' "$($song.Name) holds AAC audio"
    if ([double](($info -split "`n")[-1]) -lt 60) { throw "$($song.Name) is shorter than a minute" }
}

if ($Engine) {
    Check 'a downloaded engine update is the one that loads' {
        $engineDir = Join-Path $env:LOCALAPPDATA 'Siphon\engine'
        Copy-Item -Recurse -Force $Engine $engineDir
        try {
            $report = Run $cli @('selftest')
            Expect $report 'engine\s+yt-dlp \S+ from .*engine\\yt_dlp-[^\\]+\.whl\\yt_dlp' 'yt-dlp imported from the wheel'
            $imported = [regex]::Match($report, 'engine\s+yt-dlp (\S+) from').Groups[1].Value
            Expect (Run $cli @('--version')) "yt-dlp $([regex]::Escape($imported))\s*$" 'the version read from the wheel without importing yt-dlp'
        } finally { Remove-Item -Recurse -Force $engineDir }
    }
}

Check 'installer (per user, silent)' {
    $arguments = '/VERYSILENT', '/CURRENTUSER', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$Out\setup.log`""
    $setup = Start-Process $Setup -ArgumentList $arguments -Wait -PassThru
    if ($setup.ExitCode -ne 0) { throw "setup exited with $($setup.ExitCode)" }
    foreach ($path in "$installed\Siphon.exe", "$installed\bin\libmpv-2.dll", $shortcut) {
        if (-not (Test-Path $path)) { throw "not installed: $path" }
    }
}

Check 'the installed window, the tray and a second start' {
    $log = Join-Path $env:LOCALAPPDATA 'Siphon\siphon.log'
    $app = Start-Process "$installed\Siphon.exe" -PassThru
    Start-Sleep -Seconds 12
    Screenshot (Join-Path $Out 'siphon-window.png')
    Start-Sleep -Seconds 3
    if ($app.HasExited) { throw "Siphon.exe exited with $($app.ExitCode) within 15 s" }
    Assert-SiphonWindow $app
    Write-Host '  ok: the Siphon window is up after 15 s'
    if (-not (Test-Path $log)) { throw "no log at $log" }
    Expect (Get-Content -Raw $log) 'siphon: tray icon added' 'Shell_NotifyIcon added the tray icon'
    Close-ToTray $app
    Wait-For { Select-String -Quiet -SimpleMatch 'siphon: tray balloon shown' $log } 10 'the first-close balloon'
    Screenshot (Join-Path $Out 'siphon-tray-balloon.png')
    # GLib's own single instance needs D-Bus, which Windows lacks: the running Siphon's tray window takes the
    # second start's command line, shows its window again, and the second one exits.
    $second = Start-Process "$installed\Siphon.exe" -PassThru
    if (-not $second.WaitForExit(20000)) { $second.Kill(); throw 'a second Siphon.exe kept running' }
    if ($second.ExitCode -ne 0) { throw "a second Siphon.exe exited with $($second.ExitCode)" }
    Wait-For { $app.Refresh(); $app.MainWindowTitle -eq 'Siphon' } 10 'the running window shown again by a second start'
    $running = @(Get-Process Siphon -ErrorAction SilentlyContinue).Count
    if ($running -ne 1) { throw "$running Siphon processes run" }
    Write-Host '  ok: a second start showed the running window and exited; one Siphon runs'
    Stop-SiphonFromTray $app
    Copy-Item $log $Out
    $text = Get-Content -Raw $log
    Expect $text 'siphon: tray icon removed' 'the icon was removed on quitting'
    if ($text -match 'Traceback') { Get-Content $log | Write-Host; throw 'a Python traceback in siphon.log' }
    Write-Host '  ok: no traceback in siphon.log'
}

Check 'the installed window in dark mode, with GTK debug output' {
    $log = Join-Path $env:LOCALAPPDATA 'Siphon\siphon.log'
    $personalize = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
    New-Item -Force $personalize | Out-Null
    Set-ItemProperty $personalize AppsUseLightTheme 0 -Type DWord
    $env:G_MESSAGES_DEBUG = 'all'  # GTK writes to the C runtime's stderr, which must land in the log too
    try {
        $app = Start-Process "$installed\Siphon.exe" -PassThru
        Start-Sleep -Seconds 12
        Screenshot (Join-Path $Out 'siphon-window-dark.png')
        if ($app.HasExited) { throw "Siphon.exe exited with $($app.ExitCode)" }
        Assert-SiphonWindow $app
        Stop-SiphonFromTray $app
    } finally {
        Remove-Item Env:G_MESSAGES_DEBUG
        Set-ItemProperty $personalize AppsUseLightTheme 1 -Type DWord
    }
    Copy-Item $log (Join-Path $Out 'siphon-debug.log')
    $text = Get-Content -Raw $log
    if ($text -match 'Traceback') { throw 'a Python traceback in siphon.log' }
    $debug = @(Get-Content $log | Select-String -SimpleMatch -- '-DEBUG').Count
    if (-not $debug) { throw 'GTK debug output did not reach siphon.log' }
    Write-Host "  ok: $debug GLib/GTK debug lines in siphon.log"
}

Check 'the session ending quits Siphon without installing a downloaded update' {
    $log = Join-Path $env:LOCALAPPDATA 'Siphon\siphon.log'
    $setupLog = Join-Path $env:LOCALAPPDATA 'Siphon\update\setup.log'
    Remove-Item $setupLog -ErrorAction SilentlyContinue
    $env:SIPHON_UPDATE_SOURCE = Write-TestRelease (Join-Path $Out 'release.json')
    try { $app = Start-Process "$installed\Siphon.exe" -PassThru } finally { Remove-Item Env:SIPHON_UPDATE_SOURCE }
    Wait-For { (Test-Path $log) -and (Select-String -Quiet -SimpleMatch 'Siphon 99.0.0 is ready' $log) } 90 "'Siphon 99.0.0 is ready' in siphon.log"
    # what Windows sends every top-level window when the session ends: may it end (TRUE), then it ends
    $tray = Get-TrayWindow $app
    $zero, $answer = [IntPtr]::Zero, [IntPtr]::Zero
    $hung = 2  # SMTO_ABORTIFHUNG
    [SiphonSmoke.Win32]::SendMessageTimeoutW($tray, 0x0011, $zero, $zero, $hung, 5000, [ref] $answer) | Out-Null
    if ($answer -ne [IntPtr] 1) { throw "WM_QUERYENDSESSION was answered $answer, not TRUE" }
    [SiphonSmoke.Win32]::SendMessageTimeoutW($tray, 0x0016, [IntPtr] 1, $zero, $hung, 10000, [ref] $answer) | Out-Null
    if (-not $app.WaitForExit(15000)) { $app.Kill(); throw 'Siphon did not quit when the session ended' }
    Start-Sleep -Seconds 10
    if (Test-Path $setupLog) { throw 'the installer ran while the session was ending' }
    Copy-Item $log (Join-Path $Out 'siphon-session-end.log')
    $text = Get-Content -Raw $log
    Expect $text 'siphon: tray icon removed' 'the icon was removed'
    if ($text -match 'Traceback') { throw 'a Python traceback in siphon.log' }
    Write-Host '  ok: Siphon quit as the session ended and left the update for the next quit'
}

Check 'a downloaded update installs when Siphon quits' {
    $log = Join-Path $env:LOCALAPPDATA 'Siphon\siphon.log'
    $setupLog = Join-Path $env:LOCALAPPDATA 'Siphon\update\setup.log'
    Remove-Item $setupLog -ErrorAction SilentlyContinue
    $env:SIPHON_UPDATE_SOURCE = Write-TestRelease (Join-Path $Out 'release.json')
    try { $app = Start-Process "$installed\Siphon.exe" -PassThru } finally { Remove-Item Env:SIPHON_UPDATE_SOURCE }
    Wait-For { (Test-Path $log) -and (Select-String -Quiet -SimpleMatch 'Siphon 99.0.0 is ready' $log) } 90 "'Siphon 99.0.0 is ready' in siphon.log"
    Write-Host '  ok: Siphon read the release at start, downloaded the installer and checked its sha256'
    Assert-SiphonWindow $app
    Close-ToTray $app  # closing the window is not quitting: nothing is installed yet
    if (Test-Path $setupLog) { throw 'the installer ran when the window was only closed' }
    Stop-SiphonFromTray $app
    Wait-Setup $setupLog 'update-on-quit-setup.log'
    Start-Sleep -Seconds 5
    if (Get-Process Siphon -ErrorAction SilentlyContinue) { throw 'Siphon started again, though it was quit rather than restarted' }
    Write-Host '  ok: Siphon stayed closed'
    Copy-Item $log (Join-Path $Out 'siphon-update.log')
    if ((Get-Content -Raw $log) -match 'Traceback') { throw 'a Python traceback in siphon.log' }
}

Check 'siphon-cli update installs a release and opens Siphon again' {
    $setupLog = Join-Path $env:LOCALAPPDATA 'Siphon\update\setup.log'
    Remove-Item $setupLog -ErrorAction SilentlyContinue
    $release = Join-Path $Out 'release.json'
    $env:SIPHON_UPDATE_SOURCE = Write-TestRelease $release
    try { $text = Run "$installed\siphon-cli.exe" @('update') } finally { Remove-Item Env:SIPHON_UPDATE_SOURCE }
    Remove-Item $release  # the reopened Siphon inherits the variable from the installer: it must not update again
    Expect $text 'Siphon 99\.0\.0 is being installed; it opens when the installer is done\.' 'the installed copy started the installer and exited'
    Wait-Setup $setupLog 'update-restart-setup.log'
    $app = $null
    for ($i = 0; $i -lt 60 -and -not $app; $i++) {
        Start-Sleep -Seconds 1
        $app = Get-Process Siphon -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq "$installed\Siphon.exe" } | Select-Object -First 1
    }
    if (-not $app) { throw 'the installer did not open Siphon again within 60 s' }
    Start-Sleep -Seconds 10
    Screenshot (Join-Path $Out 'siphon-after-update.png')
    Assert-SiphonWindow $app
    Write-Host '  ok: the installer opened Siphon again'
    Stop-SiphonFromTray $app
}

Check 'uninstaller' {
    Start-Process "$installed\unins000.exe" -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait
    for ($i = 0; $i -lt 30 -and (Test-Path "$installed\Siphon.exe"); $i++) { Start-Sleep -Seconds 1 }
    if ((Test-Path "$installed\Siphon.exe") -or (Test-Path $shortcut)) { throw 'the uninstaller left Siphon behind' }
}

if ($failed.Count) { throw "$($failed.Count) smoke check(s) failed: $($failed -join ', ')" }
Write-Host "`nALL SMOKE CHECKS PASSED"
