#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Comprueba contra la API viva los cambios de esta tanda.
#
#   bash probar_cambios.sh            -> solo lectura, no modifica nada
#   bash probar_cambios.sh --escribir -> además prueba guardar una ficha y
#                                        crear/borrar un usuario de prueba
#
# Lee ADMIN_EMAIL y ADMIN_PASSWORD del .env del propio backend.
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")"

API="${API:-http://127.0.0.1:8000/api}"
ESCRIBIR=0
[ "${1:-}" = "--escribir" ] && ESCRIBIR=1

verde() { printf '\033[32m%s\033[0m\n' "$1"; }
rojo()  { printf '\033[31m%s\033[0m\n' "$1"; }
gris()  { printf '\033[90m%s\033[0m\n' "$1"; }
titulo() { echo; printf '\033[1m── %s\033[0m\n' "$1"; }

ok()   { verde "  ✓ $1"; }
fallo(){ rojo  "  ✗ $1"; }

envget() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '"'"'"''; }

ADMIN_EMAIL=$(envget ADMIN_EMAIL)
ADMIN_PASSWORD=$(envget ADMIN_PASSWORD)
if [ -z "$ADMIN_EMAIL" ] || [ -z "$ADMIN_PASSWORD" ]; then
  rojo "Falta ADMIN_EMAIL o ADMIN_PASSWORD en .env"; exit 1
fi

JQ=$(command -v jq || true)
j() { if [ -n "$JQ" ]; then jq -r "$1"; else cat; fi; }

COOK=$(mktemp)
trap 'rm -f "$COOK" "${COOK}.coord" 2>/dev/null' EXIT
ORIG=""

titulo "1. Salud y login"
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$API/health")
if [ "$code" = "200" ]; then
  ok "/health responde 200"
elif [ "$code" = "000" ]; then
  fallo "no hay nadie escuchando en $API"
  echo
  gris "  El backend no está arrancado. Ojo: si lo lanzaste en ESTA misma"
  gris "  terminal, al recuperar el prompt con Ctrl+C lo mataste."
  gris "  Deja 'npm start' en una pestaña y ejecuta este script en otra."
  echo
  gris "  Comprobar:  lsof -ti tcp:8000"
  exit 1
else
  fallo "/health devolvió $code"; exit 1
fi

login=$(curl -s -m 20 -c "$COOK" -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}")
rol=$(echo "$login" | j '.role // empty')
if [ -n "$rol" ]; then ok "login como $ADMIN_EMAIL (rol: $rol)"
else fallo "login falló: $(echo "$login" | head -c 200)"; exit 1; fi

titulo "2. Equipos: el catálogo que ahora usa la interfaz"
teams=$(curl -s -m 20 -b "$COOK" "$API/teams")
n=$(echo "$teams" | j 'length // 0')
if [ "${n:-0}" -gt 0 ] 2>/dev/null; then
  ok "/teams devuelve $n equipos"
  gris "     $(echo "$teams" | j '[.[].nombre] | join(", ")' | head -c 160)"
else
  fallo "/teams devolvió 0 equipos → el selector caerá al texto libre de los jugadores"
  gris "     $(echo "$teams" | head -c 200)"
fi

titulo "3. Ficha de oficina: tutores, banco y empadronamiento"
players=$(curl -s -m 30 -b "$COOK" "$API/players")
np=$(echo "$players" | j 'length // 0')
ok "/players devuelve $np jugadores"
pid=$(echo "$players" | j '.[0].id // empty')
if [ -z "$pid" ]; then fallo "sin jugadores, no puedo seguir"; exit 1; fi
nombre=$(echo "$players" | j '.[0].name')
gris "     jugador de prueba: $nombre"

campos=$(echo "$players" | j '.[0] | {guardians: (.guardians|type), bank: (.bank|type), empadronado: (.empadronado|type)}' | tr -d '\n ')
if echo "$campos" | grep -q '"guardians":"array"'; then ok "el jugador trae 'guardians'"; else fallo "no llega 'guardians' → mira PLAYERS_SELECT_SQL"; fi
if echo "$campos" | grep -q '"bank":"object"'; then ok "el jugador trae 'bank' (admin ve el IBAN entero)"; else fallo "no llega 'bank'"; fi
if echo "$campos" | grep -q '"empadronado":"boolean"'; then ok "el jugador trae 'empadronado'"; else fallo "no llega 'empadronado'"; fi

titulo "4. Roles asignables (coordinación no debe poder crear dirección)"
roles=$(curl -s -m 20 -b "$COOK" "$API/users/assignable-roles" | j '.roles | join(", ")')
ok "dirección puede asignar: $roles"
echo "$roles" | grep -q admin && ok "incluye admin, correcto para dirección" || fallo "dirección debería poder asignar admin"

if [ "$ESCRIBIR" = "0" ]; then
  titulo "Fin (solo lectura)"
  gris "Para probar guardado y permisos:  bash probar_cambios.sh --escribir"
  exit 0
fi

titulo "5. Guardar ficha de oficina  [escribe en la base]"
# Copia de la ficha original ANTES de tocarla. La primera versión de este
# script sobrescribía los datos reales del jugador sin guardarlos, y hubo que
# recuperarlos del Excel a mano.
ORIG=$(mktemp)
curl -s -m 30 -b "$COOK" "$API/players" \
  | j ".[] | select(.id==\"$pid\") | {guardians, bank, empadronado}" > "$ORIG"
