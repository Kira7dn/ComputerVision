[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet('install', 'check', 'remove')]
  [string]$Command = 'check'
)

$ErrorActionPreference = 'Stop'
$rules = @(
  @{ Name = 'Letron Camera HTTPS'; Protocol = 'TCP'; Port = 8971 },
  @{ Name = 'Letron Camera WebRTC TCP'; Protocol = 'TCP'; Port = 8555 },
  @{ Name = 'Letron Camera WebRTC UDP'; Protocol = 'UDP'; Port = 8555 }
)

function Assert-Administrator {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = [Security.Principal.WindowsPrincipal]::new($identity)
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this command from an elevated PowerShell session.'
  }
}

function Get-ManagedRule([string]$Name) {
  return Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
}

function Test-ManagedRules {
  foreach ($definition in $rules) {
    $rule = Get-ManagedRule $definition.Name
    if ($null -eq $rule -or $rule.Enabled -ne 'True' -or $rule.Action -ne 'Allow') {
      throw "Missing or disabled firewall rule: $($definition.Name)"
    }
    $port = $rule | Get-NetFirewallPortFilter
    $address = $rule | Get-NetFirewallAddressFilter
    if ($port.Protocol -ne $definition.Protocol -or [string]$port.LocalPort -ne [string]$definition.Port) {
      throw "Unexpected port scope for firewall rule: $($definition.Name)"
    }
    if (@($address.RemoteAddress) -notcontains 'LocalSubnet') {
      throw "Firewall rule is not limited to LocalSubnet: $($definition.Name)"
    }
    if ($rule.Profile -match 'Public|Any') {
      throw "Firewall rule permits the Public profile: $($definition.Name)"
    }
  }

  $published = @(& docker port frigate 2>$null)
  if ($published -match '(^|:)5001\b|(^|:)8554\b') {
    throw 'Frigate still publishes an unauthenticated host port (5001 or 8554).'
  }
  Write-Host 'Camera firewall rules are restricted to LocalSubnet.'
}

switch ($Command) {
  'install' {
    Assert-Administrator
    foreach ($definition in $rules) {
      Get-ManagedRule $definition.Name | Remove-NetFirewallRule
      New-NetFirewallRule `
        -DisplayName $definition.Name `
        -Direction Inbound `
        -Action Allow `
        -Enabled True `
        -Profile Domain,Private `
        -Protocol $definition.Protocol `
        -LocalPort $definition.Port `
        -RemoteAddress LocalSubnet | Out-Null
    }
    Test-ManagedRules
  }
  'check' { Test-ManagedRules }
  'remove' {
    Assert-Administrator
    foreach ($definition in $rules) {
      Get-ManagedRule $definition.Name | Remove-NetFirewallRule
    }
    Write-Host 'Camera firewall rules removed.'
  }
}
