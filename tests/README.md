# Tests de Sportink (backend)

## 1. Tests offline (sin base de datos)
    pip install pytest
    pytest tests/test_offline.py -q

## 2. Prueba de aislamiento entre clubes (contra un backend en marcha, solo lectura)
Necesita DOS cuentas admin de clubes distintos (p. ej. Rayo y Caracas):

    pip install requests
    BASE_URL=http://localhost:8000 \
    A_EMAIL=... A_PASS=... B_EMAIL=... B_PASS=... \
    python tests/isolation_check.py

Sale con código 1 y lista las rutas si algún dato es visible para los dos clubes.
Ejecútala antes de cada despliegue importante. No guardes las contraseñas en el repo.
