#!/usr/bin/env bash
# Builds dist/agentcore_deployment.zip for AgentCore Runtime "S3" source
# (direct code deployment). No Docker required:
#   - downloads linux/arm64 (cp312) wheels matching the AgentCore runtime
#   - unpacks them at the zip root (added to sys.path automatically)
#   - bundles app code, static, templates  (excludes .env, tests, caches)
set -euo pipefail
cd "$(dirname "$0")"

PY_VER=3.12
STAGE=build/stage
WHEELS=build/wheels
OUT=dist/agentcore_deployment.zip

rm -rf build dist
mkdir -p "$WHEELS" "$STAGE" dist

echo "==> Downloading linux/arm64 wheels for Python $PY_VER"
pip download --quiet --only-binary=:all: \
  --python-version $PY_VER --implementation cp \
  --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 \
  --platform manylinux_2_28_aarch64 --platform linux_aarch64 \
  --platform any \
  -r requirements.txt -d "$WHEELS"
echo "    $(ls "$WHEELS" | wc -l) wheels"

echo "==> Unpacking wheels into staging root"
for w in "$WHEELS"/*.whl; do
  python3 -m zipfile -e "$w" "$STAGE/"
done

echo "==> Bundling application code"
cat > "$STAGE/main.py" <<'EOF'
import uvicorn

if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8080, log_level="info")
EOF

cp -r src config templates static "$STAGE/"
cp requirements.txt "$STAGE/"
find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$STAGE" -name "*.pyc" -delete

echo "==> Creating $OUT"
(cd "$STAGE" && zip -qr9 "../../$OUT" .)

echo "==> Done: $OUT ($(du -h "$OUT" | cut -f1), $(python3 -c "import zipfile;print(len(zipfile.ZipFile('$OUT').namelist()))") files)"
echo "==> Entry point for the console:  python  main.py   (HTTP, port 8080, health /ping)"
