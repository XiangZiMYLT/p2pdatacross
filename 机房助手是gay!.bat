@echo off
setlocal enabledelayedexpansion

:: Core configuration (only ASCII characters)
set "target1=studen"
set "target2=main"
set "admin_tip=Run this script as ADMINISTRATOR!"

echo ==================================================
echo Process Killer for studentmain (case-insensitive)
echo ==================================================
echo.

:: Step 1: Get process list in CSV format (compatible with all Windows)
for /f "skip=1 delims=" %%a in ('tasklist /fo csv /nh 2^>nul') do (
    :: Step 2: Parse CSV (remove quotes, extract name/PID)
    for /f "tokens=1,2 delims=," %%b in ("%%a") do (
        set "proc=%%~b"
        set "pid=%%~c"
        
        :: Step 3: Case-insensitive match (no regex, no hidden chars)
        echo !proc! | findstr /i /c:"!target1!" >nul 2>&1
        if !errorlevel! equ 0 (
            echo !proc! | findstr /i /c:"!target2!" >nul 2>&1
            if !errorlevel! equ 0 (
                echo [FOUND] Process: !proc! (PID: !pid!)
                :: Step 4: Kill process (force mode)
                taskkill /f /pid !pid! >nul 2>&1
                if !errorlevel! equ 0 (
                    echo [SUCCESS] Killed: !proc!
                ) else (
                    echo [FAILED] Kill failed: !proc! - !admin_tip!
                )
                echo.
            )
        )
    )
)

echo ==================================================
echo Operation finished. Press any key to exit...
pause >nul
endlocal