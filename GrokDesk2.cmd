@echo off
set "DESK_PS1=%~dp0run-desk2.ps1"
if not exist "%DESK_PS1%" (
  if defined DESK_HOME (
    set "DESK_PS1=%DESK_HOME%\run-desk2.ps1"
  ) else (
    set "DESK_PS1=%USERPROFILE%\grok-desk\run-desk2.ps1"
  )
)
if not exist "%DESK_PS1%" (
  echo run-desk2.ps1 was not found. Set DESK_HOME to the grok-desk folder.
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%DESK_PS1%"
