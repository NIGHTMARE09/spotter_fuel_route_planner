"""Place-name handling shared by the API and the offline data scripts (no Django imports)."""

import re
import unicodedata

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
STATE_CODES = frozenset(STATE_NAMES.values())

# Legal/statistical suffixes the Census appends to place names
# ("Salt Lake City city", "Indianapolis city (balance)", "Big Cabin town").
_GOV_SUFFIXES = (
    "(balance)", "consolidated government", "metropolitan government", "unified government",
    "metro government", "urban county",
)
# Stripped at most once, so "Kansas City city" -> "kansas city", not "kansas".
# Longer suffixes first: "Canton charter township" -> "canton", not "canton charter".
_SUFFIXES = (
    "city and borough", "charter township", "municipality", "plantation", "township", "borough",
    "village", "city", "town", "cdp", "ccd", "comunidad", "zona urbana", "unorganized territory",
    "gore", "grant", "purchase", "location",
)

_ABBREV = (
    (r"\bst\.?\s", "saint "),
    (r"\bste\.?\s", "sainte "),
    (r"\bft\.?\s", "fort "),
    (r"\bmt\.?\s", "mount "),
    (r"\bmc\s", "mc"),  # "Mc Alpin" -> "mcalpin"
)


def strip_census_suffix(name: str) -> str:
    """'Salt Lake City city' -> 'Salt Lake City'. Case is kept. Only for Census names."""
    s = name.strip()
    changed = True
    while changed:
        changed = False
        for suf in _GOV_SUFFIXES:
            if s.lower().endswith(" " + suf):
                s = s[: -len(suf) - 1].rstrip()
                changed = True
    for suf in _SUFFIXES:
        if s.lower().endswith(" " + suf):
            return s[: -len(suf) - 1].rstrip()
    return s


def normalize_place(name: str, strip_suffix: bool = False) -> str:
    """Canonical key for a US place name: lowercase ASCII, expanded abbreviations.

    `strip_suffix` removes Census legal suffixes; use it for Gazetteer names,
    not for human input, where "Town" or "Village" is part of the name
    ("Greenwich Village" is not "Greenwich").
    """
    s = strip_census_suffix(name) if strip_suffix else name
    # "Cañon City" -> "canon city", not "caon city"
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().strip().lower()
    s = s.replace("-", " ").replace("/", " ")
    for pat, rep in _ABBREV:
        s = re.sub(pat, rep, s + " ").strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def lookup_keys(name: str) -> list[str]:
    """Gazetteer keys to try for a human-written city name, most specific first.

    Only spelling variants, never a different place: spacing ("La Place" ->
    "laplace", "De Forest" -> "deforest") and a leading/trailing "City"
    ("New York City" -> "new york", "Boise" -> "boise city").
    """
    k = normalize_place(name)
    keys = [k, k.replace(" ", "")]
    keys.append(k[: -len(" city")] if k.endswith(" city") else k + " city")
    return list(dict.fromkeys(x for x in keys if x))
