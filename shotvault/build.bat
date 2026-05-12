@echo off
echo.
echo  Building ShotVault...
echo.

pip install flask pywebview pyinstaller watchdog --quiet

python -m PyInstaller shotvault.spec --noconfirm

echo.
if exist "dist\ShotVault\ShotVault.exe" (
    echo  Build successful!
) else (
    echo  Build may have failed. Check the output above for errors.
    pause
)
