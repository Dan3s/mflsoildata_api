from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from sqlalchemy.orm import Session
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models.project import Project as ProjectModel
from mflsoildata_orm.models import ProjectMetadata as ProjectMetadataModel, Site as SiteModel
from schemas.projects import ProjectRead, ProjectCreate, ProjectUpdate
from schemas.metadata import QC_FIELDS, ProjectMetadataRead, ProjectMetadataUpsert, DatasetCatalogueItem
from dependencies.auth import CurrentUser, get_current_user, require_admin, require_editor

router = APIRouter(prefix="/projects", tags=["Projects"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/", response_model=List[ProjectRead])
async def get_projects(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
):
    """
    Get all projects with pagination
    """
    db = SessionLocal()
    try:
        projects = db.query(ProjectModel).offset(skip).limit(limit).all()
        return projects
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.get("/catalogue", response_model=List[DatasetCatalogueItem])
async def get_dataset_catalogue():
    """Projects + their FAIR metadata, one row per project ("dataset").

    Left-joined so projects without metadata still appear (as a draft dataset).
    The frontend does the faceting/filtering/sorting/pagination client-side.
    Declared before ``/{project_id}`` so the literal path is matched first.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(ProjectModel, ProjectMetadataModel)
            .outerjoin(ProjectMetadataModel, ProjectMetadataModel.project_id == ProjectModel.id)
            .order_by(ProjectModel.name.asc())
            .all()
        )
        # Countries per project come from its sites (a project can span several).
        countries_by_project: dict = {}
        for pid, country in (
            db.query(SiteModel.project_id, SiteModel.country)
            .filter(SiteModel.country.isnot(None), SiteModel.country != "")
            .distinct()
            .all()
        ):
            countries_by_project.setdefault(pid, set()).add(country)

        items: List[DatasetCatalogueItem] = []
        for project, meta in rows:
            items.append(
                DatasetCatalogueItem(
                    project_id=project.id,
                    code=project.code,
                    name=project.name,
                    countries=sorted(countries_by_project.get(project.id, set())),
                    title=meta.title if meta else None,
                    abstract=meta.abstract if meta else None,
                    access_level=meta.access_level if meta else None,
                    quality_controlled=meta.quality_controlled if meta else None,
                    qc_status=meta.qc_status if meta else None,
                    keywords=meta.keywords if meta else None,
                    expected_matrices=meta.expected_matrices if meta else None,
                    data_source=meta.data_source if meta else None,
                    version=meta.version if meta else None,
                    temporal_coverage_start=meta.temporal_coverage_start if meta else None,
                    temporal_coverage_end=meta.temporal_coverage_end if meta else None,
                    has_metadata=meta is not None,
                    updated_at=(meta.updated_at if meta else project.updated_at),
                )
            )
        return items
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(project_id: int):
    """
    Get a specific project by ID
    """
    db = SessionLocal()
    try:
        project = db.query(ProjectModel).filter(ProjectModel.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        return project
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.post("/", response_model=ProjectRead, dependencies=[Depends(require_editor)])
async def create_project(project: ProjectCreate):
    """
    Create a new project
    """
    db = SessionLocal()
    try:
        db_project = ProjectModel(**project.model_dump())
        db.add(db_project)
        db.commit()
        db.refresh(db_project)
        return db_project
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.put("/{project_id}", response_model=ProjectRead, dependencies=[Depends(require_editor)])
async def update_project(project_id: int, project: ProjectUpdate):
    """
    Update a project
    """
    db = SessionLocal()
    try:
        db_project = db.query(ProjectModel).filter(ProjectModel.id == project_id).first()
        if not db_project:
            raise HTTPException(status_code=404, detail="Project not found")
        
        update_data = project.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_project, field, value)
        
        db.commit()
        db.refresh(db_project)
        return db_project
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ── Project metadata (FAIR) ───────────────────────────────────────────────────


@router.get("/{project_id}/metadata", response_model=ProjectMetadataRead)
async def get_project_metadata(project_id: int):
    """Read the FAIR metadata for a project.

    404 if the project has no metadata yet (the frontend then shows an empty
    form). Readable by any authenticated user.
    """
    db = SessionLocal()
    try:
        project = db.query(ProjectModel).filter(ProjectModel.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        meta = (
            db.query(ProjectMetadataModel)
            .filter(ProjectMetadataModel.project_id == project_id)
            .first()
        )
        if not meta:
            raise HTTPException(status_code=404, detail="Project has no metadata yet")
        return meta
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.put(
    "/{project_id}/metadata",
    response_model=ProjectMetadataRead,
    dependencies=[Depends(require_editor)],
)
async def upsert_project_metadata(
    project_id: int,
    payload: ProjectMetadataUpsert,
    user: CurrentUser = Depends(get_current_user),
):
    """Create or update a project's FAIR metadata (partial update).

    Only the fields present in the payload are changed; the rest keep their
    current value (or DB defaults on first creation).

    The quality-control fields are reserved for the reviewing team
    (database_editor / admin): a plain editor sending any of them gets a 403
    rather than having them silently dropped.
    """
    sent_qc = [f for f in QC_FIELDS if f in payload.model_fields_set]
    if sent_qc and not any(r in (user.roles or []) for r in ("database_editor", "admin")):
        raise HTTPException(
            status_code=403,
            detail=(
                "Los campos de control de calidad los completa el equipo revisor "
                f"(database_editor/admin): {', '.join(sent_qc)}."
            ),
        )

    # license is a required field in the editing UI (FAIR A1: reuse terms need a
    # license). Only enforced when the caller actually touches the field, so a
    # genuine partial update that doesn't include license is unaffected.
    if "license" in payload.model_fields_set and not payload.license:
        raise HTTPException(status_code=422, detail="license es obligatorio.")

    db = SessionLocal()
    try:
        project = db.query(ProjectModel).filter(ProjectModel.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        meta = (
            db.query(ProjectMetadataModel)
            .filter(ProjectMetadataModel.project_id == project_id)
            .first()
        )
        if not meta:
            meta = ProjectMetadataModel(project_id=project_id)
            db.add(meta)

        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(meta, field, value)

        db.commit()
        db.refresh(meta)
        return meta
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.delete("/{project_id}", dependencies=[Depends(require_admin)])
async def delete_project(project_id: int):
    """
    Delete a project
    """
    db = SessionLocal()
    try:
        db_project = db.query(ProjectModel).filter(ProjectModel.id == project_id).first()
        if not db_project:
            raise HTTPException(status_code=404, detail="Project not found")
        
        db.delete(db_project)
        db.commit()
        return {"message": "Project deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
