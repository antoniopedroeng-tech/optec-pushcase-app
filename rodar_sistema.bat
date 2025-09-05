@echo off
setlocal ENABLEEXTENSIONS

REM Ir para a pasta onde o .bat está
cd /d "%~dp0"

echo ============================================
echo   Sistema de Pedidos - Setup e Execucao
echo ============================================

REM 0) Encontrar Python
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
  where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  echo [ERRO] Python nao encontrado no PATH.
  echo Instale em https://www.python.org/downloads/ e marque "Add Python to PATH".
  pause
  exit /b 1
)

REM 1) Criar venv se nao existir
if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Criando ambiente virtual (.venv)...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERRO] Falha ao criar o ambiente virtual.
    pause
    exit /b 1
  )
) else (
  echo [1/4] Ambiente virtual (.venv) ja existe.
)

REM 2) Ativar venv
echo [2/4] Ativando ambiente virtual...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [ERRO] Nao foi possivel ativar o ambiente virtual.
  pause
  exit /b 1
)

REM 3) Instalar dependencias
if exist "requirements.txt" (
  echo [3/4] Atualizando pip e instalando dependencias do requirements.txt...
  python -m pip install --upgrade pip
  pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERRO] Falha ao instalar dependencias.
    pause
    exit /b 1
  )
) else (
  echo [AVISO] Nao encontrei requirements.txt. Pulando a instalacao de dependencias.
)

REM 4) Rodar o app
if exist "app.py" (
  echo [4/4] Iniciando o aplicativo (python app.py)...
  python app.py
) else (
  echo [ERRO] Nao encontrei app.py nesta pasta: %cd%
  echo Verifique se o bat esta na pasta do projeto.
  pause
  exit /b 1
)

echo.
echo ============================================
echo   Aplicativo finalizado / janela do servidor fechada
echo ============================================
pause
