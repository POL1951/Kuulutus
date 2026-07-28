@echo off
cd /d C:\juoksu

rem =====================================================
rem  MUUTA TAMA PER KISA - kansio jossa KILP.DAT ja
rem  KilpSrj.xml sijaitsevat
rem =====================================================
set KISA=C:\kisa\data\Mikkeli.1
rem =====================================================

start "" /b cmd /c "timeout /t 3 >nul & start http://localhost:8081/"
start "" /b cmd /c "timeout /t 4 >nul & start http://localhost:8082/awards"
start "" /b cmd /c "timeout /t 5 >nul & start http://localhost:8083/awards"

python announcer_display.py --sarjat-xml "%KISA%\KilpSrj.xml" --kilp-dat "%KISA%\KILP.DAT" --lahestyminen 2

echo.
echo ====================================================
echo  Ohjelma paattyi. Ikkuna jaa auki - paina nappainta.
echo ====================================================
pause
