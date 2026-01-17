<#
.SYNOPSIS
  Watch HDC devices and auto reconnect.

.DESCRIPTION
  - Reads the device list from config/devices.json (JSON list).
  - Every N seconds checks `hdc list targets -v`.
  - If a device is not connected, tries `hdc tconn <ip:port>`.
  - If still not connected, scans other candidate ports on the known device IP (default includes 5555/8710), then scans LAN candidate ports and remaps by UDID.
  - Updates config/devices.json runtime fields: online/last_online_at/last_refresh_at + capped changes history.

.EXAMPLE
  pwsh .\scripts\watch-hdc-devices.ps1

.EXAMPLE
  pwsh .\scripts\watch-hdc-devices.ps1 -Once
#>

[CmdletBinding()]
param(
  [Parameter()]
  [string]$DevicesJsonPath = (Join-Path $PSScriptRoot '..\config\devices.json'),

  [Parameter()]
  [int]$IntervalSeconds = 10,

  [Parameter()]
  [string]$StatePath = (Join-Path $PSScriptRoot '..\.hdc-devices.state.json'),

  [Parameter()]
  [int]$ScanTimeoutMs = 800,

  [Parameter()]
  [int]$ScanThrottle = 128,

  [Parameter()]
  [int]$MaxScanHosts = 1024,

  [Parameter()]
  [switch]$EnableLanScan = $true,

  [Parameter()]
  [switch]$EnableIpPortScan = $true,

  [Parameter()]
  [int[]]$ExtraPorts = @(5555, 8710),

  [Parameter()]
  [switch]$NoWriteConfig = $false,

  [Parameter()]
  [switch]$Once = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log {
  param(
    [Parameter(Mandatory)]
    [ValidateSet('INFO', 'WARN', 'ERROR')]
    [string]$Level,

    [Parameter(Mandatory)]
    [string]$Message
  )
  $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  Write-Host "[$ts][$Level] $Message"
}

function Get-NowIso {
  return (Get-Date).ToString('o')
}

function Get-ObjField {
  param(
    [Parameter(Mandatory)]$Obj,
    [Parameter(Mandatory)][string]$Name
  )
  if ($null -eq $Obj) { return $null }
  if ($Obj -is [System.Collections.IDictionary]) {
    if ($Obj.Contains($Name)) { return $Obj[$Name] }
    return $null
  }
  $p = $Obj.PSObject.Properties[$Name]
  if ($p) { return $p.Value }
  return $null
}

function Set-ObjField {
  param(
    [Parameter(Mandatory)]$Obj,
    [Parameter(Mandatory)][string]$Name,
    [Parameter()]$Value
  )
  if ($Obj -is [System.Collections.IDictionary]) {
    $Obj[$Name] = $Value
    return
  }
  $Obj | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}

function Normalize-Online {
  param([Parameter()]$Value)
  if ($null -eq $Value) { return $null }
  if ($Value -is [bool]) { return $Value }
  if ($Value -is [string]) {
    $v = $Value.Trim().ToLowerInvariant()
    if ($v -in @('online', 'on', 'true', '1', 'yes', 'y')) { return $true }
    if ($v -in @('offline', 'off', 'false', '0', 'no', 'n')) { return $false }
  }
  return $null
}

function Split-IpPort {
  param([Parameter(Mandatory)][string]$ConnectKey)
  if ($ConnectKey -match '^(?<ip>\d{1,3}(?:\.\d{1,3}){3}):(?<port>\d{1,5})$') {
    $ip = $Matches['ip']
    $port = [int]$Matches['port']
    if ($port -lt 1 -or $port -gt 65535) { return [pscustomobject]@{ Ip = $ip; Port = $null } }
    return [pscustomobject]@{ Ip = $ip; Port = $port }
  }
  if ($ConnectKey -match '^(?<ip>\d{1,3}(?:\.\d{1,3}){3}):') {
    return [pscustomobject]@{ Ip = $Matches['ip']; Port = $null }
  }
  return [pscustomobject]@{ Ip = $null; Port = $null }
}

function Append-DeviceChange {
  param(
    [Parameter(Mandatory)]$Device,
    [Parameter(Mandatory)][hashtable]$Event,
    [Parameter()][int]$MaxItems = 3
  )
  $existing = Get-ObjField -Obj $Device -Name 'changes'
  $list = @()
  if ($existing -and ($existing -is [System.Collections.IEnumerable]) -and -not ($existing -is [string])) {
    foreach ($ev in $existing) {
      if ($ev -is [System.Collections.IDictionary] -or $ev -is [pscustomobject]) {
        $list += $ev
      }
    }
  }
  $list += $Event
  $list = @($list | Select-Object -Last $MaxItems)
  Set-ObjField -Obj $Device -Name 'changes' -Value $list
}

function Normalize-DeviceForWrite {
  param(
    [Parameter(Mandatory)]$Device,
    [Parameter()][int]$MaxChanges = 3
  )

  $ordered = [ordered]@{}
  $changesValue = $null

  if ($Device -is [System.Collections.IDictionary]) {
    foreach ($k in $Device.Keys) {
      if ("$k" -eq 'changes') {
        $changesValue = $Device[$k]
      } else {
        $ordered["$k"] = $Device[$k]
      }
    }
  } else {
    foreach ($p in $Device.PSObject.Properties) {
      if ($p.Name -eq 'changes') {
        $changesValue = $p.Value
      } else {
        $ordered[$p.Name] = $p.Value
      }
    }
  }

  $list = @()
  if ($changesValue -and ($changesValue -is [System.Collections.IEnumerable]) -and -not ($changesValue -is [string])) {
    foreach ($ev in $changesValue) {
      if ($ev -is [System.Collections.IDictionary] -or $ev -is [pscustomobject]) {
        $list += $ev
      }
    }
  }
  $list = @($list | Select-Object -Last $MaxChanges)
  $ordered['changes'] = $list

  return [pscustomobject]$ordered
}

function Record-DeviceEndpointChange {
  param(
    [Parameter(Mandatory)]$Device,
    [Parameter(Mandatory)][string]$Old,
    [Parameter(Mandatory)][string]$New,
    [Parameter(Mandatory)][string]$At,
    [Parameter(Mandatory)][string]$Reason
  )
  if ($Old -eq $New) { return }
  $o = Split-IpPort -ConnectKey $Old
  $n = Split-IpPort -ConnectKey $New
  $what = 'endpoint'
  if ($o.Ip -ne $n.Ip -and $o.Port -ne $n.Port) { $what = 'ip+port' }
  elseif ($o.Ip -ne $n.Ip) { $what = 'ip' }
  elseif ($o.Port -ne $n.Port) { $what = 'port' }

  Append-DeviceChange -Device $Device -Event @{
    at     = $At
    kind   = 'endpoint_change'
    what   = $what
    from   = $Old
    to     = $New
    reason = $Reason
  }
}

function Record-DeviceStatusChange {
  param(
    [Parameter(Mandatory)]$Device,
    [Parameter()]$PrevOnline,
    [Parameter(Mandatory)][bool]$NowOnline,
    [Parameter(Mandatory)][string]$At,
    [Parameter(Mandatory)][string]$Reason
  )
  $prev = Normalize-Online -Value $PrevOnline
  if ($null -eq $prev -or $prev -eq $NowOnline) { return }

  Append-DeviceChange -Device $Device -Event @{
    at        = $At
    kind      = if ($NowOnline) { 'online' } else { 'offline' }
    reason    = $Reason
    device_id = [string](Get-ObjField -Obj $Device -Name 'device_id')
  }
}

function Get-CandidatePorts {
  param(
    [Parameter(Mandatory)]$Devices,
    [Parameter(Mandatory)]$State,
    [Parameter(Mandatory)][int[]]$ExtraPorts
  )

  $ports = New-Object System.Collections.Generic.HashSet[int]
  foreach ($p in $ExtraPorts) {
    if ($p -gt 0 -and $p -le 65535) { $null = $ports.Add([int]$p) }
  }

  foreach ($d in $Devices) {
    $did = [string](Get-ObjField -Obj $d -Name 'device_id')
    if ($did -match ':(\d+)$') { $null = $ports.Add([int]$Matches[1]) }
  }

  foreach ($entry in ($State.devices.Values | Where-Object { $_ })) {
    if (-not ($entry -is [System.Collections.IDictionary])) { continue }
    if (-not $entry.Contains('device_id')) { continue }
    $did = [string]$entry['device_id']
    if ($did -match ':(\d+)$') { $null = $ports.Add([int]$Matches[1]) }
  }

  foreach ($d in $Devices) {
    $changes = Get-ObjField -Obj $d -Name 'changes'
    if (-not $changes) { continue }
    if (($changes -is [string]) -or -not ($changes -is [System.Collections.IEnumerable])) { continue }
    foreach ($ev in $changes) {
      foreach ($k in @('from', 'to', 'device_id')) {
        $v = Get-ObjField -Obj $ev -Name $k
        if (-not $v) { continue }
        $vStr = [string]$v
        if ($vStr -match ':(\d+)$') { $null = $ports.Add([int]$Matches[1]) }
      }
    }
  }

  if ($ports.Count -eq 0) { $null = $ports.Add(5555) }
  return @($ports | Sort-Object)
}

function Find-OpenTcpPortsOnHost {
  param(
    [Parameter(Mandatory)][string]$IpAddress,
    [Parameter(Mandatory)][int[]]$Ports,
    [Parameter(Mandatory)][int]$TimeoutMs,
    [Parameter(Mandatory)][int]$Throttle
  )
  if (-not $Ports -or $Ports.Count -eq 0) { return @() }

  if ($PSVersionTable.PSVersion.Major -ge 7) {
    $open = $Ports | ForEach-Object -Parallel {
      $port = $_
      $client = $null
      try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $ar = $client.BeginConnect($using:IpAddress, [int]$port, $null, $null)
        if ($ar.AsyncWaitHandle.WaitOne($using:TimeoutMs, $false)) {
          try { $client.EndConnect($ar) } catch { }
          if ($client.Connected) { return [int]$port }
        }
      } catch { }
      finally {
        if ($client) { $client.Close() }
      }
    } -ThrottleLimit $Throttle

    return @($open | Where-Object { $_ } | Sort-Object -Unique)
  }

  $open = foreach ($port in $Ports) {
    $client = $null
    try {
      $client = [System.Net.Sockets.TcpClient]::new()
      $ar = $client.BeginConnect($IpAddress, [int]$port, $null, $null)
      if ($ar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
        try { $client.EndConnect($ar) } catch { }
        if ($client.Connected) { $port }
      }
    } catch { }
    finally {
      if ($client) { $client.Close() }
    }
  }
  return @($open | Where-Object { $_ } | Sort-Object -Unique)
}

