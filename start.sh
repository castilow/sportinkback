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
echo "→ Arrancando API en http://localhost:${PORT}/api"
uvicorn server:app --reload --port "${PORT}"
