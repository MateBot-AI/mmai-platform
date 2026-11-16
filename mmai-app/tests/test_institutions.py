"""I9 / M4 campus reference data (8 November 2026): five campus addresses verified against official pages (address only),
unknown coordinates stay unknown, and an unknown campus location never scores as a zero-distance or CBD match."""
import json
from datetime import date

from server import matching, rules
from server.institutions import INSTITUTIONS, find_institution, institution_list
from tests.conftest import apartment, unit

VERIFIED_8_NOV = {
    "Monash University – Caulfield": ("900 Dandenong Road, Caulfield East VIC 3145", "https://www.monash.edu/business/contact-us", True),
    "Monash University – Clayton": ("Wellington Road, Clayton VIC 3800", "https://www.monash.edu/business/contact-us", False),
    "Victoria University – Footscray Park": ("70–104 Ballarat Road, Footscray VIC 3011", "https://www.vu.edu.au/about-vu/campuses/footscray-park", False),
    "Deakin University – Melbourne Burwood": ("221 Burwood Highway, Burwood VIC 3125", "https://www.deakin.edu.au/about-deakin/locations/campuses/melbourne-burwood", True),
    "La Trobe University – Melbourne (Bundoora)": ("Plenty Road, Bundoora VIC 3086", "https://www.latrobe.edu.au/melbourne/contact", False),
}


def test_five_campus_addresses_verified_address_only():
    for name, (address, url, has_point) in VERIFIED_8_NOV.items():
        i = find_institution(name)
        assert i and i["address"] == address and i["source_url"] == url, name
        assert i["verified"] is True and i["verified_on"] == "2026-11-08"
        assert "address only" in i["note"] and "not a measured route" in i["note"]
        if has_point:
            assert i["lat"] is not None and i["lon"] is not None and "numbered address" in i["note"]
        else:
            assert i["lat"] is None and i["lon"] is None and "street centroid" in i["note"]   # never a street centroid as a campus point
    assert "3083" not in find_institution("La Trobe University – Melbourne (Bundoora)")["address"]


def test_no_generic_cbd_centroid_and_no_zero_coordinates():
    eng = find_institution("English language school (enter name)")
    assert eng["lat"] is None and eng["lon"] is None and eng["verified"] is False
    for i in institution_list():
        assert not (i["lat"] == 0 or i["lon"] == 0), i["name"]
        assert (i["lat"] is None) == (i["lon"] is None), i["name"]
    assert institution_list()[-1]["name"] == "Other"
    assert all("verified_on" in i for i in INSTITUTIONS)


def test_distance_to_the_place_of_study_is_not_part_of_the_score():
    """M4 / GP6(d): proximity is not an initial ranking input. The score has an areas part (4.6) and no distance part; an
    unknown campus point is still never reported as a distance."""
    apt = apartment("2B2B")
    assert matching._campus({"school": "Monash University – Clayton"}) is None       # unknown point → no campus
    assert matching._campus({"school": "English language school (enter name)"}) is None
    assert matching._campus({"school": "RMIT University – Melbourne City"}) is not None
    u1, u2 = unit("a"), unit("b")
    u1.extra = {"id": "a", "school": "Monash University – Clayton", "travel": "5 minutes", "areas": "[]", "move_out": "2028-02-01"}
    u2.extra = {"id": "b", "school": "English language school (enter name)", "travel": "5 minutes", "areas": "[]", "move_out": "2028-02-01"}
    chk = rules.check_group(apt, [u1, u2])
    assert chk.ok, chk.reasons
    score, parts = matching.preference_score(apt, [u1, u2], chk)
    assert "proximity" not in parts and parts["areas"] == 0.6   # nobody named an area: the neutral default, whatever the campus or 4.7 answer
    u1.extra["school"] = u2.extra["school"] = "RMIT University – Melbourne City"   # a located campus and a 5-minute answer change nothing
    assert matching.preference_score(apt, [u1, u2], chk) == (score, parts)
    u1.extra["travel"] = u2.extra["travel"] = None
    assert matching.preference_score(apt, [u1, u2], chk) == (score, parts)
    u1.extra["areas"] = json.dumps([apt.suburb])   # stored as JSON text, as the enquiries table holds it
    assert matching.preference_score(apt, [u1, u2], chk)[1]["areas"] == 1.0   # the named area still counts
    u1.extra["areas"] = json.dumps(["Somewhere else"])
    assert matching.preference_score(apt, [u1, u2], chk)[1]["areas"] == 0.3
    u1.extra["areas"] = "[]"
    assert matching._campus_km(apt, [u1, u2]) is None     # no distance is reported for staff either
