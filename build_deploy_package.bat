@echo off
setlocal enabledelayedexpansion

:: AgentCore Deployment Build Script for Windows
:: Target: Linux/arm64 (aarch64) for AWS AgentCore Runtime
::- Downloads cross-platform wheels
::- Unpacks them into a staging area
::- Bundles application code (excluding .env, caches)

set PY_VER=3.12
set STAGE=build\stage
set WHEELS=build\wheels
set OUT=dist\agentcore_deployment.zip

echo ==> Cleaning up previous builds...
if exist build rd /s /q build
if exist dist rd /s /q dist
mkdir %WHEELS%
mkdir %STAGE%
mkdir dist

echo ==> Downloading linux/arm64 wheels for Python %PY_VER%...
:: Use pip download with specific platform markers to get wheels compatible with AgentCore Linux runtime
pip download --quiet --only-binary=:all: ^
  --python-version %PY_VER% --implementation cp ^
  --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 ^
  --platform manylinux_2_28_aarch64 --platform linux_aarch64 ^
  --platform any ^
  -r requirements.txt -d "%WHEELS%"

echo ==> Unpacking wheels into staging root...
for %%w in (%WHEELS%\*.whl) do (
    python -m zipfile -e "%%w" "%STAGE%/"
)

echo ==> Bundling application code...
:: Copy application files
xcopy /E /I agents "%STAGE%\agents"
xcopy /E /I static "%STAGE%\static"
copy runtime_entrypoint.py "%STAGE%\"
copy main.py "%STAGE%\"
copy config.py "%STAGE%\"
copy orchestrator.py "%STAGE%\"
copy graph.py "%STAGE%\"
copy monitoring.py "%STAGE%\"
copy requirements.txt "%STAGE%\"
copy pyproject.toml "%STAGE%\"
copy agentcore.json "%STAGE%\"

echo ==> Removing caches...
powershell -Command "Get-ChildItem -Path '%STAGE%' -Include '__pycache__' -Recurse | Remove-Item -Recurse -Force"
powershell -Command "Get-ChildItem -Path '%STAGE%' -Filter '*.pyc' -Recurse | Remove-Item -Force"

echo ==> Creating %OUT%...
powershell -Command "Compress-Archive -Path '%STAGE%\*' -DestinationPath '%OUT%' -Force"

echo ==> Done: %OUT%
echo ==> Runtime entry point: runtime_entrypoint.py (HTTP, port 8080, health /ping)
pause
