# PlagiaScan — Online Plagiarism Checker

A Flask web application that compares an uploaded/pasted document with three source groups:

1. **Local reference library** — documents deliberately added by the project operator and stored in SQLite.
2. **Public web pages** — Wikipedia and DuckDuckGo work with no key; Brave/Google keys are optional extras.
3. **Academic works** — discovered through OpenAlex and Crossref. The system compares accessible full text when available, and can also compare an API-provided abstract when full text is unavailable. Results identify abstract-based coverage; metadata alone is never treated as a text match.

The comparison engine uses TF-IDF cosine similarity plus word 3-gram containment (catches copied text even when the source is split across lines). Optional Sentence Transformers embeddings can help identify paraphrases, but results remain similarity indicators and require human review.

## Requirements

- Python 3.10–3.12 recommended.
- Internet connection for online search and source retrieval.
- No API key is required. Brave/Google keys are optional.
- OpenAlex and Crossref discovery are attempted without a paid subscription; service availability and rate limits may vary.

## Windows setup

Open PowerShell in this folder:

```powershell
cd backend
py -3.12 -m venv venv
venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `backend/.env` and add your `BRAVE_API_KEY` (or configure Google credentials) for public website discovery. OpenAlex and Crossref academic discovery are attempted automatically without a search key. Academic APIs may return abstracts or metadata rather than full papers; only actual retrieved text/abstract content is compared.

### User and admin accounts

Set `ADMIN_INITIAL_PASSWORD` to a strong, unique value in `backend/.env` before first startup. On first database use the backend creates the admin account `superadmin` (`admin@yourdomain.com`) and stores only a Werkzeug password hash. There is no default password. Public registration always creates a normal user.

Set `SESSION_SECRET_KEY` to a persistent random secret (generate with `python -c "import secrets; print(secrets.token_hex(32))"`), then restart the backend. Keep `.env` private and do not commit it. The admin can change their password from the admin dashboard; the account remains in SQLite. User login and registration are at `/login` and `/register`. Admin login is at `/admin/login`; the admin dashboard is `/admin/dashboard`.

Start the app:

```powershell
python app.py
```

Open http://127.0.0.1:5000

If PowerShell blocks activation, use:

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe app.py
```

## Optional semantic matching

TF-IDF is the default and does not download a model. To enable semantic similarity:

```powershell
python -m pip install -r requirements-embeddings.txt
```

Set `USE_EMBEDDINGS=1` in `.env`, then restart. The first run downloads the configured sentence-transformer model, so internet access and extra disk/RAM are needed.

## Using the local reference library

Open **About → Local reference library**, select PDF/DOCX/TXT and add it. The text is stored in the local SQLite database and included in future scans. A duplicate document (same extracted text) is rejected. Remove reference files from the same panel.

User scan submissions are saved in scan history, but are **not automatically added** to the reference library. This avoids storing someone's work as a reference without their knowledge.

## API routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Frontend |
| GET | `/login`, `/register`, `/dashboard` | User login, registration, and protected dashboard |
| GET | `/admin/login`, `/admin/dashboard` | Separate admin login and protected dashboard |
| POST | `/api/auth/register` | Register a normal user account |
| POST | `/api/auth/login` | User login by username or email |
| POST | `/api/auth/admin-login` | Admin login by username or email |
| POST | `/api/auth/logout` | End the current session |
| POST | `/api/auth/password` | Change the authenticated account password |
| GET | `/health` | Backend status |
| GET | `/diagnose` | Checks which online providers are reachable |
| POST | `/extract` | Extract text from PDF/DOCX/TXT |
| POST | `/scan` | Compare text with local, web and accessible academic sources |
| GET | `/history` | Recent scans for the authenticated account (admins see all) |
| GET | `/history/<id>` | One owned scan; admins may view any scan |
| GET | `/stats` | Account statistics (admins see all) |
| POST | `/contact` | Email a content support request to the configured admin |
| GET/POST | `/reference-documents` | List/add local reference documents |
| DELETE | `/reference-documents/<id>` | Remove a local reference document |

If `API_KEY` is set, API requests (except `/` and `/health`) require `Authorization: Bearer YOUR_KEY`. Enter the same key in **About → API settings**.

To enable the Contact page, configure `CONTACT_ADMIN_EMAIL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and optionally `SMTP_FROM` and `SMTP_STARTTLS` in `backend/.env`. Contact form messages are emailed to the configured admin; the submitter's address is set as Reply-To.

## Troubleshooting 0% results

Open http://127.0.0.1:5000/diagnose. Providers showing `ok: false` are blocked by your network. See `API_GUIDE.md`.

## Important limitations

- A web search engine does not search the entire internet or every private website.
- OpenAlex and Crossref help discover academic publications. The checker compares accessible full text or API-provided abstracts; metadata alone is not compared. Abstract-based comparison is partial coverage, not a full-paper check.
- Scanned/image-only PDFs need OCR; this build reports when no extractable text is present.
- TF-IDF/cosine similarity detects lexical overlap. Semantic matching is optional and can produce false positives.
- The percentage is a similarity estimate, not a definitive plagiarism verdict, and should not be presented as a validated accuracy score without a labelled test dataset.
- Respect source-site terms, robots policies, copyright and rate limits. Do not use this tool to republish source text.

## Project structure

```text
PlagiaScan_Corrected/
├── backend/
│   ├── app.py
│   ├── requirements.txt
│   ├── requirements-embeddings.txt
│   ├── .env.example
│   └── data/
├── frontend/
│   └── index.html
├── tests/
│   └── test_app.py
├── .gitignore
└── README.md
```
