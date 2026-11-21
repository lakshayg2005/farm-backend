# Android (Kotlin) hand-off: sync, clean up, redesign, test

Self-contained brief for the Claude Code session that works on the native Android apps in
`farm-mobile/android/`. Read the whole file first. The backend and the website are being
fixed in parallel by someone else; your job is the two Android apps only.

## 1. Goal

Make the **customer** and **delivery** Android apps (Kotlin + Jetpack Compose) a faithful,
tested client of the real backend, with no fake data anywhere, a clean consistent UI, and
every planned feature working end to end against a seeded demo backend.

Definition of done: a person can install both APKs, point them at the demo gateway, sign in with
a demo account and complete the V1 and V2 demo flows in section 8 without hitting a screen that
is fake, empty by accident, or silently failing.

## 2. Repos and rules

| Repo | Path (typical) | Role |
| --- | --- | --- |
| `farm-mobile` | `android/` | **You work here.** Modules `:core`, `:customer`, `:delivery` |
| `farm-backend` | | API source of truth. OpenAPI specs in `contracts/openapi/*.json`, event contracts in `contracts/events/`, design doc `docs/HLD.md`, order-service decisions `docs/m1-assumptions.md` |
| `farm-web` | | Reference for how each feature behaves in the browser (same API) |

- Work on a branch per issue: `feat/android-<name>` or `fix/android-<name>`. Conventional
  Commits (`feat:`, `fix:`, `chore:`, `test:`). Open a PR to `main` with `Closes #<issue>`; do not
  push to `main`.
- Never commit `.env`, keystores, `google-services.json` or API keys.
- Do not add AI attribution trailers or "generated with" lines to commits or PR text.
- If the backend contract looks wrong, do not work around it silently: write it down in
  `android/NOTES.md` under "Backend issues" with the request, the response and what you expected.
  The backend owner fixes it.
- The older Expo apps in `apps/` and `packages/` are superseded by `android/`. Do not change them.
  Whether to delete them is a team decision; raise it in the PR description, do not delete.

## 3. Environment you need

- JDK 17 or 21, Android Studio (or command line SDK) with platform 34/35 and an emulator image
  (Pixel 6, API 34) or a physical phone on USB.
- The gateway must be reachable from the device. Options:
  1. Team VM: `http://10.1.75.79:4501/api/v1` (college network or VPN only).
  2. Local stack: in `farm-backend`, `docker compose up -d`, then from the Android emulator use
     `http://10.0.2.2/api/v1`; from a phone use `http://<laptop-lan-ip>/api/v1`.
- The base URL default lives in `android/gradle.properties` (`farmApiBaseUrl`, currently a
  hard-coded `http://10.50.7.73/api/v1`). It can also be changed at runtime on the login screen.
- Build: `cd android && ./gradlew assembleDebug` and `./gradlew testDebugUnitTest`.

## 4. What exists today

`android/core`: `api/Http.kt` (OkHttp, `ApiConfig`, error envelope), `api/Models.kt`,
`api/Services.kt` (`CatalogApi`, `LandApi`, `LogisticsApi`, `MediaApi`, `OrderApi`,
`NotificationApi`), `auth/Auth.kt` (`AuthManager`: OTP, password, register, refresh),
`ui/Components.kt` (`FarmColors`, shared widgets), `ui/LoginScreen.kt`, `ui/TeamScreen.kt`.

`android/customer`: `browse/Browse.kt`, `checkout/Checkout.kt` (cart, idempotency key, payment
intent), `land/Land.kt`, `orders/Orders.kt`, and `ui/` (`HomeScreen`, `HomeViewModel`,
`BrowseUi`, `LandUi`, `ShopScreens`, `OrderScreens`). One `MainActivity`.

`android/delivery`: `route/Route.kt`, `Photos.kt`, `LocationPings.kt`, `ui/HomeScreen`,
`RouteScreen`, `StopSheets`. Unit tests exist for route and checkout only.

## 5. Verified problems to fix (start here)

These were found by reading the code; confirm each one before changing it.

1. **Silent fake-data fallbacks.** `customer/.../browse/Browse.kt` (around lines 188 to 233) and
   `customer/.../land/Land.kt` (lines 143 to 200) load `assets/seed_listings.json`,
   `seed_plots.json`, `seed_reservations.json` when any call throws, and show them as if real.
   A dead server looks like a working app.
