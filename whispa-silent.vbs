' Starts whispa with no window at all.
'
' This is also what the "Start with Windows" tray setting registers, so it must
' set the working directory itself: "pythonw -m whispa" needs the folder that
' contains the package on the path, and a registry Run entry cannot supply one.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
shell.Run """" & here & "\.venv\Scripts\pythonw.exe"" -m whispa", 0, False
