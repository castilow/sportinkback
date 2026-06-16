#!/usr/bin/env bash
# Arranca el backend de Sportink en local.
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "→ Creando entorno virtual..."
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "→ Instalando dependencias..."
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  echo "⚠  No existe .env — copiando desde .env.example (recuerda rellenarlo)."
  cp .env.example .env
fi

PORT="${PORT:-8000}"

# Si el puerto está ocupado (proceso anterior), lo liberamos.
if lsof -ti tcp:"${PORT}" >/dev/null 2>&1; then
  echo "→ Puerto ${PORT} ocupado, liberándolo..."
  lsof -ti tcp:"${PORT}" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo "→ Arrancando API en http://localhost:${PORT}/api"
uvicorn server:app --reload --port "${PORT}"
