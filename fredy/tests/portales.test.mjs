/*
 * Pruebas de los tres portales, dentro del contenedor de Fredy (build.sh las copia a /fredy/test-arriendo):
 *
 *   docker compose exec -w /fredy fredy node --test test-arriendo/portales.test.mjs          # sin red: páginas guardadas
 *   docker compose exec -w /fredy -e TEST_VIVO=1 fredy node --test test-arriendo/portales.test.mjs   # contra los portales
 *
 * Sin red se prueba el código (lectura y normalización). En vivo se prueba que el portal no haya
 * cambiado su página: una búsqueda real por portal debe traer anuncios con precio, enlace y coordenadas.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const VIVO = process.env.TEST_VIVO === '1';
const fixture = (nombre) => readFileSync(new URL(`./fixtures/${nombre}.html`, import.meta.url), 'utf8');

// Búsquedas reales pequeñas para el modo en vivo.
const PORTALES = {
  fincaraiz: 'https://www.fincaraiz.com.co/arriendo/apartamentos/teusaquillo/bogota/3-o-mas-habitaciones/hasta-3000000?ordenListado=3',
  metrocuadrado: 'https://www.metrocuadrado.com/apartamentos/arriendo/bogota/teusaquillo/3-habitaciones/',
  ciencuadras: 'https://www.ciencuadras.com/arriendo/apartamento/bogota/chapinero',
};

const enBogota = (l) => l.latitude > 4.4 && l.latitude < 4.9 && l.longitude > -74.3 && l.longitude < -73.9;

for (const [portal, url] of Object.entries(PORTALES)) {
  test(`${portal}: lee la página y normaliza los anuncios`, async () => {
    const original = globalThis.fetch;
    if (!VIVO) {
      globalThis.fetch = async () => ({ ok: true, status: 200, statusText: 'OK', text: async () => fixture(portal) });
    }
    try {
      delete process.env.ARRIENDO_RAW_URL; // la prueba no debe escribir en el scorer
      process.env.FINCARAIZ_MAX_PAGES = '1';
      const p = await import(`../lib/provider/${portal}.js`);
      const cfg = p.createConfig({ url, enabled: true });
      const crudos = await cfg.getListings(url);
      assert.ok(crudos.length > 0, `${portal} no devolvió anuncios: la página pudo cambiar`);
      const anuncios = crudos.map(cfg.normalize).filter(cfg.filter);
      assert.equal(anuncios.length, crudos.length, 'algún anuncio no pasó el filtro de campos');
      for (const a of anuncios) {
        assert.ok(a.id && a.title, 'falta id o título');
        assert.match(a.link, /^https:\/\/www\.(fincaraiz\.com\.co|metrocuadrado\.com|ciencuadras\.com)\//);
        assert.ok(Number.isFinite(a.price) && a.price > 100_000, `precio raro: ${a.price}`);
      }
      assert.ok(anuncios.filter(enBogota).length >= anuncios.length / 2, 'la mayoría debería tener coordenadas en Bogotá');
    } finally {
      globalThis.fetch = original;
    }
  });
}

test('fincaraiz: la página N añade /paginaN a la ruta y conserva los filtros', async () => {
  const { pageUrl } = await import('../lib/provider/fincaraiz.js');
  const u = 'https://www.fincaraiz.com.co/arriendo/apartamentos/chapinero/bogota/3-o-mas-habitaciones?ordenListado=3';
  assert.equal(pageUrl(u, 1), u);
  assert.equal(pageUrl(u, 2), 'https://www.fincaraiz.com.co/arriendo/apartamentos/chapinero/bogota/3-o-mas-habitaciones/pagina2?ordenListado=3');
});

test('ciencuadras: la rotación recorre todos los barrios sin repetir', async () => {
  const { pagesForRun } = await import('../lib/provider/ciencuadras.js');
  const barrios = Array.from({ length: 25 }, (_, i) => `b${i}`);
  const url = `https://www.ciencuadras.com/arriendo/apartamento/bogota/teusaquillo?barrios=${barrios.join(',')}`;
  const vistos = [];
  for (let hora = 0; hora < 3; hora++) {
    const paginas = pagesForRun(url, new Date(hora * 3_600_000));
    assert.ok(paginas.length <= 10);
    vistos.push(...paginas.map((p) => p.split('/').pop()));
  }
  assert.deepEqual([...vistos].sort(), [...barrios].sort());
  assert.deepEqual(pagesForRun('https://www.ciencuadras.com/arriendo/apartamento/bogota/chapinero'),
    ['https://www.ciencuadras.com/arriendo/apartamento/bogota/chapinero']);
});
