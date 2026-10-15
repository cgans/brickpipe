# Checks that this Windows machine has everything needed to test brickpipe
# end to end, and prints the fix for anything missing.
#
# Run from the brickpipe folder:
#   powershell -ExecutionPolicy Bypass -File scripts\check-setup.ps1
#
# It only reads; nothing is installed or changed.

$ErrorActionPreference = "Continue"
$script:failed = 0
$script:warned = 0

function Pass($name, $detail) {
    Write-Host "[ OK ] $name" -ForegroundColor Green -NoNewline
    Write-Host "  $detail"
}

function Fail($name, $detail, $fix) {
    $script:failed++
    Write-Host "[FAIL] $name" -ForegroundColor Red -NoNewline
    Write-Host "  $detail"
    foreach ($line in $fix) { Write-Host "         $line" }
}

function Warn($name, $detail, $fix) {
    $script:warned++
    Write-Host "[WARN] $name" -ForegroundColor Yellow -NoNewline
    Write-Host "  $detail"
    foreach ($line in $fix) { Write-Host "         $line" }
}

function Run($exe, $arguments) {
    # Returns the command's output as one string, or $null if it cannot run.
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $null }
    try {
        $out = & $exe @arguments 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { return $null }
        return $out.Trim()
    } catch { return $null }
}

Write-Host ""
Write-Host "brickpipe setup check"
Write-Host "---------------------"

# --- Needed for brickpipe itself ------------------------------------------

$git = Run "git" @("--version")
if ($git) { Pass "Git" $git }
else {
    Fail "Git" "not found" @(
        "Install:  winget install --id Git.Git -e",
        "Then close and reopen PowerShell.")
}

$python = $null
$pythonCmd = $null
foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
    $exe = $candidate[0]
    $pre = @($candidate | Select-Object -Skip 1)
    $out = Run $exe ($pre + @("--version"))
    if ($out -match "Python (\d+)\.(\d+)") {
        $python = $out
        $pythonCmd = ($candidate -join " ")
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        break
    }
}
if (-not $python) {
    Fail "Python" "not found (the Microsoft Store placeholder does not count)" @(
        "Install:  winget install --id Python.Python.3.13 -e",
        "Then close and reopen PowerShell.")
} elseif ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Fail "Python" "$python is too old; 3.10 or newer is needed" @(
        "Install:  winget install --id Python.Python.3.13 -e")
} else {
    Pass "Python" "$python  (run it as: $pythonCmd)"
}

if (Test-Path "brickpipe\cli.py") { Pass "brickpipe folder" (Get-Location).Path }
else {
    Fail "brickpipe folder" "this is not the brickpipe folder" @(
        "git clone https://github.com/cgans/brickpipe.git",
        "cd brickpipe")
}

if ($env:REBRICKABLE_API_KEY) { Pass "Rebrickable key" "set for this window" }
else {
    Warn "Rebrickable key" "not set; needed only for --availability" @(
        'Set it for this window:  $env:REBRICKABLE_API_KEY = "your-key"',
        "Free key: rebrickable.com > Account > Settings > API")
}

# --- Needed to run LDraw Nova -----------------------------------------------

$docker = Run "docker" @("--version")
if (-not $docker) {
    Fail "Docker" "not found" @(
        "Install:  winget install --id Docker.DockerDesktop -e",
        "Restart Windows, start Docker Desktop, and accept the WSL 2 prompt.",
        "If it asks for WSL:  wsl --install   (in an administrator PowerShell)")
} else {
    Pass "Docker" $docker
    if (Run "docker" @("info", "--format", "{{.ServerVersion}}")) {
        Pass "Docker engine" "running"
    } else {
        Fail "Docker engine" "installed but not running" @(
            "Start Docker Desktop from the Start menu and wait for 'Engine running'.",
            "If it will not start, enable virtualization in the BIOS and run:  wsl --update")
    }
    $compose = Run "docker" @("compose", "version")
    if ($compose) { Pass "Docker Compose" $compose }
    else {
        Fail "Docker Compose" "not available" @(
            "Update Docker Desktop; Compose is included in current versions.")
    }
}

$novaBuilt = $false
try { $novaBuilt = [bool](docker image inspect ldraw-nova-app 2>$null) } catch {}
try {
    $drive = (Get-Item (Get-Location).Path).PSDrive
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($novaBuilt) { Pass "Disk space" "$freeGb GB free on $($drive.Name): (Nova's image is already built)" }
    elseif ($freeGb -ge 20) { Pass "Disk space" "$freeGb GB free on $($drive.Name):" }
    elseif ($freeGb -ge 10) {
        Warn "Disk space" "$freeGb GB free on $($drive.Name): is tight; Nova's image needs about 5 GB" @(
            "Free some space, or run  docker system prune  to clear old images.")
    } else {
        Fail "Disk space" "$freeGb GB free on $($drive.Name): is not enough to build Nova's image" @(
            "Free about 20 GB before building; the build needs room for the image twice over.")
    }
} catch { Warn "Disk space" "could not be measured" @() }

foreach ($port in @(8765, 8443)) {
    $busy = $null
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    }
    if ($busy) {
        Warn "Port $port" "already in use (fine if Nova is already running)" @(
            "Otherwise pick another port: create ldraw-nova-docker\.env containing",
            "  LDRAW_NOVA_WEB_PORT=8766")
    } else { Pass "Port $port" "free" }
}

