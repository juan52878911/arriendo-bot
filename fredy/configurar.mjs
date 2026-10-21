/*
 * Configura Fredy sin pasar por la interfaz: canal HTTP hacia el scorer, un job por barrio y los
 * ajustes generales. Idempotente (ids fijos): se puede volver a correr tras editar la lista.
 *
 *   docker compose cp fredy/configurar.mjs fredy:/fredy/configurar.mjs
 *   docker compose cp config/busquedas.json fredy:/fredy/busquedas.json
 *   docker compose exec -w /fredy fredy node configurar.mjs
 *   docker compose restart fredy
 */

import { readFileSync } from 'node:fs';
import SqliteConnection from './lib/services/storage/SqliteConnection.js';
import { upsertChannel } from './lib/services/storage/configuredAdapterStorage.js';
import { upsertJob } from './lib/services/storage/jobStorage.js';
import { upsertSettings } from './lib/services/storage/settingsStorage.js';

// Una URL por portal y barrio. Cada portal filtra distinto:
// - Fincaraíz: N+ habitaciones y más recientes primero, sin tope de precio: los más caros sirven de
//   referencia de mercado (el puntaje sigue filtrando por presupuesto).
// - Metrocuadrado: N habitaciones en la ruta; ordena por relevancia y trae hasta 50.
// - Ciencuadras: solo el barrio (el servidor ignora los filtros); trae los 20 más recientes.
// HABITACIONES (por defecto 3) es el mínimo de habitaciones de tu búsqueda.
const HAB = Number(process.env.HABITACIONES ?? 3);
const fincaraiz = (slug) => `https://www.fincaraiz.com.co/arriendo/apartamentos/${slug}/bogota/${HAB}-o-mas-habitaciones?ordenListado=3`;
const metrocuadrado = (slug) => `https://www.metrocuadrado.com/apartamentos/arriendo/bogota/${slug}/${HAB}-habitaciones/`;
const ciencuadras = (slug) => `https://www.ciencuadras.com/arriendo/apartamento/bogota/${slug}`;

// Los barrios vienen de config/busquedas.json (privado; plantilla en config/busquedas.ejemplo.json):
//   { "ciencuadras_localidades": { "<localidad>": ["<barrio>", …] },
//     "barrios": [{ "nombre": …, "fincaraiz": <slug|null>, "metrocuadrado": <slug|null>, "ciencuadras": <slug|null> }] }
// Ciencuadras solo muestra 20 anuncios por búsqueda y no pagina: si su slug es una localidad de
// `ciencuadras_localidades`, se lee barrio a barrio, rotando 10 por pasada (cada barrio cada ~3-4 h).
// null = el portal no reconoce ese barrio o ya lo cubre otra búsqueda.
const { ciencuadras_localidades: CC_BARRIOS = {}, barrios } = JSON.parse(
  readFileSync(process.env.BUSQUEDAS ?? new URL('./busquedas.json', import.meta.url), 'utf8'));
const ciencuadrasLocalidad = (slug) => `${ciencuadras(slug)}?barrios=${CC_BARRIOS[slug].join(',')}`;
const BARRIOS = barrios.map((b) => [b.nombre, b.fincaraiz, b.metrocuadrado, b.ciencuadras]);

await SqliteConnection.init();
const admin = SqliteConnection.query(`SELECT id FROM users WHERE is_admin = 1 ORDER BY username LIMIT 1`)[0];
if (!admin) throw new Error('Fredy no tiene usuario administrador');

const canal = upsertChannel({
  id: 'arriendo-scorer',
  userId: admin.id,
  adapterId: 'http',
  name: 'Scorer arriendo-bot',
  fields: {
    endpointUrl: 'http://scorer:8000/webhook',
    authToken: process.env.ARRIENDO_TOKEN,
    selfSignedCerts: false,
  },
});

for (const [nombre, fc, m2, cc] of BARRIOS) {
  const provider = [
    fc && { id: 'fincaraiz', name: 'Fincaraíz', url: fincaraiz(fc), enabled: true },
    m2 && { id: 'metrocuadrado', name: 'Metrocuadrado', url: metrocuadrado(m2), enabled: true },
    cc && { id: 'ciencuadras', name: 'Ciencuadras', url: CC_BARRIOS[cc] ? ciencuadrasLocalidad(cc) : ciencuadras(cc), enabled: true },
  ].filter(Boolean);
  upsertJob({
    jobId: `arriendo-${(fc ?? m2 ?? cc).split("/")[0]}`,
    name: `Arriendo · ${nombre}`,
    userId: admin.id,
    enabled: true,
    dealType: 'rent',
    provider,
    notificationAdapter: [{ configuredAdapterId: canal }],
  });
}

upsertSettings({
  interval: '60',                      // minutos entre pasadas
  baseUrl: process.env.FREDY_BASE_URL, // para que los enlaces de Fredy abran desde la red local
  analyticsEnabled: false,             // sin telemetría hacia el autor de Fredy
  connectivityEnabled: false,          // banda ancha de Alemania y Suiza: no aplica aquí
});

console.log(`Canal ${canal} y ${BARRIOS.length} jobs listos para el usuario ${admin.id}`);