function Assert-HdcAvailable {
  if (-not (Get-Command hdc -ErrorAction SilentlyContinue)) {
    throw "hdc not found in PATH. Please install/configure HDC first."
  }
}

function Get-DevicesFromJson {
  param([Parameter(Mandatory)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    throw "Devices json not found: $Path"
  }
  $raw = Get-Content -LiteralPath $Path -Raw
  $devices = ($raw | ConvertFrom-Json)
  # ConvertFrom-Json may unwrap single-item arrays into a single PSCustomObject.
  return @($devices)
}

function Save-DevicesJson {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)]$Devices
  )
  $dir = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
  }
  $leaf = Split-Path -Leaf $Path
  $tmp = Join-Path $dir ".$leaf.tmp"
  $normalized = @($Devices | ForEach-Object { Normalize-DeviceForWrite -Device $_ -MaxChanges 3 })
  (ConvertTo-Json -InputObject @($normalized) -Depth 10) | Set-Content -LiteralPath $tmp -Encoding UTF8
  Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Get-HdcTargetsVerbose {
  $lines = @(& hdc list targets -v 2>&1)
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to run 'hdc list targets -v': $($lines -join "`n")"
  }

  $targets = foreach ($line in $lines) {
    $trim = ($line | ForEach-Object { "$_".Trim() })
    if ([string]::IsNullOrWhiteSpace($trim)) { continue }

    $parts = $trim -split '\s+'
    if ($parts.Count -lt 3) { continue }

    [pscustomobject]@{
      ConnectKey = $parts[0]
      Transport  = $parts[1]
      Status     = $parts[2]
    }
  }
  return ,$targets
}