2. **Fabricated reservations.** `LandRepository.submitReservation` swallows the API error
   (`// Simulated offline creation`), then builds a local `ReservationItem` with id
   `res-<millis>`. The user sees success; nothing exists on the server.
3. **Mock logins.** `AuthManager.devMockLogin` and the "Mock <ROLE> Login" buttons in
   `core/.../ui/LoginScreen.kt` create a local session with no token.
4. **Hard-coded placeholder text** in `Land.kt`: `village = "Local Village"`,
   `district = "Local District"`, `farmerName = "Assigned Farmer"`, `plotTitle ?: "Reserved
   Farmland Plot"`, `seasonName ?: "Active Season"`, `soilType ?: "Loam"`, `waterSource ?:
   "Borewell"`. Show real data, or hide the field when the API does not return it.
5. **Reservation deposit is probably never paid.** A reservation moves PENDING, ACCEPTED (farmer
   accepts), then needs a DEPOSIT payment (`POST /order/orders/payments/intent` with
   `kind: "DEPOSIT"` and `referenceId: <reservationId>`) to become ACTIVE. The only payment code
   found is in `checkout/`. Verify that a customer can pay the deposit in the app.
6. **Crop timeline never calls the API.** Nothing calls
   `GET /land/reservations/{id}/updates`; `CropTimelineView` shows local data. It must show the
   farmer's real updates with photos and the "verified on site / unverified" badge
   (`media.isGeoVerified`).
7. **Catch-all `catch (_: Exception)`** blocks in `OrderScreens.kt`, `HomeViewModel.kt`,
   `StopSheets.kt`, `RouteScreen.kt` hide failures. Every network failure needs a visible state
   (see section 7).
8. **No push notifications** (FCM not wired). The in-app inbox works. Do this last (phase A6).
9. **No tests** beyond route and checkout unit tests; no UI tests.
10. Anything else you find: add it to `android/NOTES.md`.

## 6. Backend cheat sheet

Gateway base: `<host>/api/v1/<service>/...`. Services: `identity`, `catalog`, `land`, `media`,
`quality`, `order`, `logistics`, `notification`. All JSON is camelCase. Money is rupees as a
number. Places are GeoJSON `{"type":"Point","coordinates":[lng,lat]}` (longitude first).
Lists paginate with `cursor` / `nextCursor` (order, notification, catalog search) or `offset`
(plot search). The full request and response shapes are in `farm-backend/contracts/openapi`;
the tables below are only the parts you need first.

**Errors:** `{"error": {"code", "message", "correlationId", "details"}}`. Show `message` to the
person; log `correlationId`. Codes you must handle: `VALIDATION_ERROR` (422, `details[]` has
`field` + `message`), `STOCK_UNAVAILABLE`, `LISTING_NOT_AVAILABLE`, `PAYMENT_FAILED` (402),
`IDEMPOTENCY_KEY_REUSED`, `IDEMPOTENCY_IN_PROGRESS`, `DEPOSIT_AMOUNT_MISMATCH`,
`DEPOSIT_ALREADY_PAID`, `RESERVATION_NOT_FOUND`, 401 (refresh the token once, then sign out),
403 (wrong role).

**Auth (`identity`, no token needed):**

| Call | Body | Notes |
| --- | --- | --- |
| `POST /identity/register` | `name, phone, password, roles[], email?` | returns `UserOut`, not tokens: sign in afterwards |
| `POST /identity/login` | `phone, password` | returns `accessToken, refreshToken, expiresIn, user` |
| `POST /identity/otp/request` then `/otp/verify` | `phone` / `phone, code` | dev mode returns the OTP in the request response |
| `POST /identity/refresh` | `refreshToken` | access token lasts 15 minutes |
| `GET /identity/me` | | roles: `farmer`, `customer`, `delivery`, `inspector`, `admin` |

Phones are `+91XXXXXXXXXX`. Customer app requires role `customer`; delivery app requires the
`delivery` role.

**Customer, V1 (buy produce):**

