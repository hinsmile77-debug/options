' Hidden launcher for watchdog_mahdi.bat. Task Scheduler runs THIS, not the .bat.
'
' Why this file exists (2026-08-11):
'   The watchdog must run in the logged-on interactive session, because its
'   recovery path launches start_mahdi_premarket.bat, which opens console
'   windows for COCKPIT and the observation loop. A task configured as "run
'   whether user is logged on or not" runs in session 0 and cannot do that.
'
'   But an interactive task firing every minute flashes a console window every
'   minute, and watchdog_mahdi.bat's own comment predicted the consequence:
'   a person who sees that will turn the schedule off. The Task Scheduler
'   "Hidden" checkbox does NOT help - it only hides the task from the task list,
'   not the window the action opens.
'
'   WScript.Shell.Run with intWindowStyle=0 is the part that actually hides it.
'
' bWaitOnReturn=True is deliberate: a recovery run can take up to 300 seconds,
' and we want the scheduler to see the task as still running for that whole
' time so MultipleInstances=IgnoreNew suppresses overlapping watchdogs.
' liveness.startup_in_progress() is the second layer of that same guard.
'
' ASCII only, same rule as the .bat - see the banner in watchdog_mahdi.bat.

Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
WScript.Quit shell.Run("""" & here & "watchdog_mahdi.bat""", 0, True)
