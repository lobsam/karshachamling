# Karsha Chamling — backend

FastAPI API for **karshachamling.org**. It runs as a **Cloudflare Python Worker**, with
**D1** as the database and **R2** for images.

It provides:
- the public site's content
- the `/admin` API: News, Events, Visitor Guide, Pages, Media, Messages, Donations, Users
- **Razorpay** donations

```
src/worker.py          Worker entry point
src/app/               FastAPI app (routers/, db.py = D1 layer, storage.py = R2 layer)
migrations/            D1 schema (plain SQLite SQL)
tests/                 run on normal Python with sqlite3 standing in for D1
wrangler.jsonc         Worker config: D1 + R2 bindings, vars
```

## Run locally

You need [`uv`](https://docs.astral.sh/uv/) and Node.js (`brew install uv node`).

```bash
uv sync
cp .dev.vars.example .dev.vars                                         # local secrets
uv run pywrangler d1 migrations apply karshachamling --local           # create tables
uv run pywrangler dev                                                  # http://localhost:8787
```

Local D1 and R2 are simulated by wrangler and saved in `.wrangler/state`.
- API docs: http://localhost:8787/docs
- Run the tests with `uv run pytest`.

### Create the first admin user

Hash a password, then insert the user:

```bash
HASH=$(uv run python -c "import sys; sys.path.insert(0,'src'); from app.security import hash_password; print(hash_password(input('Password: ')))")
npx wrangler d1 execute karshachamling --local --command \
  "INSERT INTO users (email, name, role, password_hash) VALUES ('you@example.org', 'Your Name', 'admin', '$HASH')"
```

For the real database, use `--remote` instead of `--local`. Once you're signed in, create
more users from `/admin/users`.

## Deploy to Cloudflare

You need the **Workers Paid** plan ($5/month). The free plan allows only 10 ms of CPU per
request, which is too little for password hashing and image resizing.

1. Run `npx wrangler login`.
2. Run `npx wrangler d1 create karshachamling`. Put the printed `database_id` into both D1
   blocks of `wrangler.jsonc`.
3. Run `npx wrangler r2 bucket create karshachamling-media`. Then, in the dashboard, open
   R2 → bucket → Settings → Custom Domains and add `media.karshachamling.org`.
4. Run `uv run pywrangler d1 migrations apply karshachamling --remote`.
5. Set the secrets (each command prompts for the value):
   ```bash
   npx wrangler secret put JWT_SECRET --env production      # 32+ random characters
   npx wrangler secret put RAZORPAY_KEY_ID --env production
   npx wrangler secret put RAZORPAY_KEY_SECRET --env production
   npx wrangler secret put RAZORPAY_WEBHOOK_SECRET --env production
   ```
   Optional secrets: `TURNSTILE_SECRET`, `CF_PAGES_DEPLOY_HOOK`, `RESEND_API_KEY`,
   `EMAIL_FROM`, `NOTIFY_EMAIL`.
6. Uncomment the `routes` entry for `api.karshachamling.org` in `wrangler.jsonc`, then run
   `uv run pywrangler deploy --env production`.
7. **Razorpay:** add a webhook to `https://api.karshachamling.org/donations/webhook` for the
   events `payment.captured`, `order.paid` and `payment.failed`.
8. **Recommended:** put Cloudflare Access (Zero Trust) in front of `karshachamling.org/admin*`.

Schema changes go in a new numbered file in `migrations/`. Apply it with the same
`migrations apply` command (`--local`, then `--remote`).

## API overview

| Area | Endpoints | Who |
|---|---|---|
| Public content | `GET /news`, `/news/{slug}`, `/events?when=upcoming\|past\|all`, `/events/{slug}`, `/guide`, `/pages`, `/pages/{slug}` | anyone (published only) |
| Contact | `POST /contact` | anyone (Turnstile-protected) |
| Donations | `POST /donations/order`, `/donations/verify`, `/donations/webhook` | anyone / Razorpay |
| Auth | `POST /auth/login`, `/auth/logout`, `/auth/password`, `GET /auth/me` | — |
| Content admin | `GET/POST /admin/{news,events,guide,pages}`, `GET/PUT/DELETE /admin/{type}/{id}`, `POST /admin/guide/reorder` | editor, admin |
| Media | `GET/POST /admin/media`, `PATCH/DELETE /admin/media/{id}` | editor, admin |
| Inbox | `GET /admin/dashboard`, `GET/PATCH/DELETE /admin/messages[/{id}]` | editor, admin |
| Donations admin | `GET /admin/donations`, `/admin/donations/export.csv` | admin |
| Users | `GET/POST /admin/users`, `PATCH/DELETE /admin/users/{id}` | admin |

**Content:** every item has English fields and Tibetan ones (`title_bo`, `summary_bo`,
`body_bo`), a `draft`/`published` status and an optional cover image. Article HTML is
sanitised on save.

**Images:** each upload is stored in R2 as the original plus WebP versions 400, 1200 and
2000 px wide. Uploads are limited to 15 MB. An image that is still in use can't be deleted
unless you pass `?force=true`.

### Notes for the admin frontend

- **Login:** the session is an httpOnly cookie, so send requests with
  `credentials: "include"`.
- **CSRF:** every `POST/PUT/PATCH/DELETE` to a logged-in route must send the header
  `X-Requested-With: kc-admin`.
- **Uploads:** send `multipart/form-data` to `POST /admin/media` with the field `file`
  (optional field: `alt`).

### Python Worker gotchas

- `nh3`, `argon2` and `Pillow` are imported inside functions, not at the top of the file.
  Workers forbid random numbers while the startup snapshot is taken.
- Outbound HTTP goes through `app/http.py`, which uses the Workers `fetch()`. `httpx` isn't
  available in the Worker.
