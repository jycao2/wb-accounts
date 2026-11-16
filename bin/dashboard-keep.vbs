' wb-accounts keep-alive dashboard launcher (silent start, for autostart)
' Double-click or put in the Startup folder: no window, no browser popup -
' just brings the dashboard service up in the background.
' Then open http://127.0.0.1:8765 in the browser (the first time, open the
' ?t=... link once so the browser remembers the cookie).
'
' Paths are auto-detected, same rules as open-dashboard.vbs;
' the token is generated automatically by dashboard.py on first run.

Option Explicit

Dim sh, fso, py, script

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

script = FindScript()
If script = "" Then WScript.Quit 0   ' silent mode: exit quietly, never disturb login
py = FindPython()
If py = "" Then WScript.Quit 0

' 0 = hidden window, False = do not wait; dashboard.py skips by itself if the port is busy
sh.Run """" & py & """ """ & script & """ --no-browser", 0, False

Function UserHome()
  UserHome = sh.ExpandEnvironmentStrings("%USERPROFILE%")
End Function

Function FindScript()
  Dim base, cand, i
  FindScript = ""
  base = fso.GetParentFolderName(WScript.ScriptFullName)
  cand = Array(base & "\scripts\dashboard.py", _
               base & "\..\scripts\dashboard.py", _
               base & "\dashboard.py", _
               UserHome() & "\.workbuddy\skills\wb-accounts\scripts\dashboard.py")
  For i = 0 To UBound(cand)
    If fso.FileExists(cand(i)) Then
      FindScript = fso.GetAbsolutePathName(cand(i))
      Exit Function
    End If
  Next
End Function

Function FindPython()
  Dim root, ver, exe
  FindPython = ""
  root = UserHome() & "\.workbuddy\binaries\python\versions"
  If fso.FolderExists(root) Then
    For Each ver In fso.GetFolder(root).SubFolders
      exe = ver.Path & "\pythonw.exe"
      If fso.FileExists(exe) Then
        FindPython = exe
        Exit Function
      End If
    Next
  End If
  FindPython = "pythonw.exe"
End Function
