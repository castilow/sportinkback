# Sportink Backend

Backend independiente de Sportink (API del software de gestión de clubes), extraído
para que **frontend y backend trabajen por separado**. Construido con **FastAPI**
(Python) sobre Supabase/Postgres (y Mongo opcional como legacy).

La API se monta bajo el prefijo **`/api`** y usa autenticación por **JWT en cookies**.

## Estructura

```
server.py                 App FastAPI (entry point: server:app)
deps.py                   Dependencias: auth, conexión BD, helpers
players_store.py          Acceso a datos de jugadores
postgres_compat.py        Capa de compatibilidad Mongo -> Postgres
migrate_mongo_to_supabase.py   Script de migración
routes/                   Routers: chat, sheets, tickets
supabase/                 config.toml + migraciones SQL
requirements.txt          Dependencias Python
.env.example              Plantilla de variables de entorno
```

## Puesta en marcha (local)

```bash
cd sportinkback

# 1) Entorno virtual
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2) Dependencias
pip install -r requirements.txt

# 3) Variables de entorno
cp .env.example .env             # y rellena los valores

# 4) Arrancar la API (recarga en caliente)
uvicorn server:app --reload --port 8000
```

La API queda en `http://localhost:8000/api`.

## Conectar con el frontend

En el frontend (la app del club) define la URL base del backend con esta variable
y asegúrate de que coincide con `CORS_ORIGINS` del backend:

```
# .env del frontend
VITE_BACKEND_URL=http://localhost:8000
# o, según el proyecto:
REACT_APP_BACKEND_URL=http://localhost:8000
```

Y en el backend, en `.env`:

```
CORS_ORIGINS=http://localhost:5173,http://localhost:3000
```

## Base de datos

Aplica las migraciones de `supabase/migrations` en tu proyecto de Supabase
(orden por fecha del nombre de archivo) antes de arrancar.

## Notas de seguridad (importante)

La auditoría original (`REVISION_CODIGO.md` del proyecto del club) detectó varios
puntos a revisar antes de producción. Aquí ya quedan parametrizados por entorno:

- `COOKIE_SECURE=True` en producción (cookies solo por HTTPS).
- `JWT_SECRET` largo y aleatorio, nunca hardcodeado.
- `ADMIN_PASSWORD` por variable de entorno; no dejar credenciales por defecto.
- `CORS_ORIGINS` explícito (sin `*`) porque se usan cookies con credenciales.