function Get-ConnectedTargetsSet {
  param([Parameter(Mandatory)][object[]]$Targets)
  $set = @{}
  foreach ($t in $Targets) {
    if ($t.Status -eq 'Connected') {
      $set[$t.ConnectKey] = $true
    }
  }
  return $set
}

function Try-HdcConnect {
  param([Parameter(Mandatory)][string]$Target)

  $out = @(& hdc tconn $Target 2>&1)
  $exit = $LASTEXITCODE
  return [pscustomobject]@{
    Target   = $Target
    ExitCode = $exit
    Output   = ($out -join "`n")
  }
}

function Get-HdcUdid {
  param([Parameter(Mandatory)][string]$Target)

  $out = @(& hdc -t $Target shell bm get --udid 2>&1)
  if ($LASTEXITCODE -ne 0) { return $null }

  $text = ($out -join "`n")
  $m = [regex]::Match($text, '([A-F0-9]{64})', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
  if (-not $m.Success) { return $null }
  return $m.Groups[1].Value.ToUpperInvariant()
}

function Load-State {
  param([Parameter(Mandatory)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return @{
      updatedAt = (Get-Date).ToString('o')
      devices   = @{}
    }
  }
  try {
    $raw = Get-Content -LiteralPath $Path -Raw
    $stateObj = $raw | ConvertFrom-Json

    function ConvertTo-HashtableDeep {
      param([Parameter(Mandatory)]$InputObject)
      if ($null -eq $InputObject) { return $null }

      if ($InputObject -is [System.Collections.IDictionary]) {
        $h = @{}
        foreach ($k in $InputObject.Keys) {
          $h["$k"] = ConvertTo-HashtableDeep -InputObject $InputObject[$k]
        }
        return $h
      }

      if (($InputObject -is [System.Collections.IEnumerable]) -and -not ($InputObject -is [string])) {
        return @($InputObject | ForEach-Object { ConvertTo-HashtableDeep -InputObject $_ })
      }

      if ($InputObject -is [pscustomobject]) {
        $h = @{}
        foreach ($p in $InputObject.PSObject.Properties) {
          $h[$p.Name] = ConvertTo-HashtableDeep -InputObject $p.Value
        }
        return $h
      }

      return $InputObject
    }

    $state = ConvertTo-HashtableDeep -InputObject $stateObj
    if (-not $state.ContainsKey('devices') -or -not $state.devices) { $state.devices = @{} }
    return $state
  } catch {
    Write-Log WARN "State file is not valid JSON, recreating: $Path"
    return @{
      updatedAt = (Get-Date).ToString('o')
      devices   = @{}
    }
  }
}

function Save-State {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)]$State
  )
  $State.updatedAt = (Get-Date).ToString('o')
  ($State | ConvertTo-Json -Depth 10) | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Convert-IPv4ToUInt32 {
  param([Parameter(Mandatory)][string]$Ip)
  $bytes = [System.Net.IPAddress]::Parse($Ip).GetAddressBytes()
  [Array]::Reverse($bytes)
  return [BitConverter]::ToUInt32($bytes, 0)
}

