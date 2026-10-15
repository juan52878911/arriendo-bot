#!/bin/sh
# arriendo-bot: comprueba cada semana que los respaldos del NAS se pueden restaurar.
# - PostGIS: restaura el último .dump en una base temporal y compara el conteo de cada tabla.
# - Fredy: integrity_check del último .db y conteo de anuncios y búsquedas.
# Si algo falla, avisa por el scorer (Telegram + franja del mapa). Va en /etc/cron.weekly/ del LXC.
set -u
cd /opt/arriendo-bot
TOKEN=$(grep ^ARRIENDO_TOKEN .env | cut -d= -f2)
fallo() {
  curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"clave\": \"respaldos\", \"mensaje\": \"La prueba semanal de respaldos falló: $1\"}" \
    localhost:8000/api/aviso >/dev/null
  python3 /opt/arriendo-bot/servidor/analisis_chrono.py tarea probar-respaldos ERROR "$1"
  echo "FALLO: $1"
  exit 1
}

DUMP=$(ls -t /data/respaldos/arriendo-*.dump 2>/dev/null | head -1)
[ -n "$DUMP" ] || fallo "no hay respaldos de PostGIS"
docker compose exec -T respaldo sh -c "dropdb -h db -U arriendo --if-exists restaurado && createdb -h db -U arriendo restaurado && pg_restore -h db -U arriendo -d restaurado --no-owner /respaldos/$(basename "$DUMP")" >/dev/null 2>&1 \
  || fallo "pg_restore de $(basename "$DUMP") no terminó"
for t in inmueble precio sector imagen foto_url etiqueta_img; do
  vivo=$(docker compose exec -T db psql -U arriendo -d arriendo -Atc "select count(*) from $t")
  copia=$(docker compose exec -T db psql -U arriendo -d restaurado -Atc "select count(*) from $t" 2>/dev/null)
  # El respaldo es de hasta un día antes: puede tener menos filas, nunca ninguna si la tabla viva tiene.
  [ -n "$copia" ] && { [ "$vivo" -eq 0 ] || [ "$copia" -gt 0 ]; } || fallo "tabla $t: viva $vivo, restaurada '$copia'"
done
docker compose exec -T db dropdb -U arriendo restaurado

DB=$(ls -t /data/respaldos/fredy-*.db 2>/dev/null | head -1)
[ -n "$DB" ] || fallo "no hay respaldos de Fredy"
r=$(docker compose exec -T respaldo-fredy sqlite3 "/respaldos/$(basename "$DB")" "pragma integrity_check; select count(*) from jobs;" 2>&1)
echo "$r" | head -1 | grep -qx ok || fallo "integridad de $(basename "$DB"): $r"
[ "$(echo "$r" | tail -1)" -gt 0 ] || fallo "$(basename "$DB") no tiene búsquedas"
python3 /opt/arriendo-bot/servidor/analisis_chrono.py tarea probar-respaldos INFO "restaurados $(basename "$DUMP") y $(basename "$DB")"
echo "Respaldos OK: $(basename "$DUMP"), $(basename "$DB")"
