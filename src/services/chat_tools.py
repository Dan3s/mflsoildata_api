"""Tool functions the chat assistant can call.

Each function here is plain Python: a clear name, a short docstring, and a
JSON-schema-shaped signature — the same shape both Gemini's function calling
and a future MCP server need, so this module stays reusable either way with
no redesign (an MCP server would just be a thin adapter re-exposing TOOLS).

Every tool that reads project-scoped data is restricted to
``chatbot_project_ids()`` — a default-deny allowlist of projects explicitly
marked as chatbot test data (``project_metadata.notes.chatbot_test_data``).
The platform's real datasets are catalogued for display only and must never
be read by a model, so this applies unconditionally — even to admin, and
deliberately independent of the ordinary ``access_level``/RBAC system (see
``dependencies.access``), which protects real data from real users and has
nothing to do with gating a test sandbox. Tools only ever return aggregates
(counts/averages) or metadata, never raw per-sample rows, so a reply can't
leak more than a sentence's worth of numbers.
"""

from typing import Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from dependencies.access import chatbot_project_ids
from mflsoildata_orm.models import (
    Measurement,
    Project,
    ProjectMetadata,
    Sample,
    SamplingEvent,
    Site,
    Variable,
)


def list_datasets(db: Session, user) -> list[dict]:
    """List every dataset (project) with its FAIR metadata: title, abstract,
    countries, access level and expected sample matrices. Restricted to the
    chatbot's test-data allowlist — see module docstring — unlike the regular
    catalogue, which is public under FAIR A2."""
    allowed = chatbot_project_ids(db)
    rows = (
        db.query(Project, ProjectMetadata)
        .outerjoin(ProjectMetadata, ProjectMetadata.project_id == Project.id)
        .filter(Project.id.in_(allowed))
        .order_by(Project.name.asc())
        .all()
    )
    countries_by_project: dict[int, set[str]] = {}
    for pid, country in (
        db.query(Site.project_id, Site.country)
        .filter(Site.country.isnot(None), Site.country != "")
        .distinct()
        .all()
    ):
        countries_by_project.setdefault(pid, set()).add(country)

    return [
        {
            "code": project.code,
            "name": project.name,
            "title": meta.title if meta else None,
            "abstract": meta.abstract if meta else None,
            "access_level": meta.access_level if meta else None,
            "countries": sorted(countries_by_project.get(project.id, set())),
            "expected_matrices": meta.expected_matrices if meta else None,
        }
        for project, meta in rows
    ]


def get_summary(db: Session, user) -> dict:
    """Platform-wide counts the caller may see: datasets, sites, samples,
    measurements, and the catalogue of measurable variables. Restricted to the
    chatbot's test-data allowlist (see module docstring) — deliberately NOT
    also intersected with accessible_project_ids: test data has nothing to
    protect, and gating it by the unrelated access_level/RBAC system would
    just make it invisible to whoever is testing the bot."""
    allowed = chatbot_project_ids(db)
    proj_q = db.query(Project)
    site_q = db.query(Site)
    sample_q = db.query(Sample).join(SamplingEvent, Sample.event_id == SamplingEvent.id)
    meas_q = (
        db.query(Measurement)
        .join(Sample, Measurement.sample_id == Sample.id)
        .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
    )
    if allowed is not None:
        proj_q = proj_q.filter(Project.id.in_(allowed))
        site_q = site_q.filter(Site.project_id.in_(allowed))
        sample_q = sample_q.filter(SamplingEvent.project_id.in_(allowed))
        meas_q = meas_q.filter(SamplingEvent.project_id.in_(allowed))
    return {
        "datasets": proj_q.count(),
        "sites": site_q.count(),
        "samples": sample_q.count(),
        "measurements": meas_q.count(),
        "variables": db.query(Variable).count(),
    }


def query_variable_stats(
    db: Session,
    user,
    variable_name: str,
    country: Optional[str] = None,
    project_code: Optional[str] = None,
) -> dict:
    """Aggregate statistics (count/average/min/max) for one measured variable
    (e.g. pH, SOC, nitrogen), optionally narrowed by country or project code.
    Matches the variable by a case-insensitive partial match on its name or
    code. Only ever returns an aggregate — never raw per-sample values — and
    is restricted to the chatbot's test-data allowlist (see module docstring)."""
    allowed = chatbot_project_ids(db)

    variable = (
        db.query(Variable)
        .filter(
            (Variable.name.ilike(f"%{variable_name}%"))
            | (Variable.variable_code.ilike(f"%{variable_name}%"))
        )
        .first()
    )
    if not variable:
        return {"error": f"No variable matches '{variable_name}'."}

    query = (
        db.query(Measurement)
        .join(Sample, Measurement.sample_id == Sample.id)
        .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
        .outerjoin(Site, Sample.site_id == Site.id)
        .filter(Measurement.variable_id == variable.id, Measurement.value_numeric.isnot(None))
    )
    if allowed is not None:
        query = query.filter(SamplingEvent.project_id.in_(allowed))
    if country:
        query = query.filter(Site.country.ilike(f"%{country}%"))
    if project_code:
        query = query.join(Project, SamplingEvent.project_id == Project.id).filter(
            Project.code.ilike(f"%{project_code}%")
        )

    count, avg, minimum, maximum = query.with_entities(
        sa_func.count(Measurement.id),
        sa_func.avg(Measurement.value_numeric),
        sa_func.min(Measurement.value_numeric),
        sa_func.max(Measurement.value_numeric),
    ).one()

    return {
        "variable": variable.name,
        "variable_code": variable.variable_code,
        "count": count or 0,
        "average": float(avg) if avg is not None else None,
        "min": float(minimum) if minimum is not None else None,
        "max": float(maximum) if maximum is not None else None,
    }


TOOLS = {
    "list_datasets": list_datasets,
    "get_summary": get_summary,
    "query_variable_stats": query_variable_stats,
}

GEMINI_TOOL_DECLARATIONS = [
    {
        "name": "list_datasets",
        "description": (
            "List all datasets (projects) with their FAIR metadata: title, abstract, "
            "countries, access level and expected sample matrices. Use this to answer "
            "'what datasets/projects are there' or 'what data do we have about X'."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_summary",
        "description": (
            "Platform-wide counts the current user may see: number of datasets, sites, "
            "samples, measurements and distinct variables."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "query_variable_stats",
        "description": (
            "Count/average/min/max for one measured variable (e.g. pH, SOC, nitrogen), "
            "optionally filtered by country or project code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "variable_name": {
                    "type": "string",
                    "description": "Variable name or code to search for, e.g. 'pH' or 'SOC'",
                },
                "country": {"type": "string", "description": "Optional country filter"},
                "project_code": {"type": "string", "description": "Optional project code filter"},
            },
            "required": ["variable_name"],
        },
    },
]
