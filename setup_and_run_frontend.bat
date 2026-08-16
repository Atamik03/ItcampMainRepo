@echo off
set PATH=c:\Program Files\nodejs;%PATH%
cd /d "c:\Users\atami\Downloads\itcamp\itcamp-1\elou_avt_web"
echo Installing npm dependencies...
npm install
echo.
echo Dependencies installed! Now running dev server...
echo.
npm run dev
pause
