"""Countries, regions (M12: region of origin is by country of birth) and the M11(e) Western list.

Typed country answers (I15) are matched to a standard name here first; unrecognised text goes to the AI
(T4(c)) and is flagged for staff confirmation.
"""
from __future__ import annotations

import re
import unicodedata

# region -> countries. Regions are the options offered in question 5.4.
REGIONS: dict[str, list[str]] = {
    "UK & Ireland": ["United Kingdom", "Ireland"],
    "Western Europe": ["France", "Germany", "Netherlands", "Belgium", "Luxembourg", "Switzerland", "Austria", "Liechtenstein", "Monaco"],
    "Northern Europe & Scandinavia": ["Sweden", "Norway", "Denmark", "Finland", "Iceland", "Estonia", "Latvia", "Lithuania"],
    "Southern Europe": ["Italy", "Spain", "Portugal", "Greece", "Malta", "Cyprus", "Croatia", "Slovenia", "Andorra", "San Marino"],
    "Eastern Europe": ["Poland", "Czech Republic", "Slovakia", "Hungary", "Romania", "Bulgaria", "Ukraine", "Russia", "Belarus", "Moldova", "Serbia", "Bosnia and Herzegovina", "Montenegro", "North Macedonia", "Albania", "Kosovo", "Georgia", "Armenia", "Azerbaijan", "Turkey"],
    "North America": ["United States", "Canada", "Mexico"],
    "Central & South America": ["Brazil", "Argentina", "Chile", "Colombia", "Peru", "Venezuela", "Ecuador", "Bolivia", "Uruguay", "Paraguay", "Guatemala", "Costa Rica", "Panama", "Honduras", "El Salvador", "Nicaragua", "Cuba", "Dominican Republic", "Jamaica", "Puerto Rico", "Trinidad and Tobago", "Haiti", "Guyana", "Suriname", "Belize"],
    "Australia & New Zealand": ["Australia", "New Zealand"],
    "Pacific Islands": ["Fiji", "Papua New Guinea", "Samoa", "Tonga", "Vanuatu", "Solomon Islands", "New Caledonia", "French Polynesia", "Kiribati", "Micronesia", "Palau", "Nauru", "Tuvalu", "Marshall Islands"],
    "East Asia": ["China", "Hong Kong", "Taiwan", "Japan", "South Korea", "North Korea", "Mongolia", "Macau"],
    "South-East Asia": ["Singapore", "Malaysia", "Indonesia", "Thailand", "Vietnam", "Philippines", "Cambodia", "Laos", "Myanmar", "Brunei", "Timor-Leste"],
    "South Asia": ["India", "Pakistan", "Nepal", "Bangladesh", "Sri Lanka", "Bhutan", "Maldives", "Afghanistan"],
    "Central Asia": ["Kazakhstan", "Uzbekistan", "Kyrgyzstan", "Tajikistan", "Turkmenistan"],
    "Middle East": ["United Arab Emirates", "Saudi Arabia", "Qatar", "Kuwait", "Bahrain", "Oman", "Israel", "Jordan", "Lebanon", "Syria", "Iraq", "Iran", "Yemen", "Palestine"],
    "North Africa": ["Egypt", "Morocco", "Algeria", "Tunisia", "Libya", "Sudan"],
    "Sub-Saharan Africa": ["Nigeria", "Kenya", "South Africa", "Ghana", "Ethiopia", "Tanzania", "Uganda", "Zimbabwe", "Zambia", "Cameroon", "Senegal", "Rwanda", "Somalia", "Mauritius", "Botswana", "Namibia", "Mozambique", "Angola", "Ivory Coast", "Democratic Republic of the Congo", "Madagascar", "Malawi", "Eritrea", "Sierra Leone", "Liberia", "Togo", "Benin", "Mali", "Burkina Faso", "Niger", "Chad", "Gabon", "Republic of the Congo", "Burundi", "South Sudan", "Lesotho", "Eswatini", "Gambia", "Guinea", "Cape Verde", "Seychelles"],
}

# M11(e): who counts as a Western applicant, by region (and the six named East Asian places).
WESTERN_REGIONS = {"UK & Ireland", "Western Europe", "Northern Europe & Scandinavia", "Southern Europe", "North America", "Australia & New Zealand", "Central & South America"}
WESTERN_EXTRA_COUNTRIES = {"China", "Hong Kong", "Singapore", "Japan", "South Korea", "Taiwan"}
# M11(e) (26 Oct 2026): South America, Central America and the Caribbean all count as Western, so the whole
# "Central & South America" region (which holds the Caribbean countries) is Western. Mexico sits in North America.

COUNTRY_REGION: dict[str, str] = {c: r for r, cs in REGIONS.items() for c in cs}
ALL_COUNTRIES = sorted(COUNTRY_REGION)

