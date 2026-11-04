"""Data-access policy based on project ``access_level`` (FAIR privacy).

Metadata (the catalogue / ficha) stays visible to everyone — FAIR A2 says
metadata remains accessible even when the data is restricted. Only DATA
endpoints (dashboard, download, map, visualization) filter by these rules.

Default policy (adjust once the access model is confirmed with the team):
  - admin                : everything.
  - PUBLIC               : any authenticated user.
  - EMBARGOED            : public once ``embargo_until`` has passed; before that,
                           only the team (editor/database_editor) or authorized users.
  - RESTRICTED / LICENSED: authorized_users, or the team (editor/database_editor).
  - PRIVATE / CONFIDENTIAL: authorized_users only.
  - project without a metadata record: ``DEFAULT_LEVEL_NO_METADATA``.
"""

from datetime import date
from typing import Optional, Set

from mflsoildata_orm.models import Project, ProjectMetadata

# Untagged (no metadata) projects: PUBLIC keeps legacy data visible; switch to
# "PRIVATE" for a privacy-first default once every project has a metadata record.
DEFAULT_LEVEL_NO_METADATA = "PUBLIC"

_TEAM_ROLES = ("editor", "database_editor")


def chatbot_project_ids(db) -> Set[int]:
    """Project ids the chat assistant may query. Default-deny, no exceptions —
    not even for admin: the platform's real datasets are catalogued for display
    only, and must never be read by a model, so a project is only included
    here if its metadata explicitly opts in with ``notes.chatbot_test_data``.
    Intended for small, clearly-labelled test/demo projects only.
    """
    return {
        meta.project_id
        for meta in db.query(ProjectMetadata).all()
        if isinstance(meta.notes, dict) and meta.notes.get("chatbot_test_data") is True
    }


def accessible_project_ids(db, user) -> Optional[Set[int]]:
    """Project ids whose DATA ``user`` may see. ``None`` means all (admin)."""
    roles = list(getattr(user, "roles", None) or [])
    if "admin" in roles:
        return None

    email = (getattr(user, "email", None) or "").lower()
    is_team = any(r in roles for r in _TEAM_ROLES)
    today = date.today()

    metas = {m.project_id: m for m in db.query(ProjectMetadata).all()}
    allowed: Set[int] = set()
    for (pid,) in db.query(Project.id).all():
        meta = metas.get(pid)
        level = (meta.access_level if meta else DEFAULT_LEVEL_NO_METADATA) or "PRIVATE"
        authorized = {
            (e or "").lower()
            for e in ((meta.authorized_users if meta else None) or [])
        }

        if level == "PUBLIC":
            allowed.add(pid)
        elif level == "EMBARGOED":
            if meta and meta.embargo_until and meta.embargo_until <= today:
                allowed.add(pid)
            elif email in authorized or is_team:
                allowed.add(pid)
        elif level in ("RESTRICTED", "LICENSED"):
            if email in authorized or is_team:
                allowed.add(pid)
        else:  # PRIVATE, CONFIDENTIAL, or any unknown value -> authorized only
            if email in authorized:
                allowed.add(pid)

    # No approval filter here on purpose: un-approved uploads are staged in
    # ``submission_row`` and never written to the domain tables, so anything
    # reachable from here is already approved (see ``routes/submissions``).
    return allowed


def project_ids_by_institution(db, institution: Optional[str]) -> Optional[Set[int]]:
    """Projects whose dataset record names ``institution`` as the data source.

    ``None`` means "no institution filter requested" — deliberately different from
    an empty set, which means "that institution owns no projects" and must return
    nothing rather than everything.
    """
    if not institution or not institution.strip():
        return None
    return {
        pid
        for (pid,) in db.query(ProjectMetadata.project_id)
        .filter(ProjectMetadata.data_source == institution.strip())
        .all()
    }


def narrow(allowed: Optional[Set[int]], extra: Optional[Set[int]]) -> Optional[Set[int]]:
    """Combine an access-control set with an optional filter set.

    ``None`` means "no restriction" on either side, so it has to be handled
    explicitly: intersecting with None would wrongly wipe the other set.
    """
    if extra is None:
        return allowed
    if allowed is None:
        return extra
    return allowed & extra
