import os
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import routes
from routes.projects import router as projects_router
from routes.sites import router as sites_router
from routes.samples import router as samples_router
from routes.measurements import router as measurements_router
from routes.lookups import router as lookups_router
from routes.dashboard import router as dashboard_router
from routes.visualization import router as visualization_router
from routes.map import router as map_router

# Create FastAPI app
app = FastAPI(
    title="MFL Soil Data API",
    description="REST API for managing soil data from multiple projects across Latin America",
    version="0.0.1",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


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
app.include_router(projects_router)
app.include_router(sites_router)
app.include_router(samples_router)
app.include_router(measurements_router)
app.include_router(lookups_router)
app.include_router(dashboard_router)
app.include_router(visualization_router)
app.include_router(map_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
