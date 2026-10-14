@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Python da virtualenv nao encontrado em .venv\Scripts\python.exe
    echo Confirme que a virtualenv do projeto foi criada.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m cv2randomizer.main
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" (
    echo.
    echo [ERRO] O Randomizer encerrou com codigo %exit_code%.
    pause
)

exit /b %exit_code%
