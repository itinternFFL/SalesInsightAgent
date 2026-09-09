<#
.SYNOPSIS
    Sets up per-employee local Windows accounts and NTFS folder permissions
    for direct filesystem access to data/employees/<name>/, mirroring the
    same Manager -> Senior Executive -> Executive visibility rules already
    enforced by the app's database-driven access control (see
    ACCESS-CONTROL.md). This is a SEPARATE, complementary control - for
    people who browse the data folder directly (File Explorer, RDP, a
    network share), not for the chat app itself, which never checks NTFS
    permissions and always goes through the database instead.

.DESCRIPTION
    For each employee below:
      1. Creates a local Windows user account if one doesn't already exist
         (idempotent - safe to re-run), with a randomly generated password
         printed once so you can hand it to them securely. Accounts are
         created with -PasswordNeverExpires:$false and
         -UserMayNotChangePassword:$false, so mark the account
         "must change password at next logon" yourself if your policy
         requires it (not scripted here, since local account policy varies
         per machine/domain).
      2. On that employee's data\employees\<name>\ folder: disables
         permission inheritance (so the folder doesn't silently inherit
         broad "Users" or "Authenticated Users" access from its parent),
         then grants:
           - SYSTEM and BUILTIN\Administrators: Full Control (required for
             normal Windows/backup operation - do not remove)
           - The employee's own account: Modify (their own data)
           - Everyone ABOVE them in the reporting chain (their Senior
             Executive, their Manager): Read & Execute - mirrors the app's
             "a Manager sees everyone below them" rule exactly, just
             enforced by NTFS instead of the database.
         Peers and anyone outside their own upward chain get NO explicit
         grant, and inheritance is off, so they have no access at all
         unless the folder's parent already grants them something -
         verify data\ itself isn't broadly shared before relying on this.

.NOTES
    - Run this in an ELEVATED PowerShell session (Run as Administrator).
      Creating local accounts and changing ACLs both require it.
    - Review the $Employees table below and edit it to match your actual
      org chart before running - it is NOT read from the app's database,
      to keep this script simple, offline, and reviewable on its own. Keep
      it in sync with backend/db.py's users table by hand if the reporting
      structure changes there.
    - This does not touch data/'s legacy top-level files (the original
      company-wide monthly reports that predate per-employee attribution -
      see ACCESS-CONTROL.md's "Unattributed data") - only the
      data/employees/<name>/ subfolders.
    - Safe to re-run: existing accounts are left alone (only new ones are
      created), and each run resets the target folder's permissions to
      exactly what's defined here, so drift gets corrected, not compounded.
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

# Project root - adjust if you run this from somewhere other than deploy/.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EmployeesRoot = Join-Path $ProjectRoot "data\employees"

# Edit this to match your actual reporting structure (see backend/db.py's
# users table for the current source of truth in the app itself).
# ReportsTo = $null marks the top of a branch (a Manager).
$Employees = @(
    @{ Name = "ummay.fizza"; ReportsTo = $null }
    @{ Name = "Adeen";       ReportsTo = "ummay.fizza" }
    @{ Name = "Shahbaz";     ReportsTo = "ummay.fizza" }
    @{ Name = "Maria";       ReportsTo = "Adeen" }
    @{ Name = "Manahil";     ReportsTo = "Adeen" }
    @{ Name = "Amna";        ReportsTo = "Shahbaz" }
    @{ Name = "Fizza";       ReportsTo = "Shahbaz" }
)

function Get-Ancestors {
    <# Everyone above $Name in the reporting chain - their Senior
       Executive, their Manager, and so on - who should be able to READ
       (not write) $Name's folder, mirroring the app's downward-visibility
       rule from the ancestor's point of view. #>
    param([string]$Name)
    $ancestors = @()
    $current = ($Employees | Where-Object { $_.Name -eq $Name }).ReportsTo
    while ($current) {
        $ancestors += $current
        $current = ($Employees | Where-Object { $_.Name -eq $current }).ReportsTo
    }
    return $ancestors
}

function New-EmployeeAccount {
    param([string]$Name)
    $existing = Get-LocalUser -Name $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Account '$Name' already exists - leaving it alone." -ForegroundColor Yellow
        return
    }
    $password = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 20 | ForEach-Object { [char]$_ })
    $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
    New-LocalUser -Name $Name -Password $securePassword -PasswordNeverExpires:$false `
        -UserMayNotChangePassword:$false -Description "Sales Insight Agent - per-employee data folder access" | Out-Null
    Write-Host "  Created account '$Name' - password: $password" -ForegroundColor Green
    Write-Host "  (copy this now - it will not be shown again; share it securely, not over chat/email in the clear)" -ForegroundColor Green
}

function Set-EmployeeFolderPermissions {
    param([string]$Name, [string[]]$ReadOnlyAncestors)

    $folderPath = Join-Path $EmployeesRoot $Name
    if (-not (Test-Path $folderPath)) {
        New-Item -ItemType Directory -Path $folderPath -Force | Out-Null
        Write-Host "  Created missing folder $folderPath" -ForegroundColor Yellow
    }

    $acl = Get-Acl $folderPath
    $acl.SetAccessRuleProtection($true, $false)  # stop inheriting, drop inherited rules
    foreach ($rule in @($acl.Access)) {
        $acl.RemoveAccessRule($rule) | Out-Null
    }

    $rules = @(
        New-Object System.Security.AccessControl.FileSystemAccessRule(
            "NT AUTHORITY\SYSTEM", "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
        New-Object System.Security.AccessControl.FileSystemAccessRule(
            "BUILTIN\Administrators", "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
        New-Object System.Security.AccessControl.FileSystemAccessRule(
            $Name, "Modify", "ContainerInherit,ObjectInherit", "None", "Allow")
    )
    foreach ($ancestor in $ReadOnlyAncestors) {
        $rules += New-Object System.Security.AccessControl.FileSystemAccessRule(
            $ancestor, "ReadAndExecute", "ContainerInherit,ObjectInherit", "None", "Allow")
    }

    foreach ($rule in $rules) {
        $acl.AddAccessRule($rule)
    }
    Set-Acl -Path $folderPath -AclObject $acl
    Write-Host "  Permissions set on $folderPath (owner: $Name; readable by: $($ReadOnlyAncestors -join ', '))" -ForegroundColor Green
}

Write-Host "=== Creating accounts ===" -ForegroundColor Cyan
foreach ($employee in $Employees) {
    Write-Host "$($employee.Name):"
    New-EmployeeAccount -Name $employee.Name
}

Write-Host ""
Write-Host "=== Setting folder permissions ===" -ForegroundColor Cyan
foreach ($employee in $Employees) {
    Write-Host "$($employee.Name):"
    $ancestors = Get-Ancestors -Name $employee.Name
    Set-EmployeeFolderPermissions -Name $employee.Name -ReadOnlyAncestors $ancestors
}

Write-Host ""
Write-Host "Done. Save the printed passwords securely now - they won't be shown again." -ForegroundColor Cyan
