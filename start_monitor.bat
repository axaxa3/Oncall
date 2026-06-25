@echo off
REM ============================================================
REM  监控栈启动脚本 (Windows CMD) — Prometheus + Node Exporter
REM ============================================================
REM  用法:
REM    start_monitor.bat             :: 启动所有服务
REM    start_monitor.bat stop        :: 停止所有服务
REM    start_monitor.bat restart     :: 重启所有服务
REM    start_monitor.bat status      :: 查看状态
REM    start_monitor.bat logs        :: 查看日志
REM ============================================================

set COMPOSE_FILE=docker-compose.monitor.yml
set PROJECT_DIR=%~dp0

goto check_docker

:check_docker
docker compose version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 docker compose，请先安装 Docker Desktop
    pause
    exit /b 1
)
goto :%1

:start
echo ==========================================
echo   启动监控栈...
echo ==========================================
cd /d "%PROJECT_DIR%"
docker compose -f %COMPOSE_FILE% up -d
echo.
echo 等待服务就绪...
timeout /t 5 /nobreak >nul
echo.
echo Prometheus UI:  http://localhost:9090
echo Node Exporter:  http://localhost:9100/metrics
echo.
echo 运行 "docker compose -f %COMPOSE_FILE% ps" 查看状态
pause
goto :eof

:stop
echo ==========================================
echo   停止监控栈...
echo ==========================================
cd /d "%PROJECT_DIR%"
docker compose -f %COMPOSE_FILE% down
echo 已停止。
pause
goto :eof

:restart
echo ==========================================
echo   重启监控栈...
echo ==========================================
cd /d "%PROJECT_DIR%"
docker compose -f %COMPOSE_FILE% restart
timeout /t 3 /nobreak >nul
echo 已重启。
docker compose -f %COMPOSE_FILE% ps
pause
goto :eof

:status
echo ==========================================
echo   监控栈状态
echo ==========================================
cd /d "%PROJECT_DIR%"
docker compose -f %COMPOSE_FILE% ps
pause
goto :eof

:logs
echo ==========================================
echo   查看日志 (Ctrl+C 退出)
echo ==========================================
cd /d "%PROJECT_DIR%"
docker compose -f %COMPOSE_FILE% logs -f
goto :eof

:eof
echo 用法: start_monitor.bat {start|stop|restart|status|logs}
pause
