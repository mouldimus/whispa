' Same as whispa.bat, but launched in a way that never flashes a console
' window at all - put a shortcut to this in shell:startup to have dictation
' available from login.
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & here & "\.venv\Scripts\pythonw.exe"" -m whispa", 0, False
