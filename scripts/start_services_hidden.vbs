Option Explicit

Dim shell, fileSystem, startScript, powerShell, command, argument, exitCode

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

startScript = fileSystem.BuildPath( _
    fileSystem.GetParentFolderName(WScript.ScriptFullName), _
    "start_services.ps1" _
)
powerShell = shell.ExpandEnvironmentStrings( _
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" _
)
command = """" & powerShell & _
    """ -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & _
    startScript & """"

For Each argument In WScript.Arguments
    Select Case LCase(CStr(argument))
        Case "-coreonly", "-geminionly", "-skipupdate"
            command = command & " " & CStr(argument)
        Case Else
            WScript.Quit 87
    End Select
Next

exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
