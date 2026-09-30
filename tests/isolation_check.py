#!/usr/bin/env python3
"""Prueba de aislamiento ENTRE CLUBES contra un backend en marcha (solo lectura).

Inicia sesión con un usuario del club A y otro del club B, recorre TODAS las rutas GET
sin parámetros de ruta publicadas en /openapi.json y comprueba que:
  1. ningún `id` aparece en las respuestas de los dos clubes (salvo catálogos compartidos);
  2. cada club recibe 403/404 al pedir por id un recurso del otro (GET /ruta/{id}).

Uso (NO pongas contraseñas en el repo; pásalas por entorno):
    BASE_URL=http://localhost:8000 \\
    A_EMAIL=admin@club-a.com A_PASS=... B_EMAIL=admin@club-b.com B_PASS=... \\
    python tests/isolation_check.py

Código de salida 0 = todo aislado; 1 = hay fugas (se listan las rutas).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Iterable

import requests

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
API = f"{BASE}/api"

# Catálogos que legítimamente comparten ids entre clubes (deportes, marca por defecto...).
SHARED_PATHS = [r"^/sports", r"^/branding$", r"^/health", r"^/$", r"^/auth/"]
# Rutas GET que no queremos golpear (descargas pesadas, PDFs, exportaciones, docs).
SKIP_PATHS = [r"\.xlsx$", r"\.pdf$", r"/pdf", r"/export", r"/openapi", r"/docs", r"/redoc", r"/rffm/sync", r"/logo"]


def login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=60)
    if r.status_code != 200:
        sys.exit(f"No se pudo iniciar sesión como {email}: {r.status_code} {r.text[:200]}")
    # La sesión va en cookies HttpOnly; si el backend las marca Secure y BASE_URL es http://,
    # requests no las reenvía. En ese caso usamos el token como cabecera Bearer.
    tok = (r.json() or {}).get("access_token") or r.cookies.get("access_token") or s.cookies.get("access_token")
    if tok:
        s.headers["Authorization"] = f"Bearer {tok}"
    return s


def collect_ids(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        v = obj.get("id")
        if isinstance(v, str) and len(v) >= 8:
            out.add(v)
        for x in obj.values():
            collect_ids(x, out)
    elif isinstance(obj, list):
        for x in obj:
            collect_ids(x, out)


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(re.search(p, path) for p in patterns)


def main() -> int:
    env = {k: os.environ.get(k) for k in ("A_EMAIL", "A_PASS", "B_EMAIL", "B_PASS")}
    if not all(env.values()):
        sys.exit("Faltan A_EMAIL, A_PASS, B_EMAIL, B_PASS en el entorno.")
    a, b = login(env["A_EMAIL"], env["A_PASS"]), login(env["B_EMAIL"], env["B_PASS"])

    spec = requests.get(f"{BASE}/openapi.json", timeout=60).json()
    paths = spec.get("paths", {})
    prefix = "/api"
    simple, with_param = [], []
    for path, ops in paths.items():
        if "get" not in ops:
            continue
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if matches_any(rel, SKIP_PATHS):
            continue
        if "{" not in rel:
            simple.append(rel)
        elif rel.count("{") == 1 and rel.endswith("}"):
            with_param.append(rel)

    leaks: list[str] = []
    ids_a: dict[str, set[str]] = {}
    ids_b: dict[str, set[str]] = {}
    checked = 0
    for rel in sorted(simple):
        ra, rb = a.get(API + rel, timeout=60), b.get(API + rel, timeout=60)
        if ra.status_code >= 500 or rb.status_code >= 500:
            print(f"  [!] {rel}: error de servidor ({ra.status_code}/{rb.status_code})")
            continue
        sa, sb = set(), set()
        for resp, acc in ((ra, sa), (rb, sb)):
            if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
                try:
                    collect_ids(resp.json(), acc)
                except ValueError:
                    pass
        ids_a[rel], ids_b[rel] = sa, sb
        checked += 1
        common = sa & sb
        if common and not matches_any(rel, SHARED_PATHS):
            leaks.append(f"{rel}: {len(common)} ids visibles para AMBOS clubes (p. ej. {sorted(common)[:2]})")

    # 2) Acceso cruzado por id: B pide a A sus recursos, y al revés.
    all_a = set().union(*ids_a.values()) if ids_a else set()
    all_b = set().union(*ids_b.values()) if ids_b else set()
    cross_checked = 0
    for rel in sorted(with_param):
        for owner_ids, other, name in ((all_a - all_b, b, "B->A"), (all_b - all_a, a, "A->B")):
            for rid in list(owner_ids)[:3]:
                url = API + re.sub(r"\{[^}]+\}", rid, rel)
                r = other.get(url, timeout=60)
                cross_checked += 1
                if r.status_code == 200 and not matches_any(rel, SHARED_PATHS):
                    body = r.text[:120].replace("\n", " ")
                    leaks.append(f"{name} GET {rel}: devolvió 200 con datos de otro club ({body})")
                    break

    print(f"Rutas GET revisadas: {checked} · accesos cruzados por id: {cross_checked}")
    if leaks:
        print("\nFUGAS DETECTADAS:")
        for l in leaks:
            print("  ✗", l)
        return 1
    print("✓ Aislamiento correcto: ningún dato compartido entre los dos clubes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
