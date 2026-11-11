"""I9 / M4: higher-education institutions and campuses in and around the Melbourne CBD.

Each campus holds the address, its source URL and whether the ADDRESS was verified against an official page
(`verified`, with `verified_on` for the five campuses re-checked on 8 November 2026). Coordinates come from
OpenStreetMap Nominatim (server/data/geocode.json) and are ADDRESS-LEVEL points only: an address verified
against the institution's page is not a provider-verified route or travel time (M4/4.7 travel remains
"needs confirmation" until measured). Where Nominatim only returned a street centroid (no number), the
coordinates are left UNKNOWN (lat/lon None, geo_key None) rather than pretending a campus point: proximity
is then simply not scored for that campus and campus_km is None; no zero-distance or CBD stand-in is used.
'Other' is always last (I9).
"""
from __future__ import annotations

import json
import os

_GEO = json.load(open(os.path.join(os.path.dirname(__file__), "data", "geocode.json")))


def _c(name, group, address, geo_key, source, verified=True, note="", verified_on=None):
    g = (_GEO.get(geo_key) or {}) if geo_key else {}
    return {
        "name": name, "group": group, "address": address, "lat": g.get("lat"), "lon": g.get("lon"),
        "source_url": source, "verified": verified, "verified_on": verified_on, "note": note,
    }


_ADDR_ONLY = "Address verified against the official page on 8 November 2026 (address only, not a measured route)."


