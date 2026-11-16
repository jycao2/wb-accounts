' wb-accounts one-click dashboard launcher (Windows)
' Double-click this file: probe port 8765 -> start the service silently if it is
' not running -> poll until the port answers -> open the browser authenticated.
'
' All paths are auto-detected (no hardcoded username / version):
'   dashboard.py: scripts\ next to this file, ..\scripts\, same folder, or
'                 %USERPROFILE%\.workbuddy\skills\wb-accounts\scripts\
'   python:       %USERPROFILE%\.workbuddy\binaries\python\versions\*\pythonw.exe,
'                 falling back to pythonw.exe on PATH
' The panel token is read from the vault file (dashboard-token.txt) so the
' browser is always opened with ?t=... and never shows a 403 page.

Option Explicit

Dim sh, fso, url, py, script, t, tok, openUrl

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

url = "http://127.0.0.1:8765/"

If Not Probe() Then
  script = FindScript()
  If script = "" Then
    MsgBox "dashboard.py not found." & vbCrLf & vbCrLf & _
           "Keep this file in the wb-accounts bin\ folder, or install the" & vbCrLf & _
           "project under %USERPROFILE%\.workbuddy\skills\wb-accounts\.", 16, "wb-accounts"
    WScript.Quit 1
  End If
  py = FindPython()
  If py = "" Then
    MsgBox "Python runtime not found." & vbCrLf & vbCrLf & _
           "Install Python 3.10+ and add it to PATH, or keep the runtime" & vbCrLf & _
           "bundled with the WorkBuddy client.", 16, "wb-accounts"
    WScript.Quit 1
  End If
  ' 0 = hidden window, False = do not wait
  sh.Run """" & py & """ """ & script & """ --no-browser", 0, False
  ' Poll until the port answers (first start refreshes snapshots in background)
  For t = 1 To 92
    WScript.Sleep 700
    If Probe() Then Exit For
  Next
End If

' Open the dashboard WITH the token, so a fresh browser never hits 403.
openUrl = url
tok = ReadToken()
If tok <> "" Then openUrl = url & "?t=" & tok
sh.Run openUrl, 1, False

' Probe: any HTTP response counts as running (403 also means the port is listening)
Function Probe()
  Dim h
  Probe = False
  On Error Resume Next
  Set h = CreateObject("MSXML2.XMLHTTP")
  h.Open "GET", url, False
  h.Send
  If Err.Number = 0 Then Probe = True
  Err.Clear
  On Error GoTo 0
End Function

Function VaultHome()
  Dim raw
  raw = sh.ExpandEnvironmentStrings("%WB_ACCOUNTS_HOME%")
  If raw = "%WB_ACCOUNTS_HOME%" Or raw = "" Then
    VaultHome = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\wb-accounts"
  Else
    VaultHome = raw
  End If
End Function

' Read the panel token written by dashboard.py on startup (empty if absent)
Function ReadToken()
  Dim p, ts, s
  ReadToken = ""
  p = VaultHome() & "\dashboard-token.txt"
  If Not fso.FileExists(p) Then Exit Function
  On Error Resume Next
  Set ts = fso.OpenTextFile(p, 1)
  s = ts.ReadLine
  ts.Close
  If Err.Number = 0 Then ReadToken = Trim(s)
  Err.Clear
  On Error GoTo 0
End Function

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
