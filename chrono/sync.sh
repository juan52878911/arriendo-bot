#!/usr/bin/env sh
# Trae el historial del mercado desde el scorer y lo indexa con chrono, para preguntar cosas como:
#   chrono top entity                     # barrios con más movimiento
#   chrono top level                      # nuevos, bajas, subidas, retirados
#   chrono timeline --by level --bucket 1d
#   chrono events --level baja            # todas las bajadas de precio
# Uso: sh chrono/sync.sh [url-del-scorer]   (por defecto $ARRIENDO_WEB_URL o http://localhost:8000)
set -eu

SCORER="${1:-${ARRIENDO_WEB_URL:-http://localhost:8000}}"
cd "$(dirname "$0")"

curl -fsS "$SCORER/api/historial.jsonl" -o historial.jsonl
if [ -d .chrono ]; then
  chrono sync
else
  chrono init historial.jsonl
fi
echo "Historial indexado: $(wc -l < historial.jsonl) eventos"
