// Farm platform load test: realistic users going through the public API (via the gateway).
//
//   PROFILE=smoke|load|stress|spike|soak|auth  BASE_URL=http://<gateway>/api/v1 \
//   ADMIN_PHONE=... ADMIN_PASSWORD=... k6 run loadtest/k6/farm.js
//
// Use loadtest/run.sh, which also streams results into Prometheus for the Grafana dashboard
// "Farm · Load test & capacity". Who the virtual users are (each keeps one role):
//   customer 85%  browses produce near home, opens listings and farms, reads notifications;
//                 in 1 of 4 sessions buys (payment intent + checkout) and in half of those cancels;
//                 in 1 of 5 sessions looks at land to rent
//   farmer   10%  checks orders, listings, payouts, notifications; sometimes changes a price
//   partner   5%  checks route offers and the route, sends GPS pings
// Think time between steps is 1-4 s, like a person using the app.
import { check, group, sleep } from 'k6';
import exec from 'k6/execution';
import { request, json, login, pick, jitter } from './lib/api.js';
import { CENTER, PARTNER_AREA, buildWorld } from './lib/world.js';

const PROFILE = __ENV.PROFILE || 'smoke';
const think = (min = 1, max = 4) => sleep(min + Math.random() * (max - min));

// --- test shapes --------------------------------------------------------------------------
// `users` profiles ramp concurrent people (closed model: each waits for its answer and thinks).
const step = (target, ramp, hold) => [
  { duration: ramp, target },
  { duration: hold, target },
];
const PROFILES = {
  smoke: { stages: step(5, '20s', '1m') },
  load: {
    stages: [...step(50, '1m', '3m'), ...step(100, '1m', '3m'), ...step(200, '1m', '3m'), { duration: '1m', target: 0 }],
  },
  stress: {
    stages: [
      ...step(100, '1m', '2m'),
      ...step(200, '1m', '2m'),
      ...step(400, '1m', '2m'),
      ...step(600, '1m', '2m'),
      ...step(800, '1m', '2m'),
      ...step(1000, '1m', '2m'),
      ...step(1500, '1m', '2m'),
      { duration: '1m', target: 0 },
    ],
  },
  spike: {
    stages: [
      ...step(50, '30s', '2m'),
      { duration: '20s', target: 800 }, // everybody opens the app at once
      { duration: '2m', target: 800 },
      { duration: '20s', target: 50 },
      { duration: '3m', target: 50 }, // does it recover?
      { duration: '20s', target: 0 },
    ],
  },
  soak: { stages: [...step(150, '2m', '30m'), { duration: '1m', target: 0 }] },
};

const custom = (__ENV.STAGES || '').trim(); // e.g. "1m:100,3m:100,1m:0"
const stages = custom
  ? custom.split(',').map((s) => ({ duration: s.split(':')[0], target: Number(s.split(':')[1]) }))
  : (PROFILES[PROFILE] || PROFILES.smoke).stages;

const scenarios =
  PROFILE === 'auth'
    ? {
        // Sign-ins per second, stepped up: how many logins the identity service can take
        logins: {
          executor: 'ramping-arrival-rate',
          exec: 'signIn',
          startRate: 1,
          timeUnit: '1s',
          preAllocatedVUs: 50,
          maxVUs: 400,
          stages: [
            { duration: '1m', target: 2 },
            { duration: '1m', target: 5 },
            { duration: '1m', target: 10 },
            { duration: '1m', target: 20 },
            { duration: '1m', target: 40 },
            { duration: '30s', target: 0 },
          ],
        },
      }
    : { users: { executor: 'ramping-vus', exec: 'user', startVUs: 0, stages, gracefulRampDown: '30s' } };

