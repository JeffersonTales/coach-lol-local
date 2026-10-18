@echo off
title Coach de LoL
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python coach.py
pause
