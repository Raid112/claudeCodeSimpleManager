Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
desktopPath = shell.SpecialFolders("Desktop")

' Create shortcut on Desktop
Set shortcut = shell.CreateShortcut(desktopPath & "\Claude Code Launcher.lnk")
shortcut.TargetPath = "wscript.exe"
shortcut.Arguments = """" & scriptDir & "\ClaudeCodeLauncher.vbs" & """"
shortcut.WorkingDirectory = scriptDir
shortcut.IconLocation = scriptDir & "\icon.ico"
shortcut.Description = "Claude Code Launcher"
shortcut.WindowStyle = 7  ' Minimized
shortcut.Save

WScript.Echo "Atalho criado na area de trabalho! Clique direito > 'Fixar na barra de tarefas' para fixar."
