import os
from fastapi import Depends, FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.routing import APIRoute
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from dependencies.auth import get_current_user

# Import routes
from routes.projects import router as projects_router
from routes.sites import router as sites_router
from routes.samples import router as samples_router
from routes.measurements import router as measurements_router
from routes.lookups import router as lookups_router
from routes.dashboard import router as dashboard_router, public_router as dashboard_public_router
from routes.visualization import router as visualization_router
from routes.map import router as map_router
from routes.upload import router as upload_router
from routes.download import router as download_router
from routes.site_attributes import router as site_attributes_router
from routes.sample_attributes import router as sample_attributes_router
from routes.soil_profiles import router as soil_profiles_router
from routes.submissions import router as submissions_router
from routes.users import router as users_router
from routes.dataverse import router as dataverse_router
from routes.chat import router as chat_router

# Create FastAPI app
DESCRIPTION = """\
REST API for soil and greenhouse-gas data across CGIAR projects.

**Data model.** A *project* (dataset) holds *sites*; each site has *sampling events*;
each event has *samples*; each sample has *measurements* of catalogue *variables*.
Sites, samples and soil profiles also carry free-form *attributes*.

**Access control.** Every data endpoint filters by the project's `access_level`, so
two callers can legitimately see different results. Metadata stays visible even when
the data itself is restricted (FAIR A2).

**Getting oriented.** Start at `/projects/catalogue` for the datasets and their
metadata, `/variables` for what can be measured, and `/dashboard/records` for the
measurements themselves.

**Authentication.** All endpoints require a Keycloak bearer token. Roles, least to
most privileged: `viewer`, `editor`, `database_editor`, `admin`.
"""


def _operation_id(route: APIRoute) -> str:
    """Readable, stable operation ids for the OpenAPI spec.

    FastAPI's default mangles the path into the name (``get_variables_variables_get``).
    Tools that generate clients — and AI agents that read the spec — surface the
    operationId as the name of the callable, so it is worth deriving it from the
    endpoint function alone: ``list_soil_profiles`` -> ``listSoilProfiles``.
    """
    head, *rest = route.name.split("_")
    return head + "".join(word.capitalize() for word in rest)


app = FastAPI(
    title="CGIAR Soil - GHG Datahub API",
    description=DESCRIPTION,
    version="0.0.1",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    generate_unique_id_function=_operation_id,
)

# The dataset endpoints return long, highly repetitive JSON (one object per
# measurement) that compresses ~34x. Without this, the row caps in the explorer
# are bounded by transfer size rather than by anything the browser cannot handle.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# Health check endpoint
@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check endpoint
    """
    return {"status": "OK", "version": "0.0.1"}


# Root redirect to docs
@app.get("/", include_in_schema=False)
async def root():
    """
    Redirect to API documentation
    """
    return RedirectResponse(url="/docs")


# Register routers
# All data routers require a valid Keycloak token (any authenticated user / Viewer+).
# Write endpoints additionally enforce editor/admin via per-route dependencies.
# Health and root stay public.
# Lookups go first so literal catalog routes (e.g. /sites/options, /projects/options)
# win over the CRUD path params (/sites/{site_id}, /projects/{project_id}); otherwise
# "options" is parsed as an id and the request fails with 422.
auth_dep = [Depends(get_current_user)]
app.include_router(lookups_router, dependencies=auth_dep)
app.include_router(projects_router, dependencies=auth_dep)
app.include_router(sites_router, dependencies=auth_dep)
app.include_router(samples_router, dependencies=auth_dep)
app.include_router(measurements_router, dependencies=auth_dep)
app.include_router(dashboard_router, dependencies=auth_dep)
app.include_router(dashboard_public_router)  # no auth — landing page stats teaser
app.include_router(visualization_router, dependencies=auth_dep)
app.include_router(map_router, dependencies=auth_dep)
app.include_router(upload_router, dependencies=auth_dep)
app.include_router(download_router, dependencies=auth_dep)
app.include_router(site_attributes_router, dependencies=auth_dep)
app.include_router(sample_attributes_router, dependencies=auth_dep)
app.include_router(soil_profiles_router, dependencies=auth_dep)
app.include_router(submissions_router, dependencies=auth_dep)
app.include_router(users_router, dependencies=auth_dep)
app.include_router(dataverse_router, dependencies=auth_dep)
app.include_router(chat_router, dependencies=auth_dep)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
