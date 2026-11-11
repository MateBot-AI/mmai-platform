"""One-off geocoding helper (Nominatim / OpenStreetMap). Output is checked in as server/data/geocode.json.
Source: https://nominatim.openstreetmap.org (ODbL). Re-run manually if addresses change."""
import json, time, urllib.request, urllib.parse, sys, os
HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, 'data', 'geocode.json')

def geocode(q):
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'au'})
    req = urllib.request.Request(url, headers={'User-Agent': 'MMAI-Stage1-build/1.0 (ALM internal)'})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as e:
        return None
    if not data: return None
    d = data[0]
    return {'lat': float(d['lat']), 'lon': float(d['lon']), 'display': d.get('display_name', ''), 'source': 'https://nominatim.openstreetmap.org/'}

if __name__ == '__main__':
    queries = json.load(open(sys.argv[1]))
    existing = json.load(open(OUT)) if os.path.exists(OUT) else {}
    for q in queries:
        if q in existing and existing[q]: continue
        existing[q] = geocode(q)
        print(q, '->', existing[q] and (existing[q]['lat'], existing[q]['lon']), flush=True)
        json.dump(existing, open(OUT, 'w'), indent=1)
        time.sleep(1.1)