INSTITUTIONS = [
    # Universities
    _c("University of Melbourne – Parkville", "Universities", "Grattan Street, Parkville VIC 3010", "Grattan Street, Parkville VIC 3010", "https://www.unimelb.edu.au/contact"),
    _c("University of Melbourne – Southbank", "Universities", "234 St Kilda Road, Southbank VIC 3006", "234 St Kilda Road, Southbank VIC 3006", "https://vtac.edu.au/institutions/unimelb.html"),
    _c("RMIT University – Melbourne City", "Universities", "124 La Trobe Street, Melbourne VIC 3000", "124 La Trobe Street, Melbourne VIC 3000", "https://www.rmit.edu.au/about/our-locations-and-facilities/locations/melbourne-city-campus"),
    _c("RMIT University – Brunswick", "Universities", "25 Dawson Street, Brunswick VIC 3056", "25 Dawson Street, Brunswick VIC 3056", "https://www.rmit.edu.au/about/our-locations-and-facilities/locations/melbourne-brunswick-campus"),
    _c("Monash University – Law Chambers (CBD)", "Universities", "555 Lonsdale Street, Melbourne VIC 3000", "555 Lonsdale Street, Melbourne VIC 3000", "https://www.monash.edu/law/home/teaching-spaces/mulc/contact"),
    _c("Monash University – 750 Collins Street (Docklands)", "Universities", "750 Collins Street, Docklands VIC 3008", "750 Collins Street, Docklands VIC 3008", "https://www.monash.edu/about/our-locations/collins-street"),
    _c("Monash University – Parkville", "Universities", "381 Royal Parade, Parkville VIC 3052", "381 Royal Parade, Parkville VIC 3052", "https://www.monash.edu/pharm/about/parkville"),
    _c("Monash University – Caulfield", "Universities", "900 Dandenong Road, Caulfield East VIC 3145", "900 Dandenong Road, Caulfield East VIC 3145", "https://www.monash.edu/business/contact-us", verified_on="2026-11-08", note=_ADDR_ONLY + " Coordinates: Nominatim match for the numbered address."),
    _c("Monash University – Clayton", "Universities", "Wellington Road, Clayton VIC 3800", None, "https://www.monash.edu/business/contact-us", verified_on="2026-11-08", note=_ADDR_ONLY + " Coordinates unknown: the only geocode was a Wellington Road street centroid, not used."),
    _c("Victoria University – City Campus (VU City Tower)", "Universities", "370 Little Lonsdale Street, Melbourne VIC 3000", "370 Little Lonsdale Street, Melbourne VIC 3000", "https://www.vu.edu.au/about-vu/campuses/city-campus"),
    _c("Victoria University – Footscray Park", "Universities", "70–104 Ballarat Road, Footscray VIC 3011", None, "https://www.vu.edu.au/about-vu/campuses/footscray-park", verified_on="2026-11-08", note=_ADDR_ONLY + " Coordinates unknown: the only geocode was a Ballarat Road street centroid, not used."),
    _c("Deakin University – Deakin Downtown (Docklands)", "Universities", "Level 12, Tower 2, 727 Collins Street, Docklands VIC 3008", "727 Collins Street, Docklands VIC 3008", "https://www.deakin.edu.au/about-deakin/locations/corporate-centres/melbourne-cbd"),
    _c("Deakin University – Melbourne Burwood", "Universities", "221 Burwood Highway, Burwood VIC 3125", "221 Burwood Highway, Burwood VIC 3125", "https://www.deakin.edu.au/about-deakin/locations/campuses/melbourne-burwood", verified_on="2026-11-08", note=_ADDR_ONLY + " Coordinates: Nominatim match for the numbered address."),
    _c("La Trobe University – City Campus", "Universities", "360 Collins Street, Melbourne VIC 3000", "360 Collins Street, Melbourne VIC 3000", "https://www.latrobe.edu.au/city"),
    _c("La Trobe University – Melbourne (Bundoora)", "Universities", "Plenty Road, Bundoora VIC 3086", None, "https://www.latrobe.edu.au/melbourne/contact", verified_on="2026-11-08", note=_ADDR_ONLY + " Postcode 3086 (not 3083). Coordinates unknown: the only geocode was a Plenty Road street centroid, not used."),
    _c("Swinburne University – Hawthorn", "Universities", "John Street, Hawthorn VIC 3122", "John Street, Hawthorn VIC 3122", "https://www.swinburne.edu.au/life-at-swinburne/locations/hawthorn-campus/"),
    _c("Australian Catholic University – Melbourne (Fitzroy)", "Universities", "115 Victoria Parade, Fitzroy VIC 3065", "115 Victoria Parade, Fitzroy VIC 3065", "https://www.acu.edu.au/locations/melbourne/melbourne-campus-map"),
    _c("CQUniversity – Melbourne", "Universities", "120 Spencer Street, Melbourne VIC 3000", "120 Spencer Street, Melbourne VIC 3000", "https://www.cqu.edu.au/about-us/locations/melbourne"),
    _c("Torrens University – Melbourne (Flinders Street)", "Universities", "196 Flinders Street, Melbourne VIC 3000", "196 Flinders Street, Melbourne VIC 3000", "https://www.torrens.edu.au/campuses/melbourne/flinders-street-melbourne"),
    # TAFE and institutes
    _c("William Angliss Institute – Melbourne", "TAFE and institutes", "555 La Trobe Street, Melbourne VIC 3000", "555 La Trobe Street, Melbourne VIC 3000", "https://www.angliss.edu.au/contact/"),
    _c("Melbourne Polytechnic – Collingwood", "TAFE and institutes", "20 Otter Street, Collingwood VIC 3066", "20 Otter Street, Collingwood VIC 3066", "https://www.melbournepolytechnic.edu.au/locations/collingwood/"),
    _c("Holmesglen Institute – Bourke Street", "TAFE and institutes", "Level 3, 206 Bourke Street, Melbourne VIC 3000", "206 Bourke Street, Melbourne VIC 3000", "https://www.holmesglen.edu.au/about-us/campuses/bourke-street"),
    _c("RMIT University – Vocational (City)", "TAFE and institutes", "124 La Trobe Street, Melbourne VIC 3000", "124 La Trobe Street, Melbourne VIC 3000", "https://www.rmit.edu.au/about/our-locations-and-facilities/locations/melbourne-city-campus"),
    # Private colleges
    _c("Monash College – Docklands", "Private colleges", "750 Collins Street, Docklands VIC 3008", "750 Collins Street, Docklands VIC 3008", "https://www.monashcollege.edu.au/life-at-monash-college/our-campus"),
    _c("Melbourne Institute of Technology (MIT)", "Private colleges", "288 La Trobe Street, Melbourne VIC 3000", "288 La Trobe Street, Melbourne VIC 3000", "https://www.mit.edu.au/about-us/contact-us"),
    _c("Victorian Institute of Technology (VIT)", "Private colleges", "Level 6-10, 235 Queen Street, Melbourne VIC 3000", "235 Queen Street, Melbourne VIC 3000", "https://vit.edu.au/contact-us"),
    _c("Kaplan Business School – Melbourne", "Private colleges", "Level 4, 370 Docklands Drive, Docklands VIC 3008", "370 Docklands Drive, Docklands VIC 3008", "https://www.kbs.edu.au/about-us/campus-locations/melbourne-campus"),
    _c("Holmes Institute – Melbourne", "Private colleges", "185 Spring Street, Melbourne VIC 3000", "185 Spring Street, Melbourne VIC 3000", "https://holmes.edu.au/about-us/campus-locations"),
    _c("Kent Institute Australia – Melbourne", "Private colleges", "Level 10, 350 Queen Street, Melbourne VIC 3000", "350 Queen Street, Melbourne VIC 3000", "https://kent.edu.au/campus-locations/"),
    _c("Engineering Institute of Technology – Melbourne", "Private colleges", "Level 5, 301 Flinders Lane, Melbourne VIC 3000", "301 Flinders Lane, Melbourne VIC 3000", "https://www.eit.edu.au/contact/"),
    _c("Asia Pacific International College – Melbourne", "Private colleges", "Level 7, 399 Lonsdale Street, Melbourne VIC 3000", "399 Lonsdale Street, Melbourne VIC 3000", "https://coursesearch.studymelbourne.vic.gov.au/provider/asia-pacific-international-college/6b6e765a1cd3996e7500933d25e80ecf"),
    _c("Academy of Information Technology – Melbourne", "Private colleges", "Level 13, 120 Spencer Street, Melbourne VIC 3000", "120 Spencer Street, Melbourne VIC 3000", "https://ait.edu.au/contact-us/"),
    _c("Australian Institute of Music – Melbourne", "Private colleges", "120 King Street, Melbourne VIC 3000", "120 King Street, Melbourne VIC 3000", "https://aim.edu.au/life-at-aim/melbourne-campus/"),
    _c("Collarts – Wellington Street (Collingwood)", "Private colleges", "208 Wellington Street, Collingwood VIC 3066", "208 Wellington Street, Collingwood VIC 3066", "https://www.collarts.edu.au/collarts-life/campuses/"),
    _c("JMC Academy – Melbourne (South Melbourne)", "Private colleges", "208 Park Street, South Melbourne VIC 3205", "208 Park Street, South Melbourne VIC 3205", "https://www.jmcacademy.edu.au/about-us/contact-us/"),
    _c("SAE University College – Melbourne", "Private colleges", "235 Normanby Road, South Melbourne VIC 3205", "235 Normanby Road, South Melbourne VIC 3205", "https://sae.edu.au/campuses/melbourne/"),
    # English language
    _c("English language school (enter name)", "English language", "", None, "", verified=False, note="Generic entry: no location until the named school is known; proximity is not scored and travel needs confirmation"),
]

OTHER = {"name": "Other", "group": "Other", "address": "", "lat": None, "lon": None, "source_url": "", "verified": False, "note": "Always last (I9)"}


def institution_list() -> list[dict]:
    return INSTITUTIONS + [OTHER]


def find_institution(name: str):
    for i in INSTITUTIONS:
        if i["name"] == name:
            return i
    return None
