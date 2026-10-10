@echo off
rem Prompt Studio 一键启动（只启动网页服务器，不会拉起任何模型）
setlocal
cd /d "%~dp0"
if not exist .venv (
  echo [setup] 创建虚拟环境并安装依赖…
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)
echo [run] Prompt Studio: http://127.0.0.1:9178
.venv\Scripts\python -m uvicorn server.main:app --host 127.0.0.1 --port 9178
goto :eof
:err
echo [error] 依赖安装失败
pause
