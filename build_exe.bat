@echo off
:: Builds VideoMixer.exe and creates the shareable folder NEXT TO this source folder:
::
::   ..\VideoMixer Portable\
::     VideoMixer.exe
::     README.md
::
:: Temporary build files go to .build\ (safe to delete any time).
:: FFmpeg is NOT bundled - if a PC doesn't have it, the app explains how to install it.
setlocal
cd /d "%~dp0"
set "WORK=%~dp0.build"
set "OUT=%~dp0..\VideoMixer Portable"

echo [1/2] Building VideoMixer.exe ...
python -m pip install --upgrade -q -r requirements.txt pyinstaller || goto :error
:: Exclude big libraries the app never uses (they get picked up if installed in the same Python)
python -m PyInstaller --noconfirm --onefile --console --name VideoMixer ^
    --distpath "%WORK%\dist" --workpath "%WORK%\work" --specpath "%WORK%" ^
    --exclude-module numpy --exclude-module PIL --exclude-module psutil --exclude-module tkinter ^
    video_mixer.py || goto :error

echo [2/2] Creating "%OUT%" ...
if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%OUT%" || goto :error
copy /y "%WORK%\dist\VideoMixer.exe" "%OUT%\" >nul || goto :error
copy /y "README.md" "%OUT%\" >nul

echo.
echo Done: %OUT%
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
