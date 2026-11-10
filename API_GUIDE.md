# PlagiaScan API guide

## Sources searched automatically (no key needed)
Wikipedia (full text), DuckDuckGo (public web pages), arXiv (abstracts), OpenAlex and Crossref (abstracts / open links), plus your **local reference library**. A Brave or Google key is optional and adds extra web coverage.

## Optional keys (`backend/.env`)
```env
SEARCH_PROVIDER=brave
BRAVE_API_KEY=your_key
# or
SEARCH_PROVIDER=google
GOOGLE_API_KEY=your_key
GOOGLE_CSE_ID=your_engine_id
OPENALEX_EMAIL=you@example.com   # optional, gives OpenAlex a faster pool
```
Restart after editing `.env`. Never commit `.env`.

## Endpoints
| Method | Route | Purpose |
|---|---|---|
| GET | `/login`, `/register`, `/dashboard` | User account pages |
| GET | `/admin/login`, `/admin/dashboard` | Separate admin pages |
| POST | `/api/auth/register` | Public registration (always creates role `user`) |
| POST | `/api/auth/login` | User login by username or email |
| POST | `/api/auth/admin-login` | Admin login by username or email |
| POST | `/api/auth/logout` | Log out |
| POST | `/api/auth/password` | Change the signed-in account password |
| GET | `/health` | Status |
| GET | `/diagnose` | Tests each provider from your machine (shows which are blocked) |
| POST | `/scan` | `{"name":"a.txt","text":"..."}` -> similarity, matches, sources_checked, source_counts, provider_results, warnings |
| POST | `/extract` | multipart `file` (PDF/DOCX/TXT) -> text |
| POST | `/contact` | `{"name":"...","email":"...","document":"...","message":"..."}` -> emails the configured admin |
| GET | `/history` | Recent scans owned by the signed-in user; admins see all |
| GET | `/history/<id>`, `/stats` | Owned report / account statistics; admins can access all |
| GET/POST/DELETE | `/reference-documents[/<id>]` | Local reference library |

If `API_KEY` is set, send `Authorization: Bearer <API_KEY>`.

Set `ADMIN_INITIAL_PASSWORD` and a long random `SESSION_SECRET_KEY` in `backend/.env` before first startup. The initial admin is `superadmin` / `admin@yourdomain.com`; no password is supplied by default. Admin credentials are stored as a Werkzeug password hash. Public registrations cannot select the admin role.

Configure `CONTACT_ADMIN_EMAIL`, `SMTP_HOST`, and the SMTP settings (`SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS`) in `backend/.env` to enable contact requests. Without the required recipient and host, `/contact` returns `503`.

## If you still get 0%
1. Open http://127.0.0.1:5000/diagnose - every provider should show `"ok": true`. If not, your firewall/proxy/VPN blocks internet access from Python.
2. 0% is correct when the text is original or exists on no searched source. Test with a copied Wikipedia paragraph: it should score ~100%.
3. Add the source file to the Local reference library; matches against it never depend on the internet.