ALIASES = {
    "uk": "United Kingdom", "u.k.": "United Kingdom", "great britain": "United Kingdom", "britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom", "northern ireland": "United Kingdom",
    "usa": "United States", "u.s.a.": "United States", "us": "United States", "u.s.": "United States", "america": "United States",
    "united states of america": "United States", "the states": "United States",
    "uae": "United Arab Emirates", "emirates": "United Arab Emirates", "dubai": "United Arab Emirates",
    "holland": "Netherlands", "the netherlands": "Netherlands", "deutschland": "Germany", "espana": "Spain", "españa": "Spain",
    "italia": "Italy", "brasil": "Brazil", "korea": "South Korea", "republic of korea": "South Korea", "s. korea": "South Korea",
    "prc": "China", "people's republic of china": "China", "mainland china": "China", "hk": "Hong Kong", "hong kong sar": "Hong Kong",
    "roc": "Taiwan", "chinese taipei": "Taiwan", "vietnam": "Vietnam", "viet nam": "Vietnam", "czechia": "Czech Republic",
    "burma": "Myanmar", "east timor": "Timor-Leste", "ivory coast": "Ivory Coast", "cote d'ivoire": "Ivory Coast", "côte d'ivoire": "Ivory Coast",
    "drc": "Democratic Republic of the Congo", "congo": "Republic of the Congo", "swaziland": "Eswatini", "macedonia": "North Macedonia",
    "russian federation": "Russia", "türkiye": "Turkey", "turkiye": "Turkey", "persia": "Iran", "nz": "New Zealand", "aotearoa": "New Zealand",
    "aus": "Australia", "oz": "Australia", "sri lankan": "Sri Lanka", "indian": "India", "nepalese": "Nepal", "nepali": "Nepal",
    "pakistani": "Pakistan", "bangladeshi": "Bangladesh", "chinese": "China", "japanese": "Japan", "swedish": "Sweden", "german": "Germany",
    "french": "France", "italian": "Italy", "spanish": "Spain", "colombian": "Colombia", "brazilian": "Brazil", "british": "United Kingdom",
    "american": "United States", "canadian": "Canada", "australian": "Australia", "irish": "Ireland", "dutch": "Netherlands",
    "norwegian": "Norway", "danish": "Denmark", "finnish": "Finland", "korean": "South Korea", "taiwanese": "Taiwan", "singaporean": "Singapore",
    "malaysian": "Malaysia", "indonesian": "Indonesia", "thai": "Thailand", "vietnamese": "Vietnam", "filipino": "Philippines",
    "philipines": "Philippines", "phillipines": "Philippines", "mexican": "Mexico", "chilean": "Chile", "argentinian": "Argentina",
    "peruvian": "Peru", "swiss": "Switzerland", "austrian": "Austria", "belgian": "Belgium", "portuguese": "Portugal", "greek": "Greece",
    "turkish": "Turkey", "polish": "Poland", "kenyan": "Kenya", "nigerian": "Nigeria", "south african": "South Africa", "egyptian": "Egypt",
    "sverige": "Sweden", "suomi": "Finland", "norge": "Norway", "danmark": "Denmark", "polska": "Poland", "österreich": "Austria",
    "osterreich": "Austria", "schweiz": "Switzerland", "suisse": "Switzerland", "belgique": "Belgium", "belgie": "Belgium", "nederland": "Netherlands",
    "nippon": "Japan", "nihon": "Japan", "hanguk": "South Korea", "zhongguo": "China", "bharat": "India", "méxico": "Mexico", "mexico": "Mexico",
    "colombia": "Colombia", "perú": "Peru", "chile": "Chile", "türkei": "Turkey", "hellas": "Greece", "ellada": "Greece", "magyarorszag": "Hungary",
    "cesko": "Czech Republic", "česko": "Czech Republic", "hrvatska": "Croatia", "srbija": "Serbia", "россия": "Russia", "rossiya": "Russia",
    "saudi": "Saudi Arabia", "ksa": "Saudi Arabia", "iranian": "Iran", "israeli": "Israel", "lebanese": "Lebanon", "jordanian": "Jordan",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z\s'.&-]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


_LOOKUP = {_norm(c): c for c in ALL_COUNTRIES}
_LOOKUP.update({_norm(k): v for k, v in ALIASES.items()})


def normalise_country(text: str):
    """Return (standard_name or None, confident: bool)."""
    if not text:
        return None, False
    n = _norm(text)
    if n in _LOOKUP:
        return _LOOKUP[n], True
    # strip common prefixes/suffixes
    n2 = re.sub(r"^(the|republic of|kingdom of|state of)\s+", "", n)
    n2 = re.sub(r"\s*\(.*\)$", "", n2).strip(" .")
    if n2 in _LOOKUP:
        return _LOOKUP[n2], True
    # unique prefix / containment match
    hits = {v for k, v in _LOOKUP.items() if len(n2) >= 4 and (k.startswith(n2) or n2 in k)}
    if len(hits) == 1:
        return hits.pop(), True
    return None, False


def region_of(country: str):
    return COUNTRY_REGION.get(country)


def is_western(country: str) -> bool:
    """M11(e)."""
    if not country:
        return False
    if country in WESTERN_EXTRA_COUNTRIES:
        return True
    return COUNTRY_REGION.get(country) in WESTERN_REGIONS