| Feature | Call |
| --- | --- |
| Browse near me | `GET /catalog/listings/search?lat&lng&radius_km<=100&crop&min_price&max_price&grade&organic&sort&limit&cursor` returns a GeoJSON FeatureCollection |
| Listing detail and photos | `GET /catalog/listings/{id}`, `GET /media/{mediaId}` |
| Checkout | `POST /order/orders/payments/intent {amount, kind:"FULL"}` then `POST /order/orders` with header `Idempotency-Key` (required) and body `{items[{listingId, qty}], deliveryAddress{..., location}, deliveryDate?, notes?, paymentIntentId}`. Returns a `Checkout` holding one `Order` per farm. Payment failure is HTTP 402; retry with a **new** Idempotency-Key |
| My orders | `GET /order/orders?limit&cursor`, `GET /order/orders/{id}` (items, events, payments, rating) |
| Cancel | `POST /order/orders/{id}/cancel` (allowed until QC_PASSED) |
| Track | `GET /logistics/orders/{orderId}/track` (driver position, ETA, the drop stop and its OTP) |
| Rate | `POST /order/orders/{id}/rating {score 1-5, comment?}` once COMPLETED |
| Notifications | `GET /notification/notifications`, `POST .../{id}/read`, `POST .../read-all`, `GET .../unread-count`, `POST .../devices` (push token) |

Order statuses: `PLACED, CONFIRMED, PACKED, QC_PASSED, READY_FOR_DISPATCH, POOLED, IN_TRANSIT,
DELIVERED, COMPLETED, CANCELLED`. The delivery fee is a flat 20.00 per farm order.

**Customer, V2 (reserve land):**

| Feature | Call |
| --- | --- |
| Search plots | `GET /land/plots/search?lat&lng&radius_km&season&crop&limit&offset` (also `west,south,east,north` for the visible map area) |
| Reserve | `POST /land/reservations {plotSeasonId, cropId, expectedQtyKg}` returns `status`, `deposit`, `total`, `expiresAt` (48 hours for the farmer to answer) |
| My reservations | `GET /land/reservations/mine` |
| Cancel | `POST /land/reservations/{id}/cancel` |
| Pay deposit | `POST /order/orders/payments/intent {amount: <reservation.deposit>, kind:"DEPOSIT", referenceId: <reservationId>}`; the amount must equal the deposit exactly. Reservation goes ACTIVE after the event round trip (a few seconds): poll `reservations/mine` |
| Crop timeline | `GET /land/reservations/{id}/updates` (stage, note, photos, location) |
| Harvest choice | After the harvest passes inspection: `POST /order/orders/harvest {reservationId, option:"DELIVERY"|"BUYBACK", deliveryAddress?, deliveryDate?}`. DELIVERY joins the normal pooling and tracking; BUYBACK credits 90 % at once and completes |

