param([ValidateSet('Production','Dev')][string]$Mode = 'Production')
& (Join-Path $PSScriptRoot 'start.ps1') -Action status -Mode $Mode
