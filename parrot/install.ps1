# Parrot for Windows installer - push-to-talk local dictation (hold Ctrl+Win).
# Usage (PowerShell):  irm https://tools.myinthermo.com/parrot/install.ps1 | iex
$ErrorActionPreference = "Stop"

Write-Host "== Parrot for Windows: push-to-talk dictation (hold Ctrl+Win) =="

# --- 1. Python 3.12 ---
$havePy = $false
try {
    & py -3.12 -c "pass" 2>$null
    if ($LASTEXITCODE -eq 0) { $havePy = $true }
} catch {}

if (-not $havePy) {
    Write-Host "Installing Python 3.12 via winget (this can take a few minutes)..."
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    # Pick up the fresh PATH so the py launcher is visible in this session.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    & py -3.12 -c "pass"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 installed but 'py -3.12' not found - close this PowerShell window, open a new one, and run the install command again."
    }
}

# --- 2. Download parrot.py ---
$dir = Join-Path $env:USERPROFILE "Parrot"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Invoke-WebRequest -Uri "https://tools.myinthermo.com/parrot/parrot.py" -OutFile (Join-Path $dir "parrot.py")
Write-Host "Downloaded parrot.py to $dir"

# --- 3. Dependencies ---
Write-Host "Installing Python packages (faster-whisper etc.)..."
& py -3.12 -m pip install --quiet --disable-pip-version-check faster-whisper sounddevice keyboard numpy
if ($LASTEXITCODE -ne 0) { throw "pip install failed - see output above." }

# --- 4. Start hidden at logon (Startup folder, no admin needed) ---
$pyexe = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
$pythonw = $pyexe -replace "python\.exe$", "pythonw.exe"
$script = Join-Path $dir "parrot.py"
$startup = [Environment]::GetFolderPath("Startup")
$vbsLine = 'CreateObject("WScript.Shell").Run """' + $pythonw + '"" ""' + $script + '""", 0, False'
Set-Content -Path (Join-Path $startup "Parrot.vbs") -Value $vbsLine -Encoding ASCII
Write-Host "Added Parrot to Startup (runs hidden at every logon)."

# --- 5. Launch now ---
Start-Process -FilePath $pythonw -ArgumentList "`"$script`""

Write-Host ""
Write-Host "Parrot is starting. FIRST RUN downloads the speech model (~250 MB),"
Write-Host "so give it a few minutes. When you hear TWO RISING BEEPS it is ready."
Write-Host ""
Write-Host "Use: click into any text box, HOLD Ctrl+Win, speak, release."
Write-Host "High beep = recording, low beep = processing, text appears at the cursor."
Write-Host "Log: $dir\parrot.log   Uninstall: delete $startup\Parrot.vbs and $dir"
