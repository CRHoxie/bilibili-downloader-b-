@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  打包 B 站视频下载器 ...
echo.

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name BiliDownloader ^
  --icon "assets\app.ico" ^
  --add-data "static;static" ^
  --add-data "assets;assets" ^
  --collect-all webview ^
  --collect-all imageio_ffmpeg ^
  --collect-all pythonnet ^
  --collect-all clr_loader ^
  --hidden-import app ^
  --hidden-import bili ^
  --hidden-import bili.wbi ^
  --hidden-import bili.parser ^
  --hidden-import bili.downloader ^
  desktop.py

if errorlevel 1 goto :fail

if exist "dist\BiliDownloader.exe" move /y "dist\BiliDownloader.exe" "dist\B站视频下载器.exe" >nul
echo.
echo  完成： dist\B站视频下载器.exe
echo.
pause
exit /b 0

:fail
echo.
echo  打包失败，请查看上面的报错。
pause
exit /b 1
