' Launch Stark with no console window (uses pythonw.exe).
' Double-click this to start Stark silently in the background.
Set sh = CreateObject("WScript.Shell")
base = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
sh.Run """" & base & "\.venv\Scripts\pythonw.exe"" """ & base & "\stark.py""", 0, False
