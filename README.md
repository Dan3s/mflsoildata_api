# MFL Soil Data — API

REST API to manage soil research data from multiple Multifunctional Landscapes (MFL) projects
across Latin America, built with FastAPI and the `mflsoildata_orm` ORM on PostgreSQL + PostGIS.

## 📋 Overview

The API powers the [MFL Soil Data frontend](https://github.com/Dan3s/mflsoildata_frontend). It
exposes projects, sites, sampling events, samples, variables and measurements, plus aggregated
endpoints for the dashboard, visualizations and map, and a bulk **upload** flow (template →
dry-run validation → transactional import) and filtered **download**. All data endpoints are
protected with Keycloak-issued JWTs and enforce role-based permissions (Admin / Editor / Viewer).

## ⚙️ Requirements

- Python 3.10+
- PostgreSQL 12+ with PostGIS
- Keycloak 25+ (tested on 26.2.5)
- pip / virtualenv

## 🛠️ Technology Stack

- **Framework**: FastAPI 0.104.1
- **Server**: Uvicorn
- **ORM**: SQLAlchemy 2 via `mflsoildata_orm`
- **Validation**: Pydantic 2
- **Database**: PostgreSQL + PostGIS
- **Auth**: Keycloak JWT validation (`pyjwt[crypto]`, RS256 via JWKS)
- **Files**: openpyxl (xlsx), python-multipart (uploads)
- **Docs**: Swagger UI & ReDoc (auto-generated)

## 📦 Installation

### 1. Clone the Repository

```bash
git clone https://github.com/Dan3s/mflsoildata_api.git
cd mflsoildata_api
```

### 2. Create a Virtual Environment

```bash
python -m venv .venv

# On Linux/macOS
source .venv/bin/activate

# On Windows
.venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` references the ORM package; install it from its repository if it is not
already available:

```bash
pip install git+https://github.com/Dan3s/mflsoildata_orm
```

### 4. Environment Configuration

Create a `.env` file in the repository root:

```bash
# Database (PostgreSQL + PostGIS)
DATABASE_URL=postgresql://user:password@localhost:5432/soil_platform

# Keycloak (JWT validation)
KEYCLOAK_ISSUER=http://localhost:8080/realms/mfl
# Only used when KEYCLOAK_VERIFY_AUD=true (requires an Audience mapper in Keycloak)
KEYCLOAK_AUDIENCE=mfl-api
KEYCLOAK_VERIFY_AUD=false
```

## 🚀 Running the Project

### Development

```bash
cd src
python main.py            # uvicorn with reload, on port 8000
# or:  uvicorn main:app --reload
```

The API will be available at `http://localhost:8000`.

### Interactive Documentation

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

### Production

Run behind a process manager and a reverse proxy (e.g. Nginx):

```bash
cd src
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

Ensure the PostgreSQL/PostGIS database exists, the `.env` is configured with production values,
and `KEYCLOAK_ISSUER` points to the production Keycloak realm.

## 🔑 Authentication & Roles

All data routers require a valid Keycloak access token (`Authorization: Bearer …`); `GET /health`
and `/` stay public. The token signature is verified against the realm JWKS, and realm roles are
read from `realm_access.roles`.

| Role | Permissions |
| --- | --- |
| `viewer` | Read (GET) everything |
| `editor` | Viewer + create/update (POST/PUT), incl. data upload and `POST /variables` |
| `admin`  | Everything, including delete (DELETE) |

Authorization is implemented in `src/dependencies/auth.py` (`get_current_user`, `require_editor`,
`require_admin`). New self-registered users default to `viewer`; role changes are made in the
Keycloak Admin Console.

## 📁 Project Structure

```
mflsoildata_api/
├── src/
│   ├── main.py                 # FastAPI entry point + router registration
│   ├── config.py               # App configuration
│   ├── routes/                 # Endpoints
│   │   ├── projects.py
│   │   ├── sites.py
│   │   ├── samples.py
│   │   ├── measurements.py
│   │   ├── lookups.py          # options/catalogs + POST /variables, /units, /variable-domains
│   │   ├── dashboard.py        # summary / records / export
│   │   ├── visualization.py    # timeseries / by-site / correlation / export
│   │   ├── map.py              # spatial summary, sites, site detail, export
│   │   ├── upload.py           # template-columns / template / validate / import
│   │   └── download.py         # estimate / export / recent
│   ├── schemas/                # Pydantic schemas
│   ├── services/
│   │   └── etl.py              # Upload parsing, validation and import (ETL) engine
│   ├── dependencies/
│   │   └── auth.py             # Keycloak JWT validation + role guards
│   └── tests/                  # Test suite
├── requirements.txt            # Python dependencies
├── .env                        # Environment variables (not in git)
└── README.md                   # This file
```

## 📖 Main Endpoints

### Projects / Sites / Samples / Measurements (CRUD)
- `GET/POST/PUT/DELETE /projects/…`, `GET/POST/PUT /sites/…`, `/samples/…`, `GET/POST /measurements/…`
  (reads = Viewer+; writes = Editor/Admin; project delete = Admin)

### Lookups
- `GET /projects/options`, `GET /sites/options?project_id=…`, `GET /sample-types`, `GET /matrices`
- `GET /variables`, `GET /variables/options`, `GET /units`, `GET /variable-domains`
- `POST /variables` (Editor/Admin) — register a new variable in the catalog
- `GET /measurement-statuses`, `GET /laboratories`

### Dashboard
- `GET /dashboard/summary` — overview metrics
- `GET /dashboard/records` — unified table with joins and filters
- `GET /dashboard/export` — CSV export of filtered results

### Visualization
- `GET /visualization/timeseries` — period-aggregated series (supports `site_id`)
- `GET /visualization/by-site` — aggregated comparison across sites
- `GET /visualization/correlation` — aligned value pairs + correlation (supports `site_id`)
- `GET /visualization/export` — CSV export of the active chart

### Map
- `GET /map/summary`, `GET /map/sites`, `GET /map/sites/{site_id}`, `GET /map/export`

### Upload (bulk load)
- `GET /upload/template-columns` — selectable columns (structure + EAV attributes + variables)
- `GET /upload/template` — generate/download the `.xlsx`/`.csv` template
- `POST /upload/validate` — dry-run validation (no DB writes); returns an `upload_id` (Editor/Admin)
- `POST /upload/import` — commit a validated upload (transactional, idempotent) (Editor/Admin)

### Download
- `GET /download/estimate` — real record/sample/site counts for the given filters
- `GET /download/export` — filtered export (`.xlsx`/`.csv`)
- `GET /download/recent` — recent downloads (placeholder until history is persisted)

### Health
- `GET /health` — service status (public)

## 🔄 Recommended Data Flow

1. Create a **Project**
2. Create **Sites** with coordinates (latitude, longitude)
3. Create **Sampling Events** (visits/campaigns)
4. Create **Samples** (soil, leaf, grain) linked to events
5. Create **Measurements** with variables (pH, SOC, etc.) for each sample

The bulk **upload** flow automates this from a filled template, mapping each column to its target
table (structure → projects/sites/events/samples; EAV → attribute values; variables → measurements).

## 🗄️ Database

The schema is optimized for:
- EAV (Entity-Attribute-Value) for flexible attributes
- Multiple matrices (SOIL, LEAF, GRAIN, profiles/horizons)
- Full traceability (timestamps, source metadata)
- Georeferencing with PostGIS

See `docs/DATA_DICTIONARY_DATABASE.md` for the complete data model and ETL guidance.

## 🧪 Tests

```bash
cd src
pytest tests/
```

## 📄 License

This project is part of the CGIAR / Alliance Bioversity International — CIAT initiative for
Multifunctional Landscapes research.

## 🔗 Related Resources

- [MFL Soil Data Frontend](https://github.com/Dan3s/mflsoildata_frontend)
- [mflsoildata_orm](https://github.com/Dan3s/mflsoildata_orm)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Keycloak Documentation](https://www.keycloak.org/documentation)
