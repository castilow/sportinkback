#!/usr/bin/env bash
# Arranca el backend de Sportink. Sirve tanto en local como en Render.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

# ---------- Producción (Render) ----------
# Render define la variable RENDER automáticamente, instala las dependencias
# en el build y pasa las variables de entorno directamente (sin .env).
# Lo único imprescindible: enlazar a 0.0.0.0 y al puerto $PORT.
if [ -n "$RENDER" ]; then
  echo "→ Arrancando API (producción) en 0.0.0.0:${PORT}"
  exec uvicorn server:app --host 0.0.0.0 --port "${PORT}"
fi

# ---------- Desarrollo local ----------
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

# Liberar el puerto si quedó ocupado por un proceso anterior.
if command -v lsof >/dev/null 2>&1 && lsof -ti tcp:"${PORT}" >/dev/null 2>&1; then
  echo "→ Puerto ${PORT} ocupado, liberándolo..."
  lsof -ti tcp:"${PORT}" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo "→ Arrancando API en http://localhost:${PORT}/api"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT}" --reload
