/*
 * Copyright (c) 2026 by Christian Kellner.
 * Licensed under Apache-2.0 with Commons Clause and Attribution/Naming Clause
 */

/**
 * Fincaraíz, the largest Colombian portal, read from the data its own search page embeds.
 *
 * Every search page is a Next.js page that ships its results as JSON in `__NEXT_DATA__`
 * (`props.pageProps.fetchResult.searchFast.data`), with coordinates, bedrooms, bathrooms, stratum,
 * administration fee, amenities and the photo gallery already in it. One request per page is
 * therefore the whole cost of a run: no browser and no detail page.
 *
 * The search page sorts by popularity and the URL carries no sort parameter, so a run reads the
 * first `MAX_PAGES` pages and orders the result by publication date itself. A search narrowed to a
 * neighbourhood (`/arriendo/apartamentos/<barrio>/<zona>/bogota`) rarely has more than that.
 *
 * Fredy's listing model has no room for bathrooms, stratum or the fee, which is what the arriendo-bot
 * scorer needs. When `ARRIENDO_RAW_URL` is set, the raw records of each run are POSTed there as well
 * - the same data already downloaded, never an extra request to the portal.
 */

import { buildHash, isOneOf } from '../utils.js';
import logger from '../services/logger.js';
import { publicationDate } from '../utils/publicationDate.js';
/** @import { ParsedListing } from '../types/listing.js' */
/** @import { ProviderConfig } from '../types/providerConfig.js' */

const BASE_URL = 'https://www.fincaraiz.com.co';

/** Pages read per run. Twenty-one listings each. */
const MAX_PAGES = Number(process.env.FINCARAIZ_MAX_PAGES ?? 3);

/** Pause between two pages of the same search, so a run never looks like a burst. */
const PAGE_DELAY_MS = 2500;

const REQUEST_HEADERS = {
  Accept: 'text/html,application/xhtml+xml',
  'Accept-Language': 'es-CO,es;q=0.9',
  'User-Agent':
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
};

const NEXT_DATA = /<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * The URL of page `n` of a search. Page one is the URL as pasted; later pages append `/paginaN`.
 *
 * @param {string} url
 * @param {number} n
 * @returns {string}
 */
export function pageUrl(url, n) {
  const parsed = new URL(url);
  const path = parsed.pathname.replace(/\/pagina\d+\/?$/, '').replace(/\/$/, '');
  parsed.pathname = n <= 1 ? path : `${path}/pagina${n}`;
  return parsed.toString();
}

/**
 * The search result embedded in one page, or null when the page does not carry one.
 *
 * @param {string} html
 * @returns {{data: Object[], hasMorePages: boolean}|null}
 */
export function parseSearchPage(html) {
  const match = NEXT_DATA.exec(html);
  if (match == null) {
    return null;
  }
  const search = JSON.parse(match[1])?.props?.pageProps?.fetchResult?.searchFast;
  if (search == null || !Array.isArray(search.data)) {
    return null;
  }
  return { data: search.data, hasMorePages: search.paginatorInfo?.hasMorePages === true };
}

/**
 * Hand the raw records to the scorer. Fire and forget: a scorer that is down must never cost Fredy
 * its run.
 *
 * @param {Object[]} records
 * @param {string} searchUrl
 */
function shareRaw(records, searchUrl) {
  const target = process.env.ARRIENDO_RAW_URL;
  if (!target || records.length === 0) {
    return;
  }
  const headers = { 'Content-Type': 'application/json' };
  if (process.env.ARRIENDO_TOKEN) {
    headers.Authorization = `Bearer ${process.env.ARRIENDO_TOKEN}`;
  }
  fetch(target, {
    method: 'POST',
    headers,
    body: JSON.stringify({ portal: 'fincaraiz', searchUrl, records }),
  }).catch((err) => logger.warn(`Could not hand Fincaraíz records to the scorer: ${err.message}`));
}

/**
 * @param {string} url The job's search URL.
 * @returns {Promise<Object[]>}
 */