$parent = Split-Path -Parent (Get-Location).Path
$nova = Join-Path $parent "ldraw-nova"
$novaDocker = Join-Path $parent "ldraw-nova-docker"
$cloneFix = @(
    "From the folder that contains brickpipe ($parent):",
    "  git clone -c core.autocrlf=false --branch v0.6.0 https://github.com/anteloc/ldraw-nova.git",
    "  git clone -c core.autocrlf=false --branch v0.6.0 https://github.com/anteloc/ldraw-nova-docker.git")

if ((Test-Path $nova) -and (Test-Path $novaDocker)) {
    Pass "Nova folders" "found next to brickpipe"

    $db = Join-Path $nova "data\ldraw-info.db"
    if (Test-Path $db) { Pass "Nova database" $db }
    else { Fail "Nova database" "missing: $db" $cloneFix }

    # Git on Windows converts line endings by default, which breaks the shell
    # scripts Docker copies into the Linux image.
    $entry = Join-Path $novaDocker "entrypoint.sh"
    if (Test-Path $entry) {
        $bytes = [System.IO.File]::ReadAllBytes($entry)
        if ($bytes -contains 13) {
            Fail "Nova line endings" "entrypoint.sh has Windows line endings; the container will not start" @(
                "Delete both Nova folders and clone again with the setting that keeps them:")
            foreach ($line in $cloneFix) { Write-Host "         $line" }
        } else { Pass "Nova line endings" "Unix line endings kept" }
    }
} else {
    Fail "Nova folders" "ldraw-nova and ldraw-nova-docker are not next to brickpipe" $cloneFix
}

# --- Needed for the stud connectivity check ----------------------------------

$ldraw = $null
if ($env:LDRAW_DIR -and (Test-Path $env:LDRAW_DIR)) { $ldraw = $env:LDRAW_DIR }
if (-not $ldraw) {
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\brickpipe\ldraw\complete.zip",
        "$env:LOCALAPPDATA\LPub3D Software\LPub3D\libraries\complete.zip",
        (Get-ChildItem -Path "$env:USERPROFILE\Desktop", "$env:USERPROFILE\Downloads" -Directory -Filter "LPub3D*" -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName "libraries\complete.zip" } | Select-Object -First 1),
        "$env:USERPROFILE\ldraw", "$env:USERPROFILE\LDraw", "C:\LDraw",
        "C:\Users\Public\Documents\LDraw", (Join-Path $parent "ldraw"))) {
        if ($candidate -and (Test-Path $candidate)) { $ldraw = $candidate; break }
    }
}
if ($ldraw) { Pass "LDraw part library" $ldraw }
else {
    Warn "LDraw part library" "not found; needed for the check that pieces clip together" @(
        "Get it with:  py -3 -m brickpipe --get-ldraw   (about 150 MB, from ldraw.org)",
        "or press 'Download the library' in the page's settings.",
        "Already have one elsewhere? Set `$env:LDRAW_DIR to the folder or the zip.")
}

$shadow = Join-Path $nova "data\offLibShadow\parts"
if ((Test-Path $nova) -and (Test-Path $shadow)) { Pass "Nova shadow library" "found (connector data)" }
elseif (Test-Path $nova) { Warn "Nova shadow library" "missing from the Nova clone; connectivity cannot be checked" $cloneFix }

# --- Needed to view the results ---------------------------------------------

# LPub3D for Windows ships as a portable zip, so it can live anywhere. Look
# in the usual places, then one level deep under the home, Desktop, Downloads
# and Program Files folders.
$lpub = $null
if ($env:LPUB3D -and (Test-Path $env:LPUB3D)) { $lpub = $env:LPUB3D }
if (-not $lpub) {
    $roots = @($env:USERPROFILE, "$env:USERPROFILE\Desktop", "$env:USERPROFILE\Downloads",
               $env:ProgramFiles, ${env:ProgramFiles(x86)}, "$env:LOCALAPPDATA\Programs") |
             Where-Object { $_ -and (Test-Path $_) }
    foreach ($root in $roots) {
        $hit = Get-ChildItem -Path $root -Directory -Filter "LPub3D*" -ErrorAction SilentlyContinue |
               ForEach-Object { Get-ChildItem -Path $_.FullName -Filter "LPub3D.exe" -Recurse -Depth 1 -ErrorAction SilentlyContinue } |
               Select-Object -First 1
        if ($hit) { $lpub = $hit.FullName; break }
    }
}
if (-not $lpub) {
    $cmd = Get-Command "LPub3D" -ErrorAction SilentlyContinue
    if ($cmd) { $lpub = $cmd.Source }
}
if ($lpub) { Pass "LPub3D" $lpub }
else {
    Warn "LPub3D" "not found; needed only to make instruction pages" @(
        "Download the 64-bit Windows zip (LPub3D_x86_64-...zip) from:",
        "  https://github.com/trevorsandy/lpub3d/releases/latest",
        "Extract it to a folder named LPub3D on your Desktop and run LPub3D.exe from there.",
        "Extracted it somewhere else? Set `$env:LPUB3D to the full path of LPub3D.exe.")
}

Write-Host ""
if ($script:failed -eq 0 -and $script:warned -eq 0) {
    Write-Host "Everything is in place." -ForegroundColor Green
} elseif ($script:failed -eq 0) {
    Write-Host "Ready, with $($script:warned) optional item(s) to look at." -ForegroundColor Yellow
} else {
    Write-Host "$($script:failed) item(s) need fixing before the full test will work." -ForegroundColor Red
}
Write-Host ""
exit $script:failed
