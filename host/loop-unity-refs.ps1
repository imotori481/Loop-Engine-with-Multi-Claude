# ---------------------------------------------------------------
#  loop-unity-refs -- send the assemblies a Unity project compiles
#  against to the sandbox, so the loop can build its C# there.
#
#    loop-unity-refs <project> <unity-project-dir>
#
#  The list comes from Assembly-CSharp.csproj, which Unity writes
#  for the external code editor. It names every DLL Unity itself
#  compiles the project's scripts against, and the symbols it
#  defines. Guessing the list from the Editor install would miss
#  the packages (Input System, Netcode, ...) or pick up the wrong
#  ones.
#
#  Left out on purpose:
#    - native DLLs: they are not .NET assemblies and break the build
#    - NetStandard facades: the .NET SDK brings its own netstandard
#    - Assembly-CSharp and its editor twins: the loop compiles that code
#      itself (Assembly-CSharp-firstpass, i.e. Assets/Plugins, is kept)
#    - UNITY_EDITOR* symbols: the loop builds the player's code
#
#  The sandbox keeps them under /srv/loop/projects/<project>/unity-refs
#  (loop project unity-refs), owned by root and frozen.
#
#  ASCII only: PowerShell 5.1 reads a BOM-less UTF-8 file as ANSI.
# ---------------------------------------------------------------
param(
  [Parameter(Mandatory = $true, Position = 0)][string]$Name,
  [Parameter(Mandatory = $true, Position = 1)][string]$UnityProject,
  # Build the tar and stop; nothing is sent. For checking what would go.
  [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$Distro = "Ubuntu-24.04"
$SshHost = "loop-dev"
$Port = 2222

$projectDir = (Resolve-Path $UnityProject).Path
$csproj = Join-Path $projectDir "Assembly-CSharp.csproj"
if (-not (Test-Path $csproj)) {
  Write-Host "ERROR: $csproj is missing."
  Write-Host "  Open the project in Unity once, set Visual Studio or Rider as the"
  Write-Host "  external script editor (Preferences > External Tools), and run"
  Write-Host "  Assets > Open C# Project. Unity writes the file then."
  exit 1
}
$versionFile = Join-Path $projectDir "ProjectSettings\ProjectVersion.txt"
$match = Select-String -Path $versionFile -Pattern '^m_EditorVersion:\s*(\S+)'
if (-not $match) { Write-Host "ERROR: no m_EditorVersion in $versionFile"; exit 1 }
$version = $match.Matches[0].Groups[1].Value

[xml]$xml = Get-Content -Raw -Path $csproj

# ---- which DLLs ---------------------------------------------------
$paths = New-Object System.Collections.Generic.List[string]
foreach ($node in $xml.SelectNodes("//*[local-name()='HintPath']")) {
  $p = $node.InnerText.Trim()
  if (-not [IO.Path]::IsPathRooted($p)) { $p = Join-Path $projectDir $p }
  $paths.Add($p)
}
# Packages and asmdefs appear as project references; their compiled
# form is in Library\ScriptAssemblies under the same name.
$scriptAssemblies = Join-Path $projectDir "Library\ScriptAssemblies"
foreach ($node in $xml.SelectNodes("//*[local-name()='ProjectReference']")) {
  $assembly = [IO.Path]::GetFileNameWithoutExtension($node.GetAttribute("Include"))
  $dll = Join-Path $scriptAssemblies "$assembly.dll"
  if (Test-Path $dll) { $paths.Add($dll) }
  else { Write-Host "WARN: no $dll for the project reference $assembly" }
}

$stage = Join-Path $env:TEMP ("loop-unity-refs-" + [guid]::NewGuid().ToString("N"))
$refs = Join-Path $stage "refs"
New-Item -ItemType Directory -Path $refs | Out-Null
$seen = @{}
$sources = New-Object System.Collections.Generic.List[string]
$skipped = 0
try {
  foreach ($p in ($paths | Select-Object -Unique)) {
    $leaf = Split-Path $p -Leaf
    if (-not (Test-Path $p)) { Write-Host "WARN: missing $p"; continue }
    if ($p -match '[\\/]NetStandard[\\/]') { $skipped++; continue }
    # Only the assembly the loop compiles itself, and the editor ones. Keep
    # Assembly-CSharp-firstpass: it is Assets/Plugins (DOTween's modules,
    # for one), which the project's code calls and the loop never builds.
    if ($leaf -match '^Assembly-CSharp(-Editor.*)?\.dll$') { $skipped++; continue }
    if ($leaf -notmatch '^[A-Za-z0-9._+-]+\.dll$') { Write-Host "WARN: odd name, skipped: $p"; continue }
    try { [Reflection.AssemblyName]::GetAssemblyName($p) | Out-Null }
    catch { $skipped++; continue }   # native, not an assembly
    $key = $leaf.ToLowerInvariant()
    if ($seen.ContainsKey($key)) {
      if ((Get-FileHash $p).Hash -ne (Get-FileHash $seen[$key]).Hash) {
        Write-Host "WARN: two different $leaf; kept $($seen[$key]), skipped $p"
      }
      continue
    }
    Copy-Item $p (Join-Path $refs $leaf)
    $seen[$key] = $p
    $sources.Add("$leaf`t$p")
  }
  if ($seen.Count -eq 0) { Write-Host "ERROR: no assemblies found in $csproj"; exit 1 }

  # ---- symbols and language version -------------------------------
  $defines = @()
  $node = $xml.SelectSingleNode("//*[local-name()='DefineConstants']")
  if ($node) {
    $defines = $node.InnerText.Split(';') | ForEach-Object { $_.Trim() } |
      Where-Object { $_ -and $_ -notmatch '^UNITY_EDITOR' }
  }
  $node = $xml.SelectSingleNode("//*[local-name()='LangVersion']")
  $langVersion = if ($node) { $node.InnerText.Trim() } else { "9.0" }

  $lf = "`n"
  [IO.File]::WriteAllText((Join-Path $stage "version.txt"), $version + $lf)
  [IO.File]::WriteAllText((Join-Path $stage "defines.txt"), (($defines -join $lf) + $lf))
  [IO.File]::WriteAllText((Join-Path $stage "langversion.txt"), $langVersion + $lf)
  [IO.File]::WriteAllText((Join-Path $stage "sources.txt"), (($sources -join $lf) + $lf))

  Write-Host "Unity $version, C# $langVersion, $($seen.Count) assemblies ($skipped left out), $($defines.Count) symbols"

  $tarPath = Join-Path $env:TEMP "loop-unity-refs.tar"
  if (Test-Path $tarPath) { Remove-Item $tarPath }
  # Windows's own tar by full path. With Git on PATH, `tar` can be GNU tar,
  # which reads "C:\..." as host "C" and fails.
  & "$env:SystemRoot\System32\tar.exe" -cf $tarPath -C $stage refs version.txt defines.txt langversion.txt sources.txt
  if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: tar failed"; exit 1 }
  if ($DryRun) { Write-Host "dry run: $tarPath was built and not sent"; exit 0 }

  # ---- to the sandbox -----------------------------------------------
  # Start the distro and wait for sshd, the same way loop.cmd does. Done
  # here rather than through a `loop` command, which would ask for the
  # sudo password once more for nothing.
  & wsl.exe -d $Distro -u root --exec /usr/bin/true
  if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: failed to start distro $Distro"; exit 1 }
  $up = $false
  for ($i = 0; $i -lt 40 -and -not $up; $i++) {
    $up = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
    if (-not $up) { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { Write-Host "ERROR: sshd did not come up within 20 seconds"; exit 1 }
  & scp -q $tarPath "${SshHost}:loop-unity-refs.tar"
  if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: could not copy $tarPath to the sandbox"; exit 1 }
  # The remote shell expands ~ to the maintenance user's home before sudo.
  & "$PSScriptRoot\loop.cmd" project unity-refs $Name "~/loop-unity-refs.tar"
  if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: loop project unity-refs failed"; exit 1 }
  Remove-Item $tarPath
}
finally {
  Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