Reservation statuses (from `services/land`): `PENDING, ACCEPTED, ACTIVE, HARVEST_READY,
FULFILLING` (harvest passed inspection, waiting for the customer's delivery or buy-back choice),
`REJECTED, CANCELLED, EXPIRED`. Mock payments approve everything except an amount equal to
the server's `MOCK_PAYMENT_FAIL_AMOUNT`, which is how you demo a payment failure.

**Delivery app (partner):**

| Feature | Call |
| --- | --- |
| Today's route | `GET /logistics/my-route?pool_date=YYYY-MM-DD` returns a `Pool` with ordered `stops[]` (`stopType` PICKUP or DROP, `seq`, `lat`, `lng`, `address`, `customerName`, `customerPhone`, `status`). Returns 404 or empty when there is no pool: show "No route today", not an error |
| Arrived | `POST /logistics/stops/{id}/arrive` |
| Pickup | `POST /logistics/stops/{id}/pickup {notes?}` |
| Deliver | `POST /logistics/stops/{id}/deliver {otp, photoMediaId, notes?, deliveredAt?}`. The OTP is the customer's code; wrong OTP returns an error |
| Skip | `POST /logistics/stops/{id}/skip` |
| Location ping | `POST /logistics/location {lat,lng,poolId?,speedMs?,heading?,accuracyM?}` every 30 s while a route is active |
| Photo | `POST /media/upload-url {ownerType:"DELIVERY", ownerId, contentType, sizeBytes}` returns a signed `uploadUrl`; `PUT` the bytes to it with the returned `headers`; then `POST /media/{mediaId}/complete`. Keep the returned `mediaId` for `deliver` |

Photos need object storage (MinIO). If an upload fails, show the real error and let the person
retry; never substitute a made-up media id.

**Events and timing:** the backend is event driven. After a state change, other services catch up
within a few seconds (reservation activation, order status, inspections, pooling). Screens that
follow such a change need pull-to-refresh and a short, bounded poll, not an assumption that the
next GET is already updated.

## 7. Engineering rules for the cleanup

- **One source of truth per feature:** screen, ViewModel, repository, API. The repository returns
  `Result`/sealed state; it never returns invented data.
- **UI state model:** every screen that loads data renders four states: `Loading` (skeleton, not
  a spinner over blank), `Content`, `Empty` (says what to do next), `Error` (the server message,
  a Retry button, and for no connectivity a specific "Cannot reach the server" with the current
  base URL and a Change server action).
- Delete `assets/seed_*.json`, `devMockLogin`, the "Mock ... Login" buttons and every
  "Simulated" or "fallback" branch. The login screen keeps only real sign-in and the server URL
  setting.
- Replace broad `catch (_: Exception)` with typed handling (`ApiException`, `NetworkException`).
  Cancellation exceptions must propagate.
- Inject a base URL and token provider so repositories can be tested with `MockWebServer`.
- No placeholder strings in production code paths. If the API has no value, omit the row.
- Accessibility: content descriptions on icon buttons, 48 dp touch targets, contrast 4.5:1,
  font scaling to 200 % without clipping.
- Respect system dark mode only if the design tokens define it; otherwise stay light and say so.

## 8. Plan in phases

### A0. Build and baseline (half a day)
1. Get both apps building and running on an emulator against the demo gateway.
2. Add `android/NOTES.md`. Record the baseline: which screens work, which show fake data, which
   crash. Screenshot each main screen.
3. Make `farmApiBaseUrl` a Gradle property overridable on the command line
   (`-PfarmApiBaseUrl=...`) and document the three URLs from section 3.

### A1. Remove fakes, add honest states (1 day)
Everything in section 5 items 1 to 4 and 7, plus the UI state model from section 7. Acceptance:
turn the server off and every screen shows an error with Retry, never data.

### A2. Make each feature correct against the real backend (2 to 3 days)
Work through the checklist below, one feature per PR, verifying each against the seeded demo
backend (logins and the story are in `farm-backend/docs/DEMO.md`; reset the backend with
`scripts/reset-demo.sh`):

| # | Feature | Pass condition |
| --- | --- | --- |
| 1 | Sign in (OTP and password), register, sign out, token refresh | Wrong role shows a clear message; an expired token refreshes silently |
| 2 | Browse produce near me, filters, listing detail with photos and grade | Results come from `catalog`; distance and grade are real; empty radius shows an empty state |
| 3 | Cart and checkout across two farms | One `Checkout`, two `Order`s; stock error shows which item; payment failure retries with a new key |
| 4 | Orders list and detail, status timeline, cancel | Statuses match section 6; cancel only offered while allowed |
| 5 | Delivery tracking and rating | Driver position and ETA update; rating only after COMPLETED |
| 6 | Land search (list and map), plot detail with seasons | Real village and district if the API provides them |
| 7 | Reserve, deposit payment, ACTIVE state | Reservation exists on the server; deposit paid; status moves to ACTIVE |
| 8 | Crop timeline with photos and verified badge | Real `updates`; unverified photos marked |
| 9 | Harvest delivery or buy-back | Both paths complete; buy-back shows the credit |
| 10 | Notifications inbox, unread badge, mark read | Matches web |
| 11 | Team screen | Real photo and names from the shared team data |
| 12 | Delivery: route, arrive, pickup (checklist), deliver with real photo and OTP, skip | Order reaches DELIVERED then COMPLETED; the customer sees it |
| 13 | Delivery: location pings | Customer tracking shows the driver moving |

Any item that fails because of the backend goes in `android/NOTES.md`, not around it.

### A3. Design system and redesign (3 days)
- Build a Compose theme from one set of tokens: colour roles, type scale (a display face and a
  body face bundled as fonts, not downloaded at run time), spacing scale (4 dp base), corner radii,
  elevation. Replace `FarmColors` with a `FarmTheme` using `MaterialTheme` colour roles.
- **The visual direction follows the website.** The team is choosing between three directions
  (A Bazaar, B Survey, C Ledger); the chosen one and its exact tokens are appended in section 11
  when decided. Do not start the visual redesign before then; do the structural work below first.
- Navigation: customer app bottom bar with Produce, Land, Orders, Profile (notifications in the
  top bar with a badge). Delivery app: Route, History, Profile. Use Navigation Compose with real
  back stack; no more single-state `HomeViewModel` switching.
- Shared components: top app bar, listing card, plot card, status chip, order timeline, stop
  card, empty state, error state, skeleton, bottom sheet, primary and secondary buttons.
- Maps: replace the canvas "maps" with real tiles. Use `osmdroid` (no API key) or Maps Compose
  (needs a key; do not commit it). Plots draw as polygons, produce and stops as markers.
- Layout works on small phones (360 dp wide) and large ones, portrait first.

### A4. Tests (2 days)
- Unit: repositories with `MockWebServer` (success, 4xx envelope, 401 refresh, no network),
  checkout idempotency, route sorting, status mapping, money and distance formatting.
- Compose UI tests for each state (loading, content, empty, error) of the main screens.
- One instrumented end-to-end test per app against a demo backend: customer places an order;
  delivery partner completes a stop.
- `./gradlew testDebugUnitTest connectedDebugAndroidTest` documented in `android/README.md`.

### A5. New features (priority order; check with the team before starting P2)
- P1 Reorder from an order's detail; favourite farms; recent searches.
- P1 Order and reservation timelines with a "what happens next" line.
- P1 Pull-to-refresh and bounded polling after payment, deposit and delivery.
- P2 Hindi and English strings (`strings.xml` per locale, no hard-coded text).
- P2 Offline read cache for the last orders and reservations, clearly labelled as saved data and
  never mixed with fake data.
- P2 Delivery app: navigation hand-off (Google Maps intent) already exists; add stop notes,
  call customer, and a daily summary (stops, km, time).
- P3 Share order status, in-app help.

### A6. Push notifications and release (1 day)
- FCM: needs a Firebase project and `google-services.json` (get it from the team, do not
  commit). Register the token with `POST /notification/notifications/devices`, delete on sign
  out, deep link a notification to its order or reservation.
- Release build: signing config from environment variables, versionCode from git, ProGuard rules,
  `./gradlew assembleRelease`, and a short install guide.

## 9. Demo flows the apps must support

**V1 customer:** sign in, browse nearby produce, add items from two farms, check out, see two
orders appear and move through packed and graded (the farmer packs on the website and an
inspector grades there), watch the delivery on the map, confirm with the OTP, rate the order.

**V2 customer:** find a plot on the map, reserve a season with a crop, wait for the farmer to
accept (website), pay the deposit, follow the crop timeline with verified photos, choose delivery
or buy-back at harvest.

**Delivery partner:** sign in, open today's pooled route, arrive at the farm, mark pickups,
drive to each stop (pings every 30 s), deliver with photo and the customer's OTP.

## 10. Reporting back

Each PR: what changed, how you verified it (device, backend URL, account), screenshots for UI
changes, and any entry added to `android/NOTES.md`. At the end of each phase post a short
summary of what works, what is blocked on the backend, and what is next.

## 11. Visual direction (filled in once chosen)

**Direction C, "Ledger"**: a calm, dense operator workspace. Same tokens as the website.

| Token | Value |
| --- | --- |
| Background | `#F2F4F5` |
| Surface (cards, sheets) | `#FFFFFF`, 1 dp border `#DFE3E6` |
| Ink (text) | `#171A1D`; secondary text `#4B5258` |
| Input border | `#C9CFD4`; subtle fill `#E6E9EB` |
| Primary (actions, done states) | green `#1D7A4D`, white text |
| Info / focus | blue `#2F5FD0` (3 dp focus ring, offset 2 dp) |
| Warning | amber `#B7791F`; Error `#C2412D` |
| Display font | Instrument Serif (screen titles, 36 to 46 sp) |
| Body font | Instrument Sans, weights 400 to 700 (bundle both as font resources) |
| Radii | 12 dp cards and sheets, 8 dp buttons and inputs, 3 dp progress segments |
| Spacing | 4 dp base: 8, 12, 16, 24, 32 |
| Touch targets | at least 48 dp tall; primary buttons 50 dp |

Signature element: the **order status rail**. Orders show an 8-segment bar (Placed, Confirmed,
Packed, Graded, Ready for dispatch, Pooled, In transit, Delivered): completed segments green, the
rest `#DFE3E6`, plus the current stage as text under the bar. Order detail shows the same stages
as a vertical rail with who acts (customer, farmer, inspector, system, partner) and the time. Use
the same pattern for reservations (Pending, Accepted, Active, Harvest ready, Fulfilling).

Component notes: status chips use text plus a colour dot, never colour alone; screens start with
the serif title and a one-line summary; lists are white cards on the grey background with 1 dp
borders, no heavy shadows; destructive actions are secondary buttons with an error-coloured label;
empty states say what to do next. The web mockups are in the shared design canvas (direction C).