async function getListings(url) {
  const seen = new Map();
  for (let n = 1; n <= MAX_PAGES; n++) {
    if (n > 1) {
      await sleep(PAGE_DELAY_MS);
    }
    const response = await fetch(pageUrl(url, n), { headers: REQUEST_HEADERS });
    if (!response.ok) {
      logger.error(`Error fetching Fincaraíz page ${n}: ${response.status} ${response.statusText}`);
      break;
    }
    const page = parseSearchPage(await response.text());
    if (page == null) {
      logger.error(`Fincaraíz page ${n} carried no search result; the page layout may have changed`);
      break;
    }
    for (const record of page.data) {
      if (record?.id != null) {
        seen.set(record.id, record);
      }
    }
    if (!page.hasMorePages) {
      break;
    }
  }

  const records = [...seen.values()].sort((a, b) =>
    String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')),
  );
  shareRaw(records, url);
  return records;
}

/**
 * One value of the listing's technical sheet, as a number when it reads as one.
 *
 * @param {Object} o
 * @param {string} field
 * @returns {number|null}
 */
export function sheetNumber(o, field) {
  const entry = (o.technicalSheet ?? []).find((item) => item?.field === field);
  if (entry == null || entry.value == null || entry.value === '') {
    return null;
  }
  const digits = String(entry.value).replace(/[^\d]/g, '');
  return digits.length > 0 ? Number(digits) : null;
}

/**
 * `created_at` is a bare date; noon in Bogotá keeps it on the same calendar day everywhere.
 *
 * @param {string|undefined} day
 * @returns {number|undefined}
 */
function publishedAt(day) {
  return typeof day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(day)
    ? publicationDate(`${day}T12:00:00-05:00`)
    : undefined;
}

/**
 * @param {any} o
 * @returns {ParsedListing}
 */
export function normalize(o) {
  const rent = o.price?.amount ?? null;
  // `admin_included` is the rent with the administration fee in it; where the advert states no
  // fee it equals the rent.
  const total = o.price?.admin_included ?? rent;
  const fee = sheetNumber(o, 'commonExpenses');
  return {
    id: buildHash(String(o.id), String(total)),
    title: o.title,
    link: o.link ? `${BASE_URL}${o.link}` : null,
    price: total,
    chargesIncluded: total != null && rent != null && total > rent ? true : undefined,
    charges: fee ?? (total != null && rent != null && total > rent ? total - rent : undefined),
    size: o.m2apto || o.m2Built || o.m2 || null,
    rooms: sheetNumber(o, 'bedrooms'),
    address: o.address ?? o.locations?.location_main?.name ?? null,
    description: o.description,
    publishedAt: publishedAt(o.created_at),
    image: o.img ?? null,
    images: (o.images ?? []).slice(0, 20).map((image) => ({ url: image.image })),
    latitude: typeof o.latitude === 'number' ? o.latitude : undefined,
    longitude: typeof o.longitude === 'number' ? o.longitude : undefined,
  };
}

/**
 * @param {ParsedListing} o
 * @param {string[]} appliedBlackList
 * @returns {boolean}
 */
function applyBlacklist(o, appliedBlackList) {
  const titleNotBlacklisted = !isOneOf(o.title, appliedBlackList);
  const descNotBlacklisted = !isOneOf(o.description, appliedBlackList);
  return o.title != null && titleNotBlacklisted && descNotBlacklisted;
}

/** @type {ProviderConfig} */
const config = {
  url: null,
  requiredFieldNames: ['id', 'title', 'link', 'price', 'address'],
  crawlContainer: null,
  crawlFields: {},
  // The URL takes no sort parameter; getListings orders the results itself.
  sortByDateParam: null,
  getListings,
  normalize,
};

export const metaInformation = {
  countries: ['co'],
  name: 'Fincaraíz',
  baseUrl: `${BASE_URL}/`,
  hosts: ['www.fincaraiz.com.co', 'fincaraiz.com.co'],
  id: 'fincaraiz',
};

/**
 * Build a run-scoped provider configuration.
 *
 * @param {{url: string, enabled?: boolean}} sourceConfig
 * @param {string[]} [blacklist]
 * @returns {ProviderConfig}
 */
export const createConfig = (sourceConfig, blacklist = []) => ({
  ...config,
  enabled: sourceConfig.enabled,
  url: sourceConfig.url,
  filter: (listing) => applyBlacklist(listing, blacklist ?? []),
});

export { config };
