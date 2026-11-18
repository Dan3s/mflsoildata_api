"""Place a site that came without coordinates by its administrative areas.

A site needs coordinates *or* its admin 1 and admin 2. In the second case it is put at the
centre of its municipality (admin 2) — or of its department (admin 1) when the municipality is
not recognised — taken from the ``admin_areas`` gazetteer, and ``sites.location_precision``
records that the point is approximate. Nothing here touches a site that has coordinates.

Names rarely match letter for letter ("Municipio de Pitalito", "PITALITO", "Bogotá, D.C."), so
they are compared normalised, and as a last resort by close spelling.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from mflsoildata_orm.models import AdminArea

PRECISION_EXACT = "EXACT"
PRECISION_ADMIN_2 = "ADMIN_2"
PRECISION_ADMIN_1 = "ADMIN_1"

# Words that say what kind of area it is, not which one.
_GENERIC = re.compile(
    r"^(municipio|departamento|provincia|canton|distrito|district|county|state|region|estado|"
    r"comuna|corregimiento)( de| del| of)? "
)

# Country names (normalised, Spanish and English) -> ISO 3166-1 alpha-3, for the countries the
# platform works in or is likely to. The gazetteer itself accepts any country that was loaded.
COUNTRY_ISO3: Dict[str, str] = {
    "colombia": "COL", "peru": "PER", "ecuador": "ECU", "bolivia": "BOL", "venezuela": "VEN",
    "brasil": "BRA", "brazil": "BRA", "argentina": "ARG", "chile": "CHL", "paraguay": "PRY",
    "uruguay": "URY", "mexico": "MEX", "guatemala": "GTM", "honduras": "HND", "nicaragua": "NIC",
    "costa rica": "CRI", "panama": "PAN", "el salvador": "SLV", "cuba": "CUB",
    "republica dominicana": "DOM", "dominican republic": "DOM", "haiti": "HTI",
    "kenya": "KEN", "ethiopia": "ETH", "etiopia": "ETH", "tanzania": "TZA", "uganda": "UGA",
    "rwanda": "RWA", "ruanda": "RWA", "malawi": "MWI", "zambia": "ZMB", "ghana": "GHA",
    "nigeria": "NGA", "mali": "MLI", "senegal": "SEN", "vietnam": "VNM", "indonesia": "IDN",
    "philippines": "PHL", "filipinas": "PHL", "india": "IND", "bangladesh": "BGD", "laos": "LAO",
    "cambodia": "KHM", "camboya": "KHM", "nepal": "NPL", "sri lanka": "LKA",
}


def normalize_name(text: Optional[str]) -> str:
    """Lower case, no accents or punctuation, no leading "Municipio de"…: "Bogotá, D.C." -> "bogota d c"."""
    if not text:
        return ""
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", str(text).lower()) if not unicodedata.combining(c)
    )
    folded = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    folded = _GENERIC.sub("", folded)
    if folded.startswith("bogota"):  # "Bogotá D.C.", "Bogotá, Distrito Capital"
        return "bogota"
    return folded


def country_to_iso3(country: Optional[str]) -> Optional[str]:
    """ISO3 of a country given by name (either language) or by ISO2/ISO3 code."""
    if not country or not str(country).strip():
        return None
    raw = str(country).strip()
    if re.fullmatch(r"[A-Za-z]{3}", raw) and raw.upper() in set(COUNTRY_ISO3.values()):
        return raw.upper()
    return COUNTRY_ISO3.get(normalize_name(raw))


@dataclass
class Placement:
    lat: float
    lon: float
    precision: str  # ADMIN_2 | ADMIN_1
    area: str  # the area it was placed at, as the gazetteer spells it
    note: Optional[str] = None  # why it fell back to admin 1, if it did


@dataclass
class Unplaced:
    reason: str  # NO_COUNTRY | COUNTRY_NOT_LOADED | NOT_FOUND
    detail: str


class AdminResolver:
    """Looks areas up in the gazetteer, remembering what it has already asked."""

    def __init__(self, db: Session):
        self.db = db
        self._areas: Dict[Tuple[str, int], List[Tuple[str, Optional[str], str, float, float]]] = {}
        self._results: Dict[Tuple[str, str, str], object] = {}

    def _load(self, iso3: str, level: int):
        key = (iso3, level)
        if key not in self._areas:
            rows = (
                self.db.query(
                    AdminArea.name_norm,
                    AdminArea.parent_name_norm,
                    AdminArea.name,
                    func.ST_Y(AdminArea.centroid),
                    func.ST_X(AdminArea.centroid),
                )
                .filter(AdminArea.iso3 == iso3, AdminArea.level == level)
                .all()
            )
            self._areas[key] = [(r[0], r[1], r[2], float(r[3]), float(r[4])) for r in rows]
        return self._areas[key]

    def has_country(self, iso3: str) -> bool:
        return bool(self._load(iso3, 1) or self._load(iso3, 2))

    @staticmethod
    def _closest(name: str, candidates: List[str]) -> Optional[str]:
        # A misspelling ("Pitalto"), not a different place: stay strict.
        found = difflib.get_close_matches(name, candidates, n=1, cutoff=0.88)
        return found[0] if found else None

    def _find(self, iso3: str, level: int, name_norm: str, parent_norm: Optional[str] = None):
        rows = self._load(iso3, level)
        if level == 2 and parent_norm:
            # The department narrows the municipality (there are many "San José"). If the
            # department is spelled differently there, fall back to the name alone.
            in_parent = [r for r in rows if r[1] and (r[1] == parent_norm or self._closest(parent_norm, [r[1]]))]
            if in_parent:
                rows = in_parent
        exact = [r for r in rows if r[0] == name_norm]
        if exact:
            return exact[0]
        near = self._closest(name_norm, sorted({r[0] for r in rows}))
        if near:
            return next(r for r in rows if r[0] == near)
        return None

    def resolve(self, country: Optional[str], admin_1: Optional[str], admin_2: Optional[str]):
        """A ``Placement`` for the area, or an ``Unplaced`` that says why not."""
        cache_key = (normalize_name(country), normalize_name(admin_1), normalize_name(admin_2))
        if cache_key in self._results:
            return self._results[cache_key]
        result = self._resolve(country, admin_1, admin_2)
        self._results[cache_key] = result
        return result

    def _resolve(self, country, admin_1, admin_2):
        iso3 = country_to_iso3(country)
        if not iso3:
            if not country or not str(country).strip():
                return Unplaced("NO_COUNTRY", "the country is not given")
            return Unplaced("COUNTRY_NOT_LOADED", f"no administrative areas are loaded for '{country}'")
        if not self.has_country(iso3):
            return Unplaced("COUNTRY_NOT_LOADED", f"no administrative areas are loaded for '{country}'")

        n1, n2 = normalize_name(admin_1), normalize_name(admin_2)
        if n2:
            hit = self._find(iso3, 2, n2, n1 or None)
            if hit:
                return Placement(hit[3], hit[4], PRECISION_ADMIN_2, hit[2])
        if n1:
            hit = self._find(iso3, 1, n1)
            if hit:
                note = f"'{admin_2}' was not found in {admin_1}" if n2 else None
                return Placement(hit[3], hit[4], PRECISION_ADMIN_1, hit[2], note)
        wanted = " / ".join(x for x in (admin_1, admin_2) if x and str(x).strip())
        return Unplaced("NOT_FOUND", f"'{wanted}' is not a known area of {country}")
