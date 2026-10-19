Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class WinR {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
}
"@
$h = (Get-Process -Id 2708).MainWindowHandle
[WinR]::ShowWindow($h, 9) | Out-Null   # SW_RESTORE
Start-Sleep -Milliseconds 200
"opencode restored"