gris "     ficha original guardada en $ORIG"

guardado=$(curl -s -m 20 -b "$COOK" -X POST "$API/players/$pid/office" \
  -H 'Content-Type: application/json' -d '{
    "guardians":[{"orden":1,"nombre":"PRUEBA Ana Tutora","dni":"00000000T","telefono":"600000000","email":"prueba@ejemplo.com","titular_pago":true}],
    "bank":{"entidad":"Banco de Prueba","titular":"PRUEBA Ana Tutora","iban":"ES9121000418450200051332","paga_en_tienda":false},
    "empadronado":true}')
if echo "$guardado" | j '.guardians[0].nombre' | grep -q "PRUEBA"; then
  ok "guardado y releído: $(echo "$guardado" | j '.guardians[0].nombre + " / " + .guardians[0].dni')"
  ok "IBAN que ve dirección: $(echo "$guardado" | j '.bank.iban')"
  ok "empadronado: $(echo "$guardado" | j '.empadronado')"
else
  fallo "no se guardó: $(echo "$guardado" | head -c 300)"
fi

titulo "6. Coordinación: ve enmascarado y no puede editar  [crea un usuario temporal]"
PW="Prueba$RANDOM$RANDOM"
EMAIL="prueba.coord.$RANDOM@ejemplo.com"
nuevo=$(curl -s -m 30 -b "$COOK" -X POST "$API/users" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\",\"name\":\"Coord Prueba\",\"role\":\"coordinator\",\"assigned_teams\":[]}")
uid=$(echo "$nuevo" | j '.id // empty')
if [ -z "$uid" ]; then
  fallo "no se pudo crear el usuario: $(echo "$nuevo" | head -c 250)"
else
  ok "usuario coordinador creado"
  sleep 1
  cl=$(curl -s -m 20 -c "${COOK}.coord" -X POST "$API/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\"}")
  if echo "$cl" | j '.role' | grep -q coordinator; then
    ok "login como coordinación"
    iban=$(curl -s -m 30 -b "${COOK}.coord" "$API/players" | j ".[] | select(.id==\"$pid\") | .bank.iban")
    if echo "$iban" | grep -q '••••'; then ok "ve el IBAN enmascarado: $iban"
    else fallo "coordinación ve el IBAN sin enmascarar: $iban"; fi

    code=$(curl -s -m 20 -b "${COOK}.coord" -o /dev/null -w '%{http_code}' -X POST "$API/players/$pid/office" \
      -H 'Content-Type: application/json' -d '{"empadronado":false}')
    [ "$code" = "403" ] && ok "editar oficina le devuelve 403" || fallo "esperaba 403 al editar, devolvió $code"

    code=$(curl -s -m 20 -b "${COOK}.coord" -o /dev/null -w '%{http_code}' -X POST "$API/inventory" \
      -H 'Content-Type: application/json' -d '{"item":"prueba","quantity":1}')
    [ "$code" = "403" ] && ok "crear inventario le devuelve 403" || fallo "esperaba 403 en inventario, devolvió $code"

    rr=$(curl -s -m 20 -b "${COOK}.coord" "$API/users/assignable-roles" | j '.roles | join(", ")')
    if echo "$rr" | grep -q admin; then fallo "¡coordinación puede asignar admin! ($rr)"
    else ok "roles que puede asignar: $rr — sin admin"; fi
  else
    fallo "no pudo entrar el coordinador: $(echo "$cl" | head -c 200)"
  fi
  code=$(curl -s -m 30 -b "$COOK" -o /dev/null -w '%{http_code}' -X DELETE "$API/users/$uid")
  [ "$code" = "200" ] && ok "usuario de prueba eliminado" || fallo "no se borró el usuario $uid (HTTP $code) — bórralo a mano"
fi

titulo "7. Alta pública de club (debe estar cerrada)"
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "$API/auth/register" \
  -H 'Content-Type: application/json' -d '{"admin":{"name":"x","email":"x@x.com","password":"abcd1234"},"club":{"nombre":"x"},"teams":[{"local_id":"t1","nombre":"A"}]}')
[ "$code" = "404" ] && ok "responde 404, cerrada como acordamos" || fallo "esperaba 404, devolvió $code"

titulo "8. Restaurar la ficha original"
if [ -s "$ORIG" ] && [ -n "$JQ" ]; then
  payload=$(jq -c '{guardians: ((.guardians // []) + [{orden:1},{orden:2}] | group_by(.orden) | map(add) | map({orden, nombre: (.nombre // ""), dni: (.dni // ""), telefono: (.telefono // ""), email: (.email // ""), titular_pago: (.titular_pago // false)})), bank: (.bank // {entidad:"",titular:"",iban:"",paga_en_tienda:false}), empadronado: (.empadronado // false)}' "$ORIG")
  code=$(curl -s -m 20 -b "$COOK" -o /dev/null -w '%{http_code}' -X POST "$API/players/$pid/office" \
    -H 'Content-Type: application/json' -d "$payload")
  if [ "$code" = "200" ]; then
    ok "ficha de '$nombre' restaurada a su estado original"
  else
    fallo "no se pudo restaurar (HTTP $code). La copia está en $ORIG"
  fi
else
  fallo "sin jq no puedo restaurar. La copia está en $ORIG"
fi

titulo "Fin"
