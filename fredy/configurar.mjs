/*
 * Configura Fredy sin pasar por la interfaz: canal HTTP hacia el scorer, un job por barrio y los
 * ajustes generales. Idempotente (ids fijos): se puede volver a correr tras editar la lista.
 *
 *   docker compose cp fredy/configurar.mjs fredy:/fredy/configurar.mjs
 *   docker compose exec -w /fredy fredy node configurar.mjs
 *   docker compose restart fredy
 */

import SqliteConnection from './lib/services/storage/SqliteConnection.js';
import { upsertChannel } from './lib/services/storage/configuredAdapterStorage.js';
import { upsertJob } from './lib/services/storage/jobStorage.js';
import { upsertSettings } from './lib/services/storage/settingsStorage.js';

// Una URL por portal y barrio. Cada portal filtra distinto:
// - Fincaraíz: 3+ habitaciones, hasta $3.000.000 y más recientes primero, todo en la URL.
// - Metrocuadrado: 3 habitaciones en la ruta; ordena por relevancia y trae hasta 50.
// - Ciencuadras: solo el barrio (el servidor ignora los filtros); trae los 20 más recientes.
// null = el portal no reconoce ese barrio (lo comprobamos el 2026-10-14).
const fincaraiz = (slug) => `https://www.fincaraiz.com.co/arriendo/apartamentos/${slug}/bogota/3-o-mas-habitaciones/hasta-3000000?ordenListado=3`;
const metrocuadrado = (slug) => `https://www.metrocuadrado.com/apartamentos/arriendo/bogota/${slug}/3-habitaciones/`;
const ciencuadras = (slug) => `https://www.ciencuadras.com/arriendo/apartamento/bogota/${slug}`;

const BARRIOS = [];

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
    cc && { id: 'ciencuadras', name: 'Ciencuadras', url: ciencuadras(cc), enabled: true },
  ].filter(Boolean);
  upsertJob({
    jobId: `arriendo-${fc.split('/')[0]}`,
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
