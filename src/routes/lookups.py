from fastapi import APIRouter, Body, Depends, HTTPException, Query
from typing import List, Optional
from sqlalchemy import or_
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.enums import Matrix, ValueType
from mflsoildata_orm.models import Lab, Measurement, Project, ProjectMetadata, Sample, SampleType, SamplingEvent, Site, Unit, Variable
from schemas.lookups import AgrovocTerm, LookupItem, MatrixItem, VariableLookupItem
from schemas.variables import VariableCreate, VariableRead
from services.agrovoc import search as agrovoc_search
from services.etl import normalize_code
from dependencies.auth import CurrentUser, get_current_user, require_database_editor
from dependencies.access import accessible_project_ids

router = APIRouter(tags=["Lookups"])


@router.get("/institutions/options", response_model=List[LookupItem])
async def get_institution_options():
    """Institutions that own data, taken from the datasets' `data_source` field.

    Used to filter data by the organisation responsible for it. Ids are positional
    because an institution is a plain name here, not a catalogue entity.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(ProjectMetadata.data_source)
            .filter(ProjectMetadata.data_source.isnot(None), ProjectMetadata.data_source != "")
            .distinct()
            .order_by(ProjectMetadata.data_source.asc())
            .all()
        )
        return [LookupItem(id=i + 1, label=name) for i, (name,) in enumerate(rows)]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/lookups/agrovoc", response_model=List[AgrovocTerm])
async def agrovoc_keywords(
    q: str = Query(..., min_length=2, description="Prefix of the keyword (min 2 chars)"),
    lang: str = Query("en", description="'en' or 'es'"),
    limit: int = Query(10, ge=1, le=25),
):
    """Keyword suggestions from FAO's AGROVOC thesaurus.

    Returns an empty list (never an error) if AGROVOC is unreachable, so the
    metadata form degrades to plain free-text entry instead of breaking.
    """
    return agrovoc_search(q, lang, limit)

VALID_VALUE_TYPES = {item.value for item in ValueType}
VALID_MATRICES = {item.value for item in Matrix}


@router.get("/projects/options", response_model=List[LookupItem])
async def get_project_options(user: CurrentUser = Depends(get_current_user)):
    """Projects as {id, label} options, for *data* pickers and filters (map,
    visualization, download, dashboard) — filtered by access_level so a
    project the caller can't see data for isn't offered as if it had any.
    The dataset catalogue lists every project regardless (FAIR A2 metadata),
    via /projects/catalogue, not this endpoint."""
    db = SessionLocal()
    try:
        query = db.query(Project)
        allowed = accessible_project_ids(db, user)
        if allowed is not None:
            query = query.filter(Project.id.in_(allowed))
        projects = query.order_by(Project.name.asc()).all()
        return [LookupItem(id=project.id, label=project.name) for project in projects]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/projects/summary")
async def get_project_summary():
    """Platform-wide counts: projects, sites, samples and measurements."""
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
    """Sites as {id, label} options, optionally narrowed to one project."""
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
    """Sample types (soil, leaf, grain...) as {id, label} options."""
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
    """Matrices the platform recognises (SOIL, LEAF, GRAIN, SOIL_PROFILE, SOIL_HORIZON)."""
    return [MatrixItem(code=item.value, label=item.value.replace("_", " ").title()) for item in Matrix]


@router.get("/variables", response_model=List[VariableLookupItem])
async def get_variables(
    matrix: Optional[str] = Query(None),
    domain: Optional[str] = Query(None),
    value_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    """Catalogue of measurable variables, filterable by matrix, domain, value type or free
    text.
    """
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
    """Variables as {id, label} options; narrow to those actually measured in a project."""
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
        # Disambiguate variables that share a name across matrices (e.g. "Ba Total
        # (mg/kg)" in SOIL / LEAF / GRAIN) by appending the matrix to the label.
        return [
            LookupItem(
                id=variable.id,
                label=f"{variable.name} — {variable.matrix}" if variable.matrix else variable.name,
            )
            for variable in variables
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post("/variables", response_model=VariableRead, status_code=201, dependencies=[Depends(require_database_editor)])
async def create_variable(payload: VariableCreate = Body(...)):
    """Create a new variable in the controlled catalog.

    Used by the upload view's "Nueva variable" form so users can register an
    analyte (with proper metadata) before selecting it as a template column.
    The code is normalized and must be unique (data dictionary 2.2 / 3.11).
    """
    code = normalize_code(payload.variable_code) or normalize_code(payload.name)
    if not code:
        raise HTTPException(status_code=400, detail="Debe indicar 'variable_code' o 'name'.")
    if payload.value_type not in VALID_VALUE_TYPES:
        raise HTTPException(status_code=422, detail=f"value_type inválido. Use uno de: {sorted(VALID_VALUE_TYPES)}")
    if payload.matrix and payload.matrix not in VALID_MATRICES:
        raise HTTPException(status_code=422, detail=f"matrix inválido. Use uno de: {sorted(VALID_MATRICES)}")

    db = SessionLocal()
    try:
        existing = db.query(Variable).filter(Variable.variable_code == code).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Ya existe una variable con código '{code}'.")
        if payload.default_unit_id is not None:
            if db.query(Unit).filter(Unit.id == payload.default_unit_id).first() is None:
                raise HTTPException(status_code=422, detail="default_unit_id no existe.")
        variable = Variable(
            variable_code=code,
            name=payload.name,
            domain=normalize_code(payload.domain) or "CUSTOM",
            matrix=payload.matrix,
            value_type=payload.value_type,
            default_unit_id=payload.default_unit_id,
            description=payload.description,
        )
        db.add(variable)
        db.commit()
        db.refresh(variable)
        return variable
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/units", response_model=List[LookupItem])
async def get_units(search: Optional[str] = Query(None)):
    """Unit catalog for the new-variable form (label = symbol)."""
    db = SessionLocal()
    try:
        query = db.query(Unit)
        if search:
            pattern = f"%{search}%"
            query = query.filter(or_(Unit.symbol.ilike(pattern), Unit.unit_code.ilike(pattern)))
        units = query.order_by(Unit.symbol.asc()).all()
        return [LookupItem(id=unit.id, label=unit.symbol or unit.unit_code) for unit in units]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/variable-domains", response_model=List[str])
async def get_variable_domains():
    """Distinct existing domains, to suggest values in the new-variable form."""
    db = SessionLocal()
    try:
        rows = db.query(Variable.domain).filter(Variable.domain.isnot(None)).distinct().all()
        return sorted({row[0] for row in rows if row[0]})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/countries", response_model=List[str])
async def get_countries():
    """Distinct site countries, for the dashboard and map filters (RF24 / RF29)."""
    db = SessionLocal()
    try:
        rows = db.query(Site.country).filter(Site.country.isnot(None)).distinct().all()
        return sorted({row[0] for row in rows if row[0]})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/measurement-statuses", response_model=List[LookupItem])
async def get_measurement_statuses():
    """Quality statuses a measurement can hold, as {id, label} options."""
    return [
        LookupItem(id=1, label="Pendiente"),
        LookupItem(id=2, label="Validado"),
        LookupItem(id=3, label="Revisado"),
        LookupItem(id=4, label="Rechazado"),
    ]


@router.get("/laboratories", response_model=List[LookupItem])
async def get_laboratories(search: Optional[str] = Query(None)):
    """Laboratories that analysed samples, as {id, label} options."""
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
