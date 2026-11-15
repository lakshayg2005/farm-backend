# identity service

**Owner:** Joint (M5)

Users, roles, phone OTP, login, JWT issue and refresh, farmer verification. It is the only service
that holds the JWT **private** key; every other service verifies tokens with the public key
(`farm_common.auth`).

| | |
| --- | --- |
| Python package | `identity_service` |
| Local dev port | 8001 |
| Database schema | `identity` (users, user_roles, refresh_tokens, otp_codes, outbox, processed_events) |
| Publishes | `user.registered`, `user.updated`, `user.roles_changed`, `farmer.verified` |
| Listens to | nothing |

## Routes (behind the gateway: `/api/v1/identity/...`)

| Method | Route | Who | What |
| --- | --- | --- | --- |
| POST | `/register` | public | Sign up as farmer / customer / delivery (one or more) |
| POST | `/login` | public | Phone + password → access + refresh token |
| POST | `/otp/request` | public | Send a 6-digit code (mock SMS: returned as `devCode` while `OTP_DEV_MODE=true`) |
| POST | `/otp/verify` | public | Verify the code → marks phone verified and logs in |
| POST | `/refresh` | public | Swap a refresh token for a new pair (each refresh token works once) |
| POST | `/logout` | public | Revoke a refresh token |
| GET / PATCH | `/me` | signed in | Read / update own profile (name, email, home location) |
| POST | `/me/roles` | signed in | Add farmer / customer / delivery to own account |
| GET | `/public-key` | public | PEM public key for verifying tokens |
| GET | `/users?role&farmerVerified&limit&cursor` | admin | List users |
| GET | `/users/{id}` | admin | One user |
| POST | `/users/{id}/verify-farmer` | admin | Mark farmer verified (`farmer.verified`) |
| PUT | `/users/{id}/roles` | admin | Replace roles (only way to grant inspector / admin); ends their sessions |
| GET | `/internal/users/{id}` | other services | User details (not exposed by the gateway) |
| GET | `/internal/auth/verify` | Traefik forward-auth | 200 + `X-User-Id`, `X-User-Roles`, or 401 |

Tokens: access 15 min (`type=access`, carries `roles`), refresh 7 days (`type=refresh`, stored by `jti`).
A refresh token that is used twice revokes all of that user's sessions (stolen-token protection).
Role changes show up in the access token after the next `/refresh`.

## Run locally

```bash
uv run python scripts/gen_jwt_keys.py                      # once: writes secrets/jwt_*.pem
export JWT_PRIVATE_KEY_PATH=secrets/jwt_private.pem
export DATABASE_URL=postgresql+asyncpg://farm:change-me@localhost:5432/farm
export EVENTS_ENABLED=false                                  # until RabbitMQ runs (docker-compose, #2)
(cd services/identity && uv run alembic upgrade head)
uv run --package farm-identity uvicorn identity_service.main:app --reload --port 8001

# first admin (nobody can sign up as admin)
uv run --package farm-identity python -m identity_service.cli create-admin \
    --name "Platform Admin" --phone +919999999999 --password "change-me-please"
```

Other services need `JWT_PUBLIC_KEY_PATH=secrets/jwt_public.pem` to verify tokens.

## Tests

`uv run pytest services/identity`: they run against in-memory SQLite (the `identity` schema is
mapped away), with a generated key pair and events disabled.
