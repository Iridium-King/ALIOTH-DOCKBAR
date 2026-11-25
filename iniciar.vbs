Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)

pythonwPath = ""
candidates = Array( _
    WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313"), _
    WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python314"), _
    WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312"), _
    WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311"), _
    "C:\Program Files\Python313", _
    "C:\Program Files\Python312" _
)

For Each pyDir In candidates
    If FSO.FileExists(pyDir & "\pythonw.exe") And FSO.FileExists(pyDir & "\Lib\encodings\__init__.py") Then
        pythonwPath = pyDir & "\pythonw.exe"
        Exit For
    End If
Next

If pythonwPath = "" Then
    pythonwPath = "pyw.exe"
End If

mainPyPath = scriptDir & "\main.py"

WshShell.CurrentDirectory = scriptDir
WshShell.Run """" & pythonwPath & """ """ & mainPyPath & """", 0, False
