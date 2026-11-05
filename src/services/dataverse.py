"""Client for importing datasets from a Dataverse installation (e.g. Harvard
Dataverse) by DOI.

Read-only: fetches a dataset's Citation/Geospatial metadata and its file list
(Native API), and streams a single file's bytes (Data Access API). Never
infers column mappings — the fetched file is handed to the same upload/mapping
pipeline any other file goes through (``services.etl``).

Dataverse's field JSON is not flat: a field is either
- primitive: ``{"value": "..."}`` (or a list of strings when ``multiple``)
- controlledVocabulary: same shape as primitive, just a constrained value
- compound: ``multiple`` is (almost) always true; ``value`` is a list of
  dicts, each dict keyed by sub-field name, each of *those* wrapped the same
  way (``{"typeName", "multiple", "typeClass", "value"}``) one level deeper.

Env: DATAVERSE_BASE_URL (default the public demo instance), DATAVERSE_API_TOKEN
(optional server-wide fallback; normally the caller supplies their own token
for a restricted dataset instead).
"""

import os
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

DATAVERSE_BASE_URL = os.getenv("DATAVERSE_BASE_URL", "https://demo.dataverse.org").rstrip("/")
DATAVERSE_API_TOKEN = os.getenv("DATAVERSE_API_TOKEN") or None


class DataverseError(Exception):
    """Dataverse is unreachable, refused the request, or returned something we
    couldn't parse. Message is safe to show to the user."""


class DataverseNotFoundError(DataverseError):
    """No dataset exists at the given DOI (or the caller lacks access to see it)."""


def _headers(token: Optional[str]) -> Dict[str, str]:
    tok = token or DATAVERSE_API_TOKEN
    return {"X-Dataverse-key": tok} if tok else {}


# ---------------------------------------------------------------------------
# Field-shape helpers
# ---------------------------------------------------------------------------


def _fields_by_name(fields: List[dict]) -> Dict[str, dict]:
    return {f["typeName"]: f for f in fields if "typeName" in f}


def _primitive(field: Optional[dict]) -> Optional[str]:
    """Value of a primitive/controlledVocabulary field (first value if ``multiple``)."""
    if not field:
        return None
    value = field.get("value")
    if field.get("multiple"):
        return value[0] if value else None
    return value


def _primitive_list(field: Optional[dict]) -> List[str]:
    if not field:
        return []
    value = field.get("value")
    if value is None:
        return []
    if not field.get("multiple"):
        return [value]
    return list(value)


def _compound_entries(field: Optional[dict]) -> List[dict]:
    """Raw list of compound entries (each a dict of wrapped sub-fields)."""
    if not field:
        return []
    value = field.get("value")
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def _sub(entry: dict, sub_name: str) -> Optional[str]:
    """One sub-field's value from one compound entry."""
    sub = entry.get(sub_name)
    return sub.get("value") if sub else None


def _coerce_date(raw: Optional[str], *, end_of_period: bool) -> Optional[str]:
    """Dataverse's timePeriodCovered sub-fields are free text — depositors
    routinely enter just a year ("2013") or a year-month ("2013-06"), not a
    full date. Our ficha's temporal_coverage_start/end is a real date (the
    API rejects anything else), so a partial value is widened to the
    start/end of the period it names rather than passed through as-is, which
    would otherwise fail validation only at final import time, long after
    this got silently prefilled."""
    if not raw:
        return None
    raw = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return _last_day_of_month(raw) if end_of_period else f"{raw}-01"
    if re.fullmatch(r"\d{4}", raw):
        return f"{raw}-12-31" if end_of_period else f"{raw}-01-01"
    return None


