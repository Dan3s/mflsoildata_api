# MFL Soil Data — API

REST API for soil and greenhouse-gas research data across CGIAR / Multifunctional Landscapes
(MFL) projects in Latin America, built with FastAPI and the shared `mflsoildata_orm` ORM on
PostgreSQL + PostGIS.

## Overview

The API powers the [MFL Soil Data frontend](https://github.com/Dan3s/mflsoildata_frontend). Its
data model is: a **project** (dataset) has **sites**; each site has **sampling events**; each
event has **samples**; each sample has **measurements** of catalogue **variables**. Sites,
samples and soil profiles also carry free-form EAV **attributes**.

On top of that it exposes:
- Aggregated endpoints for the dashboard, visualization charts and the map.
- A **review-gated upload workflow**: an editor validates and submits a file, a
  `database_editor`/admin approves or rejects it before anything is written to the database.
- **FAIR metadata** per project (`project_metadata`) with an `access_level` that every data
  endpoint filters by, so two callers can legitimately see different results for the same
  project. Metadata itself stays visible even when the data is restricted (FAIR principle A2).
- Import from an external **Dataverse** installation by DOI (as a preview + file download that
  feeds the same upload/validate flow as any other file).
- Thin **Keycloak user/role administration**, with Keycloak as the single source of truth —
  nothing about identity or roles is mirrored into this database.

All of this is documented interactively once the server is running — see
[Interactive documentation](#interactive-documentation) below.

## Requirements

- Python 3.10+
- PostgreSQL 12+ with PostGIS
- Keycloak 25+ (tested on 26.2.5)
- pip / virtualenv

## Technology Stack

- **Framework**: FastAPI 0.104
- **Server**: Uvicorn
- **ORM**: SQLAlchemy 2, via the shared [`mflsoildata_orm`](https://github.com/Dan3s/mflsoildata_orm) package
- **Validation**: Pydantic 2
- **Database**: PostgreSQL + PostGIS
- **Auth**: Keycloak JWT validation (`pyjwt[crypto]`, RS256 via JWKS)
- **Files**: openpyxl (`.xlsx`), python-multipart (uploads)
- **Docs**: Swagger UI & ReDoc (auto-generated from the code)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/Dan3s/mflsoildata_api.git
cd mflsoildata_api
```

### 2. Create a virtual environment

```bash
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` does not pin the ORM package — install it from its repository if it is not
already available in your environment:

```bash
pip install git+https://github.com/Dan3s/mflsoildata_orm
```

### 4. Environment configuration

Copy `.env.example` to `.env` and fill in your local values — it documents every variable the
API reads (database connection, Keycloak issuer, the Keycloak admin service account used by the
Users screen and submission emails, optional SMTP settings, and Dataverse import defaults).

## Running the Project

### Development

```bash
cd src
python main.py            # uvicorn with reload, on port 8000
# or:  uvicorn main:app --reload
```

The API is then available at `http://localhost:8000`. `run.sh` / `run.bat` wrap the same command.

### Interactive documentation

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **OpenAPI schema**: http://localhost:8000/openapi.json

### Production

Run behind a process manager and a reverse proxy (e.g. Nginx):

```bash
cd src
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

Ensure the PostgreSQL/PostGIS database exists, `.env` carries production values, and
`KEYCLOAK_ISSUER` points at the production realm.

## Authentication & Roles

Every router except `GET /health`, `GET /` (redirects to `/docs`) and the public dashboard teaser
(`GET /dashboard/public-summary`) requires a valid Keycloak access token
(`Authorization: Bearer …`). The token signature is verified against the realm JWKS, and realm
roles are read from `realm_access.roles` (see `src/dependencies/auth.py`:
`get_current_user`, `require_editor`, `require_database_editor`, `require_admin`).

| Role | Can do |
| --- | --- |
| `viewer` | Read (GET) everything they're allowed to see (see [Access control](#access-control--fair-metadata) below) |
| `editor` | Viewer + add data **values** (measurements, EAV attribute *values*, soil profiles) + submit a bulk upload for review |
| `database_editor` | Editor + extend the **catalog/schema** (new variables, EAV attribute *definitions*) + approve/reject submissions |
| `admin` | Everything, including deleting a project, managing users/roles, and bypassing `access_level` entirely |

New self-registered Keycloak users default to `viewer`; role changes are made from the platform's
Users screen (backed by `routes/users.py`) or directly in the Keycloak Admin Console.

## Project Structure

```
mflsoildata_api/
├── src/
│   ├── main.py                     # FastAPI app, router registration, health/root
│   ├── routes/                     # One module per resource/feature
│   │   ├── projects.py             # Projects CRUD + dataset catalogue + FAIR metadata
│   │   ├── sites.py, samples.py, measurements.py
│   │   ├── lookups.py              # Catalog options (projects/sites/variables/units/…)
│   │   ├── dashboard.py            # summary / records / wide (explorer) / export
│   │   ├── visualization.py        # timeseries / by-site / correlation / export
│   │   ├── map.py                  # spatial summary, sites, site detail, export
│   │   ├── site_attributes.py, sample_attributes.py, soil_profiles.py   # EAV attributes
│   │   ├── upload.py               # template-columns / template / validate / import
│   │   ├── submissions.py          # review queue: list / approve / reject
│   │   ├── dataverse.py            # Dataverse-by-DOI preview + file download
│   │   ├── download.py             # estimate / export / audit
│   │   └── users.py                # user & role administration (Keycloak admin API)
│   ├── schemas/                    # Pydantic request/response models, one module per route file
│   ├── services/                   # Business logic: etl (upload engine), dataverse, agrovoc,
│   │                                # email, keycloak_admin, eav
│   ├── dependencies/
│   │   ├── auth.py                 # Keycloak JWT validation + role guards
│   │   └── access.py               # `access_level`-based data-visibility policy
│   └── tests/                      # pytest suite
├── docs/                           # Deeper design/reference docs (data dictionary, FAIR metadata)
├── requirements.txt
├── pytest.ini
├── .env.example                    # Documents every environment variable the API reads
├── run.sh / run.bat                # Dev-server launch helpers
└── README.md                       # This file
```

## Main Endpoints

### Projects, sites, samples, measurements (CRUD)
- `GET/POST/PUT/DELETE /projects/…`, `GET/POST/PUT /sites/…`, `/samples/…`, `GET/POST /measurements/…`
  (reads = Viewer+; writes = Editor/Admin; project delete = Admin)
- `GET /projects/catalogue` — every project joined with its FAIR metadata (one row per dataset);
  this is the one listing endpoint that is **not** filtered by `access_level` (metadata stays
  public per FAIR A2).
- `GET/PUT /projects/{id}/metadata` — read/upsert a project's FAIR metadata ficha (write =
  Editor/Admin; the quality-control fields are reserved for `database_editor`/admin).

### Lookups
- `GET /projects/options`, `GET /projects/summary`, `GET /sites/options?project_id=…`
- `GET /sample-types`, `GET /matrices`, `GET /countries`
- `GET /variables`, `GET /variables/options`, `POST /variables` (Database editor/Admin)
- `GET /units`, `GET /variable-domains`, `GET /measurement-statuses`, `GET /laboratories`
- `GET /institutions/options`, `GET /lookups/agrovoc` (AGROVOC keyword search for the metadata ficha)

### Dashboard
- `GET /dashboard/summary` — overview metrics
- `GET /dashboard/public-summary` — anonymous teaser for the landing page (PUBLIC projects only)
- `GET /dashboard/records` — unified, filterable/paginated table
- `GET /dashboard/wide` — one row per sample, one column per selected variable (feeds the
  Graphic Walker data explorer)
- `GET /dashboard/export` — CSV export of filtered results

### Visualization
- `GET /visualization/timeseries`, `/by-site`, `/correlation`, `/export`

### Map
- `GET /map/summary`, `/sites`, `/sites/{site_id}`, `/sites/{site_id}/attributes`,
  `/regions`, `/variables`, `/export`

### EAV attributes
- `GET/POST /site-attributes`, `/site-attribute-values`
- `GET/POST /sample-attributes`, `/sample-attribute-values`
- `GET/POST /soil-profiles`, `/soil-profile-attributes`, `/soil-profile-attribute-values`

### Upload & review workflow
1. `GET /upload/template-columns` — selectable columns (structure + EAV attributes + variables)
2. `GET /upload/template` — generate/download the `.xlsx`/`.csv` template
3. `POST /upload/validate` — dry-run validation (no DB writes); returns an `upload_id` (Editor/Admin)
4. `POST /upload/import` — stage the validated rows as a **PENDING** `DatasetSubmission`
   (Editor/Admin). **Nothing is written to the domain tables yet.**
5. `GET /submissions`, `GET /submissions/{id}/rows` — review queue (database_editor/Admin)
6. `POST /submissions/{id}/approve` — replays the staged rows into the database and applies the
   submitted metadata ficha; this is the step that actually publishes the data
7. `POST /submissions/{id}/reject` — discards nothing (rows are kept as evidence), emails the
   submitter the reason

The submitter and reviewers are emailed at each step when SMTP is configured (see `.env.example`);
without it, emails are only logged, so local dev/CI never breaks.

### Dataverse import
- `GET /dataverse/preview?doi=…` — read-only preview of a Dataverse dataset's files
- `GET /dataverse/file` — download one file; the frontend feeds it into the exact same
  `/upload/validate` mapping flow as a manually picked file (Dataverse content is never
  auto-imported or auto-mapped)

### Download
- `GET /download/estimate` — real record/sample/site counts for the given filters
- `GET /download/export` — filtered export (`.xlsx`/`.csv`)
- `GET /download/audit` — download history

### Users (admin)
- `GET /users`, `GET /users/search`, `GET /users/roles`, `PUT /users/{id}/roles`

### Health
- `GET /health` — service status (public, no auth)

## Access control & FAIR metadata

Every *data* endpoint (dashboard, map, visualization, download — never the metadata/catalogue
endpoints) filters results through `accessible_project_ids()` in `src/dependencies/access.py`,
based on the caller's identity and each project's `project_metadata.access_level`:

| `access_level` | Who can see the data |
| --- | --- |
| `PUBLIC` | Any authenticated user (and anonymous callers, on the public teaser endpoint) |
| `EMBARGOED` | Public once `embargo_until` has passed; before that, `authorized_users` or the team (`editor`/`database_editor`) |
| `RESTRICTED` / `LICENSED` | `authorized_users`, or the team |
| `PRIVATE` / `CONFIDENTIAL` | `authorized_users` only |
| *(no metadata row yet)* | `PUBLIC` (legacy default, see `DEFAULT_LEVEL_NO_METADATA`) |

`admin` always bypasses this check. Whoever submits a dataset is automatically kept in its
`authorized_users` (see `routes/submissions.py`), so an uploader can always see their own data.

Any new endpoint that reads project-scoped data (sites, samples, measurements, attributes…) must
apply this same filter — see the pattern already used in `routes/dashboard.py` / `routes/map.py`.
For the full rationale behind the metadata model (FAIR principles, the access-level design
discussion), see [`docs/PROPUESTA_METADATOS.md`](docs/PROPUESTA_METADATOS.md); for the complete
data model, see [`docs/DATA_DICTIONARY_DATABASE.md`](docs/DATA_DICTIONARY_DATABASE.md).

## Tests

```bash
pytest
```

(`pytest.ini` points at `src/tests`; run from the repository root.)

## Adding a New Endpoint

1. Add Pydantic schemas in `src/schemas/<feature>.py` (Base / Create / Read / Update, following
   the existing modules).
2. Add a router in `src/routes/<feature>.py`. Use `SessionLocal()` in a `try/finally`, and if the
   endpoint reads project-scoped data, apply `accessible_project_ids()` as described above.
3. Register the router in `src/main.py`'s `app.include_router(...)` block. It is included with
   `dependencies=auth_dep` like every other router unless the endpoint is deliberately public
   (see the `dashboard_public_router` exception).
4. Gate writes with `Depends(require_editor)` / `require_database_editor` / `require_admin` from
   `dependencies/auth.py`, matching the [role table](#authentication--roles) above.
5. Add tests under `src/tests/test_<feature>.py`.

## License

This project is part of the CGIAR / Alliance Bioversity International — CIAT initiative for
Multifunctional Landscapes research.

## Related Resources

- [MFL Soil Data Frontend](https://github.com/Dan3s/mflsoildata_frontend)
- [mflsoildata_orm](https://github.com/Dan3s/mflsoildata_orm)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Keycloak Documentation](https://www.keycloak.org/documentation)
