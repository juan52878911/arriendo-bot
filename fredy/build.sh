#!/usr/bin/env sh
# Prepara el código de Fredy con el soporte para Colombia y deja listo fredy/upstream para
# `docker compose build fredy`. Fredy se fija a un commit para que una actualización suya no
# cambie el comportamiento sin que lo decidamos.
set -eu

FREDY_REPO="https://github.com/orangecoding/fredy.git"
FREDY_COMMIT="8b5900920e857b27d5d76db4646a62a04bea695a" # v29.2.1, 2026-10-13

cd "$(dirname "$0")"

if [ ! -d upstream/.git ]; then
  git clone -q "$FREDY_REPO" upstream
fi
git -C upstream fetch -q origin "$FREDY_COMMIT"
git -C upstream checkout -q --force "$FREDY_COMMIT"
git -C upstream clean -qfd

# 1. Los portales.
for portal in fincaraiz metrocuadrado ciencuadras; do
  cp "$portal.js" "upstream/lib/provider/$portal.js"
done

# 2. Que la interfaz lo ordene y que el mapa pueda ir a Colombia.
sed -i.bak "s|^  // Switzerland|  // Colombia\n  'fincaraiz',\n  'metrocuadrado',\n  'ciencuadras',\n  // Switzerland|" upstream/ui/src/services/providerOrder.js
sed -i.bak "s|^export const COUNTRY_BOUNDS = {|export const COUNTRY_BOUNDS = {\n  co: [\n    [-79.1, -4.3],\n    [-66.8, 12.6],\n  ],|" upstream/ui/src/components/map/countryBounds.js
rm -f upstream/ui/src/services/providerOrder.js.bak upstream/ui/src/components/map/countryBounds.js.bak

grep -q "'fincaraiz'" upstream/ui/src/services/providerOrder.js
grep -q "^  co: \[" upstream/ui/src/components/map/countryBounds.js
echo "Fredy listo en fredy/upstream ($FREDY_COMMIT) con Fincaraíz, Metrocuadrado y Ciencuadras"
