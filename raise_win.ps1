Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class Win3 {
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndAfter, int X, int Y, int cx, int cy, uint uFlags);
}
"@
# 0x0010 = SWP_NOACTIVATE (raise z-order without stealing focus), 0x0003 = NOMOVE|NOSIZE
$h = (Get-Process -Id 2708).MainWindowHandle
[Win3]::SetWindowPos($h, [IntPtr](-1), 0, 0, 0, 0, 0x0013) | Out-Null
"raised NOACTIVATE"
