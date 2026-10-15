#!/bin/sh
# arriendo-bot: cada semana abre una búsqueda real en cada portal y comprueba que todavía se puede
# leer. Si un portal cambió su página, avisa por el scorer (Telegram + franja del mapa) antes de que
# pasen 24 h sin anuncios. Va en /etc/cron.weekly/ del LXC.
set -u
cd /opt/arriendo-bot
salida=$(docker compose exec -T -w /fredy -e TEST_VIVO=1 fredy node --test test-arriendo/portales.test.mjs 2>&1)
if ! echo "$salida" | grep -q "^# fail 0"; then
  fallos=$(echo "$salida" | grep -E "^not ok|AssertionError|no devolvió" | head -3 | tr '\n' ' ' | tr -d '"\\')
  TOKEN=$(grep ^ARRIENDO_TOKEN .env | cut -d= -f2)
  curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"clave\": \"portales\", \"mensaje\": \"La prueba semanal de portales falló: $fallos\"}" localhost:8000/api/aviso >/dev/null
  echo "FALLO: $fallos"; exit 1
fi
echo "Portales OK"
