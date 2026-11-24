// The test world: farmers with farms, listings and land, customers, delivery partners. Built once
// in setup() through the public API; idempotent, so a second run reuses the same accounts.
//
// Everything lives around Bilaspur (about 120 km from the real Durg data), so load-test listings
// do not show up for real users browsing near Durg, and customers only ever ORDER from load-test
// listings. Delivery partners ping their location far away (Patna), so they are never offered a
// real route.
import http from 'k6/http';
import { sleep } from 'k6';
import { BASE, PASSWORD, PHONE_PREFIX, json, login, point, request } from './api.js';

export const CENTER = { lat: 22.08, lng: 82.15 }; // Bilaspur, Chhattisgarh
export const PARTNER_AREA = { lat: 25.6, lng: 85.1 }; // Patna: far from every real partner

const CROPS = [
  ['tomato', 'Desi', 28],
  ['onion', 'Nashik Red', 32],
  ['potato', 'Jyoti', 24],
  ['okra', 'Pusa Sawani', 45],
  ['spinach', 'All Green', 30],
  ['brinjal', 'Long Purple', 36],
];

const phone = (kind, n) => `${PHONE_PREFIX}${kind}${String(n).padStart(5, '0')}`;
export const customerPhone = (n) => phone(1, n);
export const farmerPhone = (n) => phone(2, n);
export const partnerPhone = (n) => phone(3, n);

// The gateway allows 5 sign-ups / sign-ins a second per client IP (burst 10): stay under it.
const AUTH_BATCH = Number(__ENV.AUTH_BATCH || 4);
const AUTH_PAUSE = Number(__ENV.AUTH_PAUSE || 1.1);

function registerAll(people) {
  for (let i = 0; i < people.length; i += AUTH_BATCH) {
    if (i) sleep(AUTH_PAUSE);
    const batch = people.slice(i, i + AUTH_BATCH).map((p) => [
      'POST',
      `${BASE}/identity/register`,
      JSON.stringify({
        name: p.name,
        phone: p.phone,
        password: PASSWORD,
        roles: p.roles,
        homeLat: p.lat,
        homeLng: p.lng,
      }),
      {
        headers: { 'Content-Type': 'application/json' },
        tags: { name: 'setup register' },
        responseCallback: http.expectedStatuses(201, 409),
        timeout: '60s',
      },
    ]);
    const res = http.batch(batch);
    res.forEach((r, k) => {
      if (r.status !== 201 && r.status !== 409) {
        throw new Error(`register ${people[i + k].phone}: ${r.status} ${r.body}`);
      }
    });
  }
}

function offset(n, spread = 0.08) {
  // deterministic spread around the centre
  const a = (n * 137.508 * Math.PI) / 180;
  const r = spread * Math.sqrt((n % 50) / 50 + 0.02);
  return { lat: CENTER.lat + r * Math.sin(a), lng: CENTER.lng + r * Math.cos(a) };
}

export function buildWorld(cfg) {
  const admin = login(cfg.adminPhone, cfg.adminPassword);
  if (!admin) throw new Error('admin login failed: set ADMIN_PHONE / ADMIN_PASSWORD');

  const farmers = [];
  for (let i = 0; i < cfg.farmers; i++) {
    farmers.push({ phone: farmerPhone(i), name: `LoadTest Farmer ${i}`, roles: ['farmer'], ...offset(i) });
  }
  const customers = [];
  for (let i = 0; i < cfg.customers; i++) {
    customers.push({ phone: customerPhone(i), name: `LoadTest Customer ${i}`, roles: ['customer'], ...offset(i + 7, 0.12) });
  }
  const partners = [];
  for (let i = 0; i < cfg.partners; i++) {
    partners.push({ phone: partnerPhone(i), name: `LoadTest Partner ${i}`, roles: ['delivery'], lat: PARTNER_AREA.lat, lng: PARTNER_AREA.lng });
  }
  registerAll([...farmers, ...customers, ...partners]);

  const listings = [];
  const plots = [];
  farmers.forEach((f, i) => {
    sleep(AUTH_PAUSE / 2); // up to two sign-ins per farmer
    let tokens = login(f.phone);
    if (!tokens) throw new Error(`farmer login failed: ${f.phone}`);
    if (!tokens.user.farmerVerified) {
      request('POST', `/identity/users/${tokens.user.id}/verify-farmer`, {
        token: admin.access,
        name: 'setup verify farmer',
      });
      tokens = login(f.phone); // the new token says "verified"
    }
    const t = tokens.access;

    let farms = json(request('GET', '/catalog/farms/mine', { token: t, name: 'setup farms' }), { items: [] }).items;
    if (!farms.length) {
      farms = [
        json(
          request('POST', '/catalog/farms', {
            token: t,
            name: 'setup create farm',
            idempotencyKey: `loadtest-farm-${i}`,
            body: {
              name: `LoadTest Farm ${i}`,
              village: 'Loadtest',
              district: 'Bilaspur',
              state: 'Chhattisgarh',
              location: point(f.lat, f.lng),
            },
          }),
        ),
      ];
    }
    const farm = farms[0];
    let mine = json(request('GET', '/catalog/listings/mine', { token: t, name: 'setup listings' }), { items: [] }).items;
    if (!mine.length) {
      mine = CROPS.slice(0, cfg.listingsPerFarm).map(([crop, variety, price], j) =>
        json(
          request('POST', '/catalog/listings', {
            token: t,
            name: 'setup create listing',
            idempotencyKey: `loadtest-listing-${i}-${j}`,
            body: {
              farmId: farm.id,
              cropId: crop,
              variety,
              description: 'Load-test listing (deleted after the test).',
              qtyAvailable: 1000000,
              unit: 'kg',
              pricePerUnit: price + (i % 7),
              harvestDate: new Date().toISOString().slice(0, 10),
              isOrganic: j % 2 === 0,
            },
          }),
        ),
      );
    }
    mine.forEach((l) => listings.push({ id: l.id, farmId: farm.id, price: l.pricePerUnit }));

    if (i < cfg.plots) {
      let myPlots = json(request('GET', '/land/plots/mine', { token: t, name: 'setup plots' }), []);
      myPlots = Array.isArray(myPlots) ? myPlots : myPlots.items || [];
      if (!myPlots.length) {
        const s = 0.0008;
        const ring = [
          [f.lng, f.lat],
          [f.lng + s, f.lat],
          [f.lng + s, f.lat + s],
          [f.lng, f.lat + s],
          [f.lng, f.lat],
        ];
        const plot = json(
          request('POST', '/land/plots', {
            token: t,
            name: 'setup create plot',
            body: {
              title: `LoadTest plot ${i}`,
              soilType: 'Black soil',
              waterSource: 'Borewell',
              boundary: { type: 'Polygon', coordinates: [ring] },
            },
          }),
        );
        const start = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
        const end = new Date(Date.now() + 120 * 86400000).toISOString().slice(0, 10);
        request('POST', `/land/plots/${plot.id}/seasons`, {
          token: t,
          name: 'setup create season',
          body: { season: 'Rabi', startDate: start, endDate: end, price: 25, allowedCrops: ['rice', 'wheat'] },
        });
        myPlots = [plot];
      }
      myPlots.forEach((p) => plots.push(p.id));
    }
  });

  return {
    customers: customers.map((c) => ({ phone: c.phone, lat: c.lat, lng: c.lng })),
    farmers: farmers.map((f) => ({ phone: f.phone })),
    partners: partners.map((p) => ({ phone: p.phone })),
    listings,
    plots,
  };
}
