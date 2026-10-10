@echo off
title Yomira
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Iniciar Yomira.ps1"
if errorlevel 1 pause
