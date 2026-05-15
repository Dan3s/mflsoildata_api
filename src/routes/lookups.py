from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
from sqlalchemy import or_
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.enums import Matrix
from mflsoildata_orm.models import Lab, Measurement, Project, Sample, SampleType, SamplingEvent, Site, Variable
from schemas.lookups import LookupItem, MatrixItem, VariableLookupItem

router = APIRouter(tags=["Lookups"])


@router.get("/projects/options", response_model=List[LookupItem])
async def get_project_options():
    db = SessionLocal()
    try:
        projects = db.query(Project).order_by(Project.name.asc()).all()
        return [LookupItem(id=project.id, label=project.name) for project in projects]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/projects/summary")
async def get_project_summary():
    db = SessionLocal()
    try:
        total_projects = db.query(Project).count()
        total_sites = db.query(Site).count()
        total_samples = db.query(Sample).count()
        total_measurements = db.query(Measurement).count()
        return {
            "projects": total_projects,
            "sites": total_sites,
            "samples": total_samples,
            "measurements": total_measurements,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/sites/options", response_model=List[LookupItem])
async def get_site_options(project_id: Optional[int] = Query(None)):
    db = SessionLocal()
    try:
        query = db.query(Site)
        if project_id is not None:
            query = query.filter(Site.project_id == project_id)
        sites = query.order_by(Site.site_code.asc()).all()
        return [LookupItem(id=site.id, label=site.site_code) for site in sites]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/sample-types", response_model=List[LookupItem])
async def get_sample_types():
    db = SessionLocal()
    try:
        sample_types = db.query(SampleType).order_by(SampleType.name.asc()).all()
        return [LookupItem(id=sample_type.id, label=sample_type.name) for sample_type in sample_types]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/matrices", response_model=List[MatrixItem])
async def get_matrices():
    return [MatrixItem(code=item.value, label=item.value.replace("_", " ").title()) for item in Matrix]


@router.get("/variables", response_model=List[VariableLookupItem])
async def get_variables(
    matrix: Optional[str] = Query(None),
    domain: Optional[str] = Query(None),
    value_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    db = SessionLocal()
    try:
        query = db.query(Variable)
        if matrix is not None:
            query = query.filter(Variable.matrix == matrix)
        if domain is not None:
            query = query.filter(Variable.domain == domain)
        if value_type is not None:
            query = query.filter(Variable.value_type == value_type)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Variable.name.ilike(pattern),
                    Variable.variable_code.ilike(pattern),
                    Variable.description.ilike(pattern),
                )
            )
        variables = query.order_by(Variable.name.asc()).all()
        return [
            VariableLookupItem(
                id=variable.id,
                label=variable.name,
                variable_code=variable.variable_code,
                domain=variable.domain,
                matrix=variable.matrix,
                value_type=variable.value_type,
            )
            for variable in variables
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/variables/options", response_model=List[LookupItem])
async def get_variable_options(
    project_id: Optional[int] = Query(None),
    matrix: Optional[str] = Query(None),
    domain: Optional[str] = Query(None),
    value_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    db = SessionLocal()
    try:
        query = db.query(Variable)
        if project_id is not None:
            query = (
                query.join(Measurement, Measurement.variable_id == Variable.id)
                .join(Sample, Measurement.sample_id == Sample.id)
                .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
                .filter(SamplingEvent.project_id == project_id)
            )
        if matrix is not None:
            query = query.filter(Variable.matrix == matrix)
        if domain is not None:
            query = query.filter(Variable.domain == domain)
        if value_type is not None:
            query = query.filter(Variable.value_type == value_type)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Variable.name.ilike(pattern),
                    Variable.variable_code.ilike(pattern),
                    Variable.description.ilike(pattern),
                )
            )
        variables = query.distinct().order_by(Variable.name.asc()).all()
        return [LookupItem(id=variable.id, label=variable.name) for variable in variables]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/measurement-statuses", response_model=List[LookupItem])
async def get_measurement_statuses():
    return [
        LookupItem(id=1, label="Pendiente"),
        LookupItem(id=2, label="Validado"),
        LookupItem(id=3, label="Revisado"),
        LookupItem(id=4, label="Rechazado"),
    ]


@router.get("/laboratories", response_model=List[LookupItem])
async def get_laboratories(search: Optional[str] = Query(None)):
    db = SessionLocal()
    try:
        query = db.query(Lab)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Lab.name.ilike(pattern),
                    Lab.lab_code.ilike(pattern),
                    Lab.organization.ilike(pattern),
                )
            )
        labs = query.order_by(Lab.name.asc()).all()
        return [LookupItem(id=lab.id, label=lab.name) for lab in labs]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()