export const options = {
  scenarios,
  setupTimeout: '20m',
  teardownTimeout: '1m',
  // Goals; the run continues past them so a stress test can find where they break.
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{expected_response:true}': ['p(95)<500', 'p(99)<1500'],
    checks: ['rate>0.99'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  discardResponseBodies: false,
  userAgent: 'farm-loadtest/1.0 (k6)',
};

// --- setup: build (or reuse) the test world ------------------------------------------------
export function setup() {
  const cfg = {
    adminPhone: __ENV.ADMIN_PHONE,
    adminPassword: __ENV.ADMIN_PASSWORD,
    farmers: Number(__ENV.FARMERS || 20),
    customers: Number(__ENV.CUSTOMERS || 300),
    partners: Number(__ENV.PARTNERS || 20),
    listingsPerFarm: 4,
    plots: Number(__ENV.PLOTS || 10),
  };
  const world = buildWorld(cfg);
  console.log(
    `world: ${world.customers.length} customers, ${world.farmers.length} farmers, ` +
      `${world.partners.length} partners, ${world.listings.length} listings, ${world.plots.length} plots`,
  );
  return world;
}

// --- per-VU session ------------------------------------------------------------------------
let session = null; // { role, person, tokens }
const REFRESH_AFTER_MS = 12 * 60 * 1000; // access tokens live 15 minutes

function role(vu) {
  const slot = vu % 20; // 17 of 20 customers, 2 farmers, 1 partner
  if (slot === 0) return 'partner';
  if (slot === 1 || slot === 2) return 'farmer';
  return 'customer';
}

function ensureSession(world) {
  const vu = exec.vu.idInTest;
  if (!session) {
    const r = role(vu);
    const pool = r === 'customer' ? world.customers : r === 'farmer' ? world.farmers : world.partners;
    const person = pool[vu % pool.length];
    const tokens = login(person.phone); // opening the app: sign in
    if (!tokens) {
      sleep(2);
      return null;
    }
    session = { role: r, person, tokens };
  } else if (Date.now() - session.tokens.at > REFRESH_AFTER_MS) {
    const res = request('POST', '/identity/refresh', {
      body: { refreshToken: session.tokens.refresh },
      name: 'POST /identity/refresh',
    });
    if (res.status === 200) {
      const b = json(res);
      session.tokens = { access: b.accessToken, refresh: b.refreshToken, user: b.user, at: Date.now() };
    } else {
      session = null;
      return null;
    }
  }
  return session;
}

export function user(world) {
  const s = ensureSession(world);
  if (!s) return;
  const t = s.tokens.access;
  if (s.role === 'customer') customer(world, s, t);
  else if (s.role === 'farmer') farmer(world, t);
  else partner(t);
}

function customer(world, s, t) {
  const home = s.person;
  group('browse produce', () => {
    request('GET', '/catalog/crops', { token: t });
    request('GET', '/notification/notifications/unread-count', { token: t });
    think();
    const crop = Math.random() < 0.3 ? `&crop=${pick(['tomato', 'onion', 'potato', 'okra'])}` : '';
    const found = json(
      request('GET', `/catalog/listings/search?lat=${jitter(home.lat, 0.02)}&lng=${jitter(home.lng, 0.02)}&radius_km=25&limit=20${crop}`, {
        token: t,
        name: 'GET /catalog/listings/search',
      }),
      { features: [] },
    );
    think();
    const features = found.features || [];
    for (let i = 0; i < 2 && features.length; i++) {
      const f = pick(features).properties;
      request('GET', `/catalog/listings/${f.id}`, { token: t, name: 'GET /catalog/listings/{id}' });
      request('GET', `/media/owners/LISTING/${f.id}`, { token: t, name: 'GET /media/owners/{type}/{id}', ok: [200, 404] });
      think(1, 3);
    }
    if (features.length) {
      const farmId = pick(features).properties.farmId;
      request('GET', `/catalog/farms/${farmId}`, { token: t, name: 'GET /catalog/farms/{id}' });
      request('GET', `/catalog/farms/${farmId}/listings`, { token: t, name: 'GET /catalog/farms/{id}/listings' });
      think();
    }
  });

  if (Math.random() < 0.25) {
    group('buy', () => {
      // only ever buy load-test listings (never real farmers' stock)
      const items = [pick(world.listings)];
      if (Math.random() < 0.3) items.push(pick(world.listings));
      request('GET', '/order/orders/delivery-options', { token: t, ok: [200, 404, 422] });
      const intent = json(
        request('POST', '/order/orders/payments/intent', { token: t, body: { amount: 1, kind: 'FULL' } }),
      );
      if (!intent) return;
      think(1, 2);
      const today = new Date(Date.now() + 5.5 * 3600 * 1000).toISOString().slice(0, 10); // India date
      const res = request('POST', '/order/orders', {
        token: t,
        idempotencyKey: `lt-${exec.vu.idInTest}-${exec.scenario.iterationInTest}-${Date.now()}`,
        body: {
          items: items.map((l) => ({ listingId: l.id, qty: 1 + Math.floor(Math.random() * 3) })),
          deliveryAddress: {
            line1: '1 Load Test Road',
            city: 'Bilaspur',
            pincode: '495001',
            contactPhone: home.phone,
            location: { type: 'Point', coordinates: [jitter(CENTER.lng, 0.05), jitter(CENTER.lat, 0.05)] },
          },
          deliveryDate: today,
          paymentIntentId: intent.id,
        },
      });
      const placed = json(res, {});
      check(res, { 'order confirmed': () => (placed.orders || []).every((o) => o.status === 'CONFIRMED') });
      think();
      const order = (placed.orders || [])[0];
      if (order) {
        request('GET', `/order/orders/${order.id}`, { token: t, name: 'GET /order/orders/{id}' });
        if (Math.random() < 0.5) {
          think(1, 2);
          request('POST', `/order/orders/${order.id}/cancel`, {
            token: t,
            name: 'POST /order/orders/{id}/cancel',
            body: { reason: 'Load test' },
          });
        }
      }
    });
  }

  if (Math.random() < 0.2) {
    group('look for land', () => {
      const plots = json(
        request('GET', `/land/plots/search?lat=${jitter(CENTER.lat, 0.03)}&lng=${jitter(CENTER.lng, 0.03)}&radius_km=40&limit=20`, {
          token: t,
          name: 'GET /land/plots/search',
        }),
        { features: [] },
      );
      think();
      const list = plots.features || [];
      if (list.length) {
        const plot = pick(list);
        const id = plot.id || plot.properties.id;
        request('GET', `/land/plots/${id}`, { token: t, name: 'GET /land/plots/{id}' });
        request('GET', `/land/plots/${id}/seasons`, { token: t, name: 'GET /land/plots/{id}/seasons' });
        think();
      }
      request('GET', '/land/reservations/mine', { token: t });
    });
  }

  group('my account', () => {
    request('GET', '/order/orders?limit=10', { token: t, name: 'GET /order/orders' });
    request('GET', '/notification/notifications?limit=20', { token: t, name: 'GET /notification/notifications' });
    request('GET', '/identity/me', { token: t });
  });
  think(2, 6);
}

function farmer(world, t) {
  group('farmer dashboard', () => {
    request('GET', '/identity/me', { token: t });
    request('GET', '/notification/notifications/unread-count', { token: t });
    const orders = json(request('GET', '/order/orders?limit=20', { token: t, name: 'GET /order/orders' }), { items: [] });
    think();
    const listings = json(request('GET', '/catalog/listings/mine', { token: t }), { items: [] }).items || [];
    think();
    request('GET', '/catalog/farms/mine', { token: t });
    request('GET', '/order/orders/payouts?limit=20', { token: t, name: 'GET /order/orders/payouts' });
    request('GET', '/land/plots/mine', { token: t });
    request('GET', '/land/reservations/mine', { token: t });
    if (listings.length && Math.random() < 0.3) {
      const l = pick(listings);
      think(1, 2);
      request('PATCH', `/catalog/listings/${l.id}`, {
        token: t,
        name: 'PATCH /catalog/listings/{id}',
        body: { pricePerUnit: Math.round(20 + Math.random() * 30) },
      });
    }
    if ((orders.items || []).length) {
      request('GET', `/order/orders/${orders.items[0].id}`, { token: t, name: 'GET /order/orders/{id}' });
    }
    request('GET', '/notification/notifications?limit=20', { token: t, name: 'GET /notification/notifications' });
  });
  think(3, 8);
}

function partner(t) {
  group('delivery partner', () => {
    request('GET', '/logistics/offers/mine', { token: t });
    request('GET', '/logistics/my-route', { token: t, ok: [200, 404] });
    for (let i = 0; i < 3; i++) {
      // GPS ping, far from any real route (see world.js)
      request('POST', '/logistics/location', {
        token: t,
        body: { lat: jitter(PARTNER_AREA.lat, 0.02), lng: jitter(PARTNER_AREA.lng, 0.02), accuracyM: 8 },
      });
      think(2, 5);
    }
    request('GET', '/notification/notifications/unread-count', { token: t });
  });
  think(2, 5);
}

// --- auth profile: logins per second ------------------------------------------------------
export function signIn(world) {
  const c = world.customers[Math.floor(Math.random() * world.customers.length)];
  login(c.phone);
}