def _last_day_of_month(year_month: str) -> str:
    year, month = (int(p) for p in year_month.split("-"))
    next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (next_month_first - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Citation / Geospatial block -> our ficha shape
# ---------------------------------------------------------------------------


def _extract_ficha(citation_fields: List[dict], geospatial_fields: Optional[List[dict]]) -> Dict[str, Any]:
    by_name = _fields_by_name(citation_fields)

    authors = []
    for entry in _compound_entries(by_name.get("author")):
        name = _sub(entry, "authorName")
        affiliation = _sub(entry, "authorAffiliation")
        if not name:
            continue
        authors.append(f"{name} ({affiliation})" if affiliation else name)

    contact_entries = _compound_entries(by_name.get("datasetContact"))
    contact_name = _sub(contact_entries[0], "datasetContactName") if contact_entries else None
    contact_email = _sub(contact_entries[0], "datasetContactEmail") if contact_entries else None
    # A dataset can declare more than one contact — the second one becomes our backup contact.
    contact_name_2 = _sub(contact_entries[1], "datasetContactName") if len(contact_entries) > 1 else None
    contact_email_2 = _sub(contact_entries[1], "datasetContactEmail") if len(contact_entries) > 1 else None

    description_entries = _compound_entries(by_name.get("dsDescription"))
    abstract = _sub(description_entries[0], "dsDescriptionValue") if description_entries else None

    keywords = [_sub(entry, "keywordValue") for entry in _compound_entries(by_name.get("keyword"))]
    keywords = [k for k in keywords if k]

    subjects = _primitive_list(by_name.get("subject"))
    subject = ", ".join(subjects) if subjects else None

    funding_parts = []
    for entry in _compound_entries(by_name.get("grantNumber")):
        agency = _sub(entry, "grantNumberAgency")
        number = _sub(entry, "grantNumberValue")
        piece = " — ".join(p for p in (number, agency) if p)
        if piece:
            funding_parts.append(piece)
    funding_information = "; ".join(funding_parts) or None

    related_publications = []
    for entry in _compound_entries(by_name.get("publication")):
        related_publications.append(_sub(entry, "publicationCitation") or _sub(entry, "publicationURL"))
    related_publications = [p for p in related_publications if p]

    related_datasets = _primitive_list(by_name.get("relatedDatasets")) or None

    data_sources = _primitive_list(by_name.get("dataSources"))
    data_source = "; ".join(data_sources) if data_sources else None

    language_values = _primitive_list(by_name.get("language"))
    language = ", ".join(language_values) if language_values else None

    time_period_entries = _compound_entries(by_name.get("timePeriodCovered"))
    temporal_start = temporal_end = None
    if time_period_entries:
        temporal_start = _coerce_date(_sub(time_period_entries[0], "timePeriodCoveredStart"), end_of_period=False)
        temporal_end = _coerce_date(_sub(time_period_entries[0], "timePeriodCoveredEnd"), end_of_period=True)

    # Stored as a plain display string ("O: .. E: .. N: .. S: ..") rather than a
    # nested object: project_metadata.geographic_bounding_box is JSONB but a
    # string is simplest to show in a normal ficha text field, no special UI.
    bounding_box = None
    if geospatial_fields:
        geo_by_name = _fields_by_name(geospatial_fields)
        bbox_entries = _compound_entries(geo_by_name.get("geographicBoundingBox"))
        if bbox_entries:
            entry = bbox_entries[0]
            west = _sub(entry, "westLongitude")
            east = _sub(entry, "eastLongitude")
            north = _sub(entry, "northLatitude")
            south = _sub(entry, "southLatitude")
            parts = [
                f"O: {west}" if west else None,
                f"E: {east}" if east else None,
                f"N: {north}" if north else None,
                f"S: {south}" if south else None,
            ]
            parts = [p for p in parts if p]
            bounding_box = ", ".join(parts) if parts else None

    return {
        "title": _primitive(by_name.get("title")),
        "abstract": abstract,
        "keywords": keywords or None,
        "authors": authors or None,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "contact_name_2": contact_name_2,
        "contact_email_2": contact_email_2,
        "subject": subject,
        "funding_information": funding_information,
        "related_publications": related_publications or None,
        "related_datasets": related_datasets,
        "data_source": data_source,
        "usage_notes": _primitive(by_name.get("notesText")),
        "language": language,
        "temporal_coverage_start": temporal_start,
        "temporal_coverage_end": temporal_end,
        "geographic_bounding_box": bounding_box,
    }


def _extract_files(latest_version: dict) -> List[Dict[str, Any]]:
    result = []
    for f in latest_version.get("files") or []:
        data_file = f.get("dataFile") or {}
        if not data_file.get("id"):
            continue
        result.append(
            {
                "id": data_file["id"],
                "filename": data_file.get("filename") or f"file_{data_file['id']}",
                "content_type": data_file.get("contentType"),
                "size": data_file.get("filesize"),
                "restricted": bool(f.get("restricted")),
                "original_filename": data_file.get("originalFileName"),
                "categories": f.get("categories") or None,
            }
        )
    return result


def _build_citation(ficha: Dict[str, Any], publication_date: Optional[str], persistent_url: Optional[str]) -> Optional[str]:
    """Best-effort display citation for the preview UI — not authoritative."""
    authors = ficha.get("authors") or []
    parts = [
        ", ".join(authors) if authors else None,
        publication_date,
        f'"{ficha["title"]}"' if ficha.get("title") else None,
        persistent_url,
    ]
    parts = [p for p in parts if p]
    return ". ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_dataset(doi: str, token: Optional[str] = None) -> Dict[str, Any]:
    """Fetch a dataset's citation/geospatial metadata + file list by DOI."""
    doi = (doi or "").strip()
    if not doi:
        raise DataverseError("Debes indicar un DOI.")
    persistent_id = doi if doi.startswith("doi:") else f"doi:{doi}"

    try:
        response = httpx.get(
            f"{DATAVERSE_BASE_URL}/api/datasets/:persistentId/",
            params={"persistentId": persistent_id},
            headers=_headers(token),
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise DataverseError(f"No se pudo contactar a Dataverse: {exc}") from exc

    if response.status_code == 404:
        raise DataverseNotFoundError(f"No se encontró un dataset con el DOI «{doi}».")
    if response.status_code in (401, 403):
        raise DataverseError("Este dataset es restringido: se necesita un token de acceso válido.")
    if response.status_code >= 400:
        raise DataverseError(f"Dataverse respondió {response.status_code}: {response.text[:200]}")

    try:
        payload = response.json()
        data = payload["data"]
        latest = data.get("latestVersion") or data
        blocks = latest.get("metadataBlocks") or {}
        citation_fields = (blocks.get("citation") or {}).get("fields") or []
    except (KeyError, ValueError) as exc:
        raise DataverseError(f"La respuesta de Dataverse no tuvo el formato esperado: {exc}") from exc

    geospatial_fields = (blocks.get("geospatial") or {}).get("fields") or None
    ficha = _extract_ficha(citation_fields, geospatial_fields)
    license_info = latest.get("license") or {}

    return {
        "doi": doi,
        "persistent_id": persistent_id,
        "citation": _build_citation(ficha, data.get("publicationDate"), data.get("persistentUrl")),
        "license": license_info.get("name"),
        "license_uri": license_info.get("uri"),
        "ficha": ficha,
        "files": _extract_files(latest),
        # A dataset-level guestbook (Dataverse's own "answer this form before
        # downloading" feature) blocks the Data Access API call below with a 400
        # for every file, regardless of size or restriction status. Fetch its
        # questions with fetch_guestbook() and resubmit via fetch_datafile's
        # guestbook_response.
        "requires_guestbook": bool(data.get("guestbookId")),
        "guestbook_id": data.get("guestbookId"),
    }


def fetch_guestbook(guestbook_id: int, token: Optional[str] = None) -> Dict[str, Any]:
    """The Guestbook's required-field flags and custom questions, so we can
    render the exact form this dataset configured instead of a generic one."""
    try:
        response = httpx.get(
            f"{DATAVERSE_BASE_URL}/api/guestbooks/{guestbook_id}",
            headers=_headers(token),
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise DataverseError(f"No se pudo contactar a Dataverse: {exc}") from exc

    if response.status_code == 404:
        raise DataverseNotFoundError(f"No se encontró el guestbook {guestbook_id}.")
    if response.status_code >= 400:
        raise DataverseError(f"Dataverse respondió {response.status_code}: {response.text[:200]}")

    data = response.json()["data"]
    return {
        "id": data["id"],
        "name": data.get("name") or "",
        "email_required": bool(data.get("emailRequired")),
        "name_required": bool(data.get("nameRequired")),
        "institution_required": bool(data.get("institutionRequired")),
        "position_required": bool(data.get("positionRequired")),
        "custom_questions": [
            {
                "id": q["id"],
                "question": q.get("question") or "",
                "required": bool(q.get("required")),
                "type": q.get("type") or "text",
            }
            for q in (data.get("customQuestions") or [])
            if not q.get("hidden")
        ],
    }


def fetch_datafile(
    file_id: int,
    token: Optional[str] = None,
    guestbook_response: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, Optional[str], Optional[str]]:
    """Download one file's bytes. Returns (content, filename, content_type).

    When ``guestbook_response`` is given, POSTs it to the same endpoint instead
    of GETting — Dataverse replies 200 with ``{"data": {"signedUrl": ...}}``
    rather than the file itself; that URL 303-redirects to the actual bytes
    (validated against a live guestbook-gated dataset — see the Data Access
    API guide's "Guestbook Response" section).
    """
    try:
        if guestbook_response is not None:
            response = httpx.post(
                f"{DATAVERSE_BASE_URL}/api/access/datafile/{file_id}",
                headers=_headers(token),
                json={"guestbookResponse": guestbook_response},
                timeout=60.0,
            )
        else:
            response = httpx.get(
                f"{DATAVERSE_BASE_URL}/api/access/datafile/{file_id}",
                headers=_headers(token),
                timeout=60.0,
                follow_redirects=True,
            )
    except httpx.HTTPError as exc:
        raise DataverseError(f"No se pudo descargar el archivo: {exc}") from exc

    if response.status_code == 404:
        raise DataverseNotFoundError(f"No se encontró el archivo {file_id} en Dataverse.")
    if response.status_code in (401, 403):
        raise DataverseError("Este archivo es restringido: se necesita un token de acceso válido.")
    if response.status_code == 400:
        detail = None
        try:
            detail = response.json().get("message")
        except ValueError:
            pass
        if detail and "guestbook" in detail.lower():
            raise DataverseError(
                "Este dataset requiere completar un formulario (Guestbook) antes de descargar archivos."
            )
        raise DataverseError(f"Dataverse rechazó la descarga: {detail or response.text[:200]}")
    if response.status_code >= 400:
        raise DataverseError(f"Dataverse respondió {response.status_code} al descargar el archivo.")

    if guestbook_response is not None:
        try:
            signed_url = response.json()["data"]["signedUrl"]
        except (KeyError, ValueError) as exc:
            raise DataverseError("Dataverse no devolvió una URL de descarga firmada.") from exc
        try:
            response = httpx.get(signed_url, timeout=60.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise DataverseError(f"No se pudo descargar el archivo firmado: {exc}") from exc
        if response.status_code >= 400:
            raise DataverseError(f"Dataverse respondió {response.status_code} al seguir la URL firmada.")

    filename = None
    disposition = response.headers.get("content-disposition", "")
    # Signed-URL redirects use RFC 5987 (`filename*=UTF-8''name.txt`); a plain
    # download uses `filename="name.txt"` — check the starred form first since
    # "filename=" is also a substring of "filename*=".
    if "filename*=" in disposition:
        from urllib.parse import unquote

        raw = disposition.split("filename*=", 1)[-1].strip()
        if raw.lower().startswith("utf-8''"):
            raw = raw[len("utf-8''"):]
        filename = unquote(raw)
    elif "filename=" in disposition:
        filename = disposition.split("filename=", 1)[-1].strip().strip('"').strip(";").strip('"')

    return response.content, filename, response.headers.get("content-type")
