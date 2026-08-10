@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente virtual nao encontrado.
  echo Consulte o README.md para instalar o projeto.
  pause
  exit /b 1
)

start "" "http://127.0.0.1:8787/docs"
".venv\Scripts\python.exe" -m soulscraper api --port 8787

if errorlevel 1 (
  echo.
  echo Nao foi possivel iniciar a API.
  pause
)