function Convert-UInt32ToIPv4 {
  param([Parameter(Mandatory)][uint32]$Value)
  $bytes = [BitConverter]::GetBytes($Value)
  [Array]::Reverse($bytes)
  return ([System.Net.IPAddress]::new($bytes)).ToString()
}

function Get-NetworkRange {
  param(
    [Parameter(Mandatory)][string]$IpAddress,
    [Parameter(Mandatory)][int]$PrefixLength
  )
  if ($PrefixLength -lt 0 -or $PrefixLength -gt 32) { throw "Invalid prefix length: $PrefixLength" }
  $ip = Convert-IPv4ToUInt32 -Ip $IpAddress
  $mask = if ($PrefixLength -eq 0) { [uint32]0 } else { [uint32]::MaxValue -shl (32 - $PrefixLength) }
  $network = $ip -band $mask
  $broadcast = $network -bor ([uint32]::MaxValue -bxor $mask)
  return [pscustomobject]@{
    NetworkUInt32   = $network
    BroadcastUInt32 = $broadcast
    HostCount       = [int]([math]::Max(0, ($broadcast - $network - 1)))
  }
}

function Get-LocalIPv4Networks {
  $nets = @()
  $nics = [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
    Where-Object { $_.OperationalStatus -eq 'Up' }

  foreach ($nic in $nics) {
    $props = $nic.GetIPProperties()
    foreach ($ua in $props.UnicastAddresses) {
      if ($ua.Address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { continue }
      $ip = $ua.Address.ToString()
      if ($ip -like '127.*' -or $ip -like '169.254.*') { continue }
      $prefix = $null
      try { $prefix = $ua.PrefixLength } catch { $prefix = $null }
      if (-not $prefix) {
        try {
          $maskBytes = $ua.IPv4Mask.GetAddressBytes()
          $bits = ($maskBytes | ForEach-Object { [Convert]::ToString($_, 2).PadLeft(8, '0') }) -join ''
          $prefix = ($bits.ToCharArray() | Where-Object { $_ -eq '1' }).Count
        } catch {
          continue
        }
      }
      $nets += [pscustomobject]@{ IPAddress = $ip; PrefixLength = [int]$prefix }
    }
  }
  return ,$nets
}

function Get-SubnetCandidates {
  param(
    [Parameter(Mandatory)]$Devices,
    [Parameter(Mandatory)][int]$MaxHosts
  )

  $subnets = New-Object System.Collections.Generic.List[object]
  $seen = @{}

  foreach ($net in (Get-LocalIPv4Networks)) {
    $prefix = $net.PrefixLength
    $range = Get-NetworkRange -IpAddress $net.IPAddress -PrefixLength $prefix
    if ($range.HostCount -gt $MaxHosts) {
      $prefix = 24
    }
    $k = "$($net.IPAddress)/$prefix"
    if (-not $seen.ContainsKey($k)) {
      $seen[$k] = $true
      $subnets.Add([pscustomobject]@{ IPAddress = $net.IPAddress; PrefixLength = $prefix })
    }
  }

  foreach ($d in $Devices) {
    if (-not $d.device_id) { continue }
    $ip = ($d.device_id -split ':')[0]
    if ($ip -notmatch '^(\\d{1,3}\\.){3}\\d{1,3}$') { continue }
    $prefix = 24
    $k = "$ip/$prefix"
    if (-not $seen.ContainsKey($k)) {
      $seen[$k] = $true
      $subnets.Add([pscustomobject]@{ IPAddress = $ip; PrefixLength = $prefix })
    }
  }

  return ,$subnets.ToArray()
}

function Get-IPsInSubnet {
  param(
    [Parameter(Mandatory)][string]$IpAddress,
    [Parameter(Mandatory)][int]$PrefixLength
  )
  $range = Get-NetworkRange -IpAddress $IpAddress -PrefixLength $PrefixLength
  if ($range.HostCount -le 0) { return @() }
  $start = [uint32]($range.NetworkUInt32 + 1)
  $end = [uint32]($range.BroadcastUInt32 - 1)

  $ips = New-Object System.Collections.Generic.List[string]
  for ($i = $start; $i -le $end; $i++) {
    $ips.Add((Convert-UInt32ToIPv4 -Value $i))
  }
  return ,$ips.ToArray()
}

function Find-OpenTcpPortHosts {
  param(
    [Parameter(Mandatory)][string[]]$IpAddresses,
    [Parameter(Mandatory)][int]$Port,
    [Parameter(Mandatory)][int]$TimeoutMs,
    [Parameter(Mandatory)][int]$Throttle
  )

  if ($IpAddresses.Count -eq 0) { return @() }

  if ($PSVersionTable.PSVersion.Major -ge 7) {
    $open = $IpAddresses | ForEach-Object -Parallel {
      $ip = $_
      $client = $null
      try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $ar = $client.BeginConnect($ip, $using:Port, $null, $null)
        if ($ar.AsyncWaitHandle.WaitOne($using:TimeoutMs, $false)) {
          try { $client.EndConnect($ar) } catch { }
          if ($client.Connected) { return $ip }
        }
      } catch { }
      finally {
        if ($client) { $client.Close() }
      }
    } -ThrottleLimit $Throttle

    return @($open | Where-Object { $_ } | Sort-Object -Unique)
  }

  $open = foreach ($ip in $IpAddresses) {
    $client = $null
    try {
      $client = [System.Net.Sockets.TcpClient]::new()
      $ar = $client.BeginConnect($ip, $Port, $null, $null)
      if ($ar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
        try { $client.EndConnect($ar) } catch { }
        if ($client.Connected) { $ip }
      }
    } catch { }
    finally {
      if ($client) { $client.Close() }
    }
  }
  return @($open | Sort-Object -Unique)
}

Assert-HdcAvailable

$state = Load-State -Path $StatePath

Write-Log INFO "Devices: $DevicesJsonPath (reloaded each loop)"
Write-Log INFO "State: $StatePath"
Write-Log INFO "Interval: ${IntervalSeconds}s (EnableLanScan=$EnableLanScan, EnableIpPortScan=$EnableIpPortScan, NoWriteConfig=$NoWriteConfig, Once=$Once)"
Write-Log INFO "ExtraPorts: $($ExtraPorts -join ',')"

$devicesCache = Get-DevicesFromJson -Path $DevicesJsonPath
if (-not $devicesCache -or $devicesCache.Count -eq 0) {
  throw "No devices found in $DevicesJsonPath"
}
$devicesMtimeUtcCache = (Get-Item -LiteralPath $DevicesJsonPath).LastWriteTimeUtc
Write-Log INFO "Loaded $($devicesCache.Count) devices from $DevicesJsonPath"

while ($true) {
  $nowIso = Get-NowIso
  $devices = $null
  $devicesMtimeUtc = $devicesMtimeUtcCache
  try {
    $devicesMtimeUtc = (Get-Item -LiteralPath $DevicesJsonPath).LastWriteTimeUtc
    $devices = Get-DevicesFromJson -Path $DevicesJsonPath
    if (-not $devices -or $devices.Count -eq 0) {
      throw "No devices found in $DevicesJsonPath"
    }
    $devicesCache = $devices
    $devicesMtimeUtcCache = $devicesMtimeUtc
  } catch {
    Write-Log ERROR "Failed to load devices json, using last known devices. $($_.Exception.Message)"
    $devices = $devicesCache
    $devicesMtimeUtc = $devicesMtimeUtcCache
  }

  $portsAll = Get-CandidatePorts -Devices $devices -State $state -ExtraPorts $ExtraPorts

  $targets = Get-HdcTargetsVerbose
  $connected = Get-ConnectedTargetsSet -Targets $targets

  $offline = New-Object System.Collections.Generic.List[object]
  $stateChanged = $false
  $devicesChanged = $false
  $deviceByUdid = @{}
  $expectedUdids = @{}
  $prevOnlineByUdid = @{}
  foreach ($d in $devices) {
    $uRaw = Get-ObjField -Obj $d -Name 'udid'
    if (-not $uRaw) { continue }
    $u = "$uRaw".ToUpperInvariant()
    if (-not $u) { continue }
    $deviceByUdid[$u] = $d
    $expectedUdids[$u] = $true
    $prevOnlineByUdid[$u] = Normalize-Online -Value (Get-ObjField -Obj $d -Name 'online')
  }

  $connectedUdidToTarget = @{}
  foreach ($ck in $connected.Keys) {
    if ($ck -notmatch '^(\d{1,3}\.){3}\d{1,3}:\d{1,5}$') { continue }
    $u = Get-HdcUdid -Target $ck
    if ($u) { $connectedUdidToTarget[$u] = $ck }
  }

  foreach ($d in $devices) {
    $prevOnline = Normalize-Online -Value (Get-ObjField -Obj $d -Name 'online')
    Set-ObjField -Obj $d -Name 'last_refresh_at' -Value $nowIso
    if (-not $d.PSObject.Properties['changes']) {
      Set-ObjField -Obj $d -Name 'changes' -Value @()
    }
    $devicesChanged = $true

    $oldDeviceId = [string](Get-ObjField -Obj $d -Name 'device_id')
    $udidRaw = Get-ObjField -Obj $d -Name 'udid'
    $udid = $null
    if ($udidRaw) { $udid = "$udidRaw".ToUpperInvariant() }

    $labelParts = @()
    $t = [string](Get-ObjField -Obj $d -Name 'type')
    $m = [string](Get-ObjField -Obj $d -Name 'model')
    if ($t) { $labelParts += $t }
    if ($m) { $labelParts += $m }
    if ($udid) { $labelParts += $udid }
    $label = ($labelParts -join ' | ')
    if (-not $label) { $label = $oldDeviceId }

    if ($udid -and $connectedUdidToTarget.ContainsKey($udid)) {
      $target = [string]$connectedUdidToTarget[$udid]
      if ($oldDeviceId -ne $target) {
        Set-ObjField -Obj $d -Name 'device_id' -Value $target
        Record-DeviceEndpointChange -Device $d -Old $oldDeviceId -New $target -At $nowIso -Reason 'already_connected'
        $oldDeviceId = $target
      }

      Set-ObjField -Obj $d -Name 'online' -Value $true
      Set-ObjField -Obj $d -Name 'last_online_at' -Value $nowIso
      Record-DeviceStatusChange -Device $d -PrevOnline $prevOnline -NowOnline $true -At $nowIso -Reason 'already_connected'

      if (-not $state.devices.ContainsKey($udid) -or -not $state.devices[$udid]) { $state.devices[$udid] = @{} }
      if ($state.devices[$udid]['device_id'] -ne $target) {
        $state.devices[$udid]['device_id'] = $target
        $stateChanged = $true
      }
      $state.devices[$udid]['lastSeen'] = $nowIso

      Write-Log INFO "Connected: $label"
      continue
    }

    $knownTarget = $null
    if ($udid -and $state.devices.ContainsKey($udid) -and $state.devices[$udid] -and $state.devices[$udid].ContainsKey('device_id')) {
      $knownTarget = "$($state.devices[$udid]['device_id'])"
    }

    $candidates = @($knownTarget, $oldDeviceId) | Where-Object { $_ -and -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique

    $connectedCandidate = $null
    foreach ($c in $candidates) {
      if ($connected.ContainsKey($c)) {
        $connectedCandidate = $c
        break
      }
    }

    if ($connectedCandidate) {
      $target = [string]$connectedCandidate
      if ($oldDeviceId -ne $target) {
        Set-ObjField -Obj $d -Name 'device_id' -Value $target
        Record-DeviceEndpointChange -Device $d -Old $oldDeviceId -New $target -At $nowIso -Reason 'already_connected'
        $oldDeviceId = $target
      }

      if (-not $udid) {
        $newUdid = Get-HdcUdid -Target $target
        if ($newUdid -and -not $deviceByUdid.ContainsKey($newUdid)) {
          Set-ObjField -Obj $d -Name 'udid' -Value $newUdid
          $udid = $newUdid
          $deviceByUdid[$udid] = $d
          $expectedUdids[$udid] = $true
          $prevOnlineByUdid[$udid] = $prevOnline
        }
      }

      Set-ObjField -Obj $d -Name 'online' -Value $true
      Set-ObjField -Obj $d -Name 'last_online_at' -Value $nowIso
      Record-DeviceStatusChange -Device $d -PrevOnline $prevOnline -NowOnline $true -At $nowIso -Reason 'already_connected'

      if ($udid) {
        if (-not $state.devices.ContainsKey($udid) -or -not $state.devices[$udid]) { $state.devices[$udid] = @{} }
        if ($state.devices[$udid]['device_id'] -ne $target) {
          $state.devices[$udid]['device_id'] = $target
          $stateChanged = $true
        }
        $state.devices[$udid]['lastSeen'] = $nowIso
      }

      Write-Log INFO "Connected: $label"
      continue
    }

    $connectedNowTarget = $null
    foreach ($c in $candidates) {
      Write-Log INFO "Try connect: $c"
      $r = Try-HdcConnect -Target $c
      if ($r.ExitCode -ne 0) {
        $msg = $r.Output.Trim()
        if ($msg) { Write-Log WARN "tconn failed: $c ($msg)" }
        continue
      }
      Start-Sleep -Milliseconds 300
      $targets = Get-HdcTargetsVerbose
      $connected = Get-ConnectedTargetsSet -Targets $targets
      if ($connected.ContainsKey($c)) {
        $connectedNowTarget = $c
        break
      }
    }

    if ($connectedNowTarget) {
      $target = [string]$connectedNowTarget
      Write-Log INFO "Reconnected: $label -> $target"

      if ($oldDeviceId -ne $target) {
        Set-ObjField -Obj $d -Name 'device_id' -Value $target
        Record-DeviceEndpointChange -Device $d -Old $oldDeviceId -New $target -At $nowIso -Reason 'tconn'
        $oldDeviceId = $target
      }

      if (-not $udid) {
        $newUdid = Get-HdcUdid -Target $target
        if ($newUdid -and -not $deviceByUdid.ContainsKey($newUdid)) {
          Set-ObjField -Obj $d -Name 'udid' -Value $newUdid
          $udid = $newUdid
          $deviceByUdid[$udid] = $d
          $expectedUdids[$udid] = $true
          $prevOnlineByUdid[$udid] = $prevOnline
        }
      }

      Set-ObjField -Obj $d -Name 'online' -Value $true
      Set-ObjField -Obj $d -Name 'last_online_at' -Value $nowIso
      Record-DeviceStatusChange -Device $d -PrevOnline $prevOnline -NowOnline $true -At $nowIso -Reason 'tconn'

      if ($udid) {
        if (-not $state.devices.ContainsKey($udid) -or -not $state.devices[$udid]) { $state.devices[$udid] = @{} }
        if ($state.devices[$udid]['device_id'] -ne $target) {
          $state.devices[$udid]['device_id'] = $target
          $stateChanged = $true
        }
        $state.devices[$udid]['lastSeen'] = $nowIso
      }
      continue
    }

    $recovered = $false
    if ($EnableIpPortScan -and ($udid -or $candidates.Count -gt 0)) {
      $ips = @()
      $triedPorts = New-Object System.Collections.Generic.HashSet[int]
      foreach ($c in $candidates) {
        $sp = Split-IpPort -ConnectKey $c
        if ($sp.Ip) { $ips += $sp.Ip }
        if ($sp.Port) { $null = $triedPorts.Add([int]$sp.Port) }
      }
      $ips = @($ips | Select-Object -Unique)
      $portsToTry = @($portsAll | Where-Object { -not $triedPorts.Contains($_) })

      foreach ($ip in $ips) {
        if (-not $portsToTry -or $portsToTry.Count -eq 0) { break }
        $openPorts = Find-OpenTcpPortsOnHost -IpAddress $ip -Ports $portsToTry -TimeoutMs $ScanTimeoutMs -Throttle $ScanThrottle
        foreach ($port in $openPorts) {
          $target = "${ip}:$port"
          if (-not $connected.ContainsKey($target)) {
            $r = Try-HdcConnect -Target $target
            if ($r.ExitCode -ne 0) { continue }
            Start-Sleep -Milliseconds 300
          }

          $u = Get-HdcUdid -Target $target
          if (-not $u) { continue }
          if ($udid -and $u -ne $udid) { continue }

          if (-not $udid) {
            Set-ObjField -Obj $d -Name 'udid' -Value $u
            $udid = $u
            $deviceByUdid[$udid] = $d
            $expectedUdids[$udid] = $true
            $prevOnlineByUdid[$udid] = $prevOnline
          }

          if ($oldDeviceId -ne $target) {
            Set-ObjField -Obj $d -Name 'device_id' -Value $target
            Record-DeviceEndpointChange -Device $d -Old $oldDeviceId -New $target -At $nowIso -Reason 'ip_port_scan'
            $oldDeviceId = $target
          }

          Set-ObjField -Obj $d -Name 'online' -Value $true
          Set-ObjField -Obj $d -Name 'last_online_at' -Value $nowIso
          Record-DeviceStatusChange -Device $d -PrevOnline $prevOnline -NowOnline $true -At $nowIso -Reason 'ip_port_scan'

          if (-not $state.devices.ContainsKey($udid) -or -not $state.devices[$udid]) { $state.devices[$udid] = @{} }
          if ($state.devices[$udid]['device_id'] -ne $target) {
            $state.devices[$udid]['device_id'] = $target
            $stateChanged = $true
          }
          $state.devices[$udid]['lastSeen'] = $nowIso

          Write-Log INFO "Recovered by ip-port scan: $label -> $target"
          $recovered = $true
          break
        }
        if ($recovered) { break }
      }
    }

    if ($recovered) { continue }

    $offline.Add($d) | Out-Null
    Write-Log WARN "Offline:   $label"
  }

  if ($EnableLanScan -and $offline.Count -gt 0) {
    Write-Log WARN "LAN scan: $($offline.Count) device(s) still offline. Scanning..."

    $subnets = Get-SubnetCandidates -Devices $devices -MaxHosts $MaxScanHosts
    $ipPool = New-Object System.Collections.Generic.HashSet[string]
    foreach ($s in $subnets) {
      foreach ($ip in (Get-IPsInSubnet -IpAddress $s.IPAddress -PrefixLength $s.PrefixLength)) {
        $null = $ipPool.Add($ip)
      }
    }

    $offlineUdids = @{}
    foreach ($d in $offline) {
      $uRaw = Get-ObjField -Obj $d -Name 'udid'
      if ($uRaw) { $offlineUdids["$uRaw".ToUpperInvariant()] = $true }
    }

    foreach ($port in $portsAll) {
      Write-Log INFO "Scan port $port on $($ipPool.Count) host(s) (timeout=${ScanTimeoutMs}ms, throttle=$ScanThrottle)"
      $openIps = Find-OpenTcpPortHosts -IpAddresses @($ipPool) -Port $port -TimeoutMs $ScanTimeoutMs -Throttle $ScanThrottle
      Write-Log INFO "Found $(@($openIps).Count) host(s) with port $port open"

      foreach ($ip in $openIps) {
        if ($offlineUdids.Count -eq 0) { break }

        $target = "${ip}:$port"
        if (-not $connected.ContainsKey($target)) {
          $r = Try-HdcConnect -Target $target
          if ($r.ExitCode -ne 0) { continue }
          Start-Sleep -Milliseconds 300
        }

        $udid = Get-HdcUdid -Target $target
        if (-not $udid) { continue }

        if ($expectedUdids.ContainsKey($udid) -and $offlineUdids.ContainsKey($udid)) {
          Write-Log INFO "Discovered device: $udid @ $target"
          if ($deviceByUdid.ContainsKey($udid)) {
            $cfg = $deviceByUdid[$udid]
            $prev = $null
            if ($prevOnlineByUdid.ContainsKey($udid)) { $prev = $prevOnlineByUdid[$udid] }

            $old = [string](Get-ObjField -Obj $cfg -Name 'device_id')
            if ($old -ne $target) {
              Set-ObjField -Obj $cfg -Name 'device_id' -Value $target
              Record-DeviceEndpointChange -Device $cfg -Old $old -New $target -At $nowIso -Reason 'lan_scan'
            }

            Set-ObjField -Obj $cfg -Name 'online' -Value $true
            Set-ObjField -Obj $cfg -Name 'last_online_at' -Value $nowIso
            Record-DeviceStatusChange -Device $cfg -PrevOnline $prev -NowOnline $true -At $nowIso -Reason 'lan_scan'
          }

          if (-not $state.devices.ContainsKey($udid) -or -not $state.devices[$udid]) { $state.devices[$udid] = @{} }
          if ($state.devices[$udid]['device_id'] -ne $target) {
            $state.devices[$udid]['device_id'] = $target
            $stateChanged = $true
          }
          $state.devices[$udid]['lastSeen'] = $nowIso
          $offlineUdids.Remove($udid) | Out-Null
        }
      }
    }

    # Refresh connected set after scan
    $targets = Get-HdcTargetsVerbose
    $connected = Get-ConnectedTargetsSet -Targets $targets
  }

  foreach ($d in $offline) {
    $udidRaw = Get-ObjField -Obj $d -Name 'udid'
    $udid = $null
    if ($udidRaw) { $udid = "$udidRaw".ToUpperInvariant() }

    $lastOnlineAt = [string](Get-ObjField -Obj $d -Name 'last_online_at')
    if ($lastOnlineAt -eq $nowIso) { continue }
    if ($udid -and $state.devices.ContainsKey($udid) -and $state.devices[$udid] -and $state.devices[$udid].ContainsKey('lastSeen')) {
      if ("$($state.devices[$udid]['lastSeen'])" -eq $nowIso) { continue }
    }

    $prevOnline = Normalize-Online -Value (Get-ObjField -Obj $d -Name 'online')
    Set-ObjField -Obj $d -Name 'online' -Value $false
    Record-DeviceStatusChange -Device $d -PrevOnline $prevOnline -NowOnline $false -At $nowIso -Reason 'unreachable'
  }

  if ($stateChanged) {
    Save-State -Path $StatePath -State $state
    Write-Log INFO "State updated: $StatePath"
  }

  if ($devicesChanged -and -not $NoWriteConfig) {
    $processedByUdid = @{}
    foreach ($d in $devices) {
      $uRaw = Get-ObjField -Obj $d -Name 'udid'
      if (-not $uRaw) { continue }
      $u = "$uRaw".ToUpperInvariant()
      if ($u) { $processedByUdid[$u] = $d }
    }

    $devicesToWrite = $devices
    try {
      $currentMtimeUtc = (Get-Item -LiteralPath $DevicesJsonPath).LastWriteTimeUtc
      if ($currentMtimeUtc -ne $devicesMtimeUtc) {
        try {
          $latest = Get-DevicesFromJson -Path $DevicesJsonPath
          foreach ($ld in $latest) {
            $luRaw = Get-ObjField -Obj $ld -Name 'udid'
            if (-not $luRaw) { continue }
            $lu = "$luRaw".ToUpperInvariant()
            if (-not $lu -or -not $processedByUdid.ContainsKey($lu)) { continue }
            $src = $processedByUdid[$lu]
            foreach ($field in @('device_id', 'udid', 'online', 'last_online_at', 'last_refresh_at', 'changes')) {
              if ($src.PSObject.Properties[$field]) {
                Set-ObjField -Obj $ld -Name $field -Value (Get-ObjField -Obj $src -Name $field)
              }
            }
          }
          $devicesToWrite = $latest
        } catch {
          Write-Log WARN "Devices json changed and reload failed, skipping write this round: $($_.Exception.Message)"
          $devicesToWrite = $null
        }
      }
    } catch { }

    if ($devicesToWrite) {
      Save-DevicesJson -Path $DevicesJsonPath -Devices $devicesToWrite
      Write-Log INFO "Config updated: $DevicesJsonPath"
    }
  }

  if ($Once) { break }
  Start-Sleep -Seconds $IntervalSeconds
}
