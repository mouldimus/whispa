' Launches whispa with no console window - put a shortcut to this in
' shell:startup to have dictation available from login.
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & here & "\.venv\Scripts\pythonw.exe"" -m whispa", 0, False
