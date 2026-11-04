@echo off
REM Script to run the API in development mode on Windows

cd /d "%~dp0src"

echo 🚀 Starting MFL Soil Data API...
echo 📚 Docs available at: http://localhost:8000/docs
echo.

uvicorn main:app --reload --host 0.0.0.0 --port 8000
