import hashlib, html, math, os
from typing import Dict, List, Literal, Optional
from urllib.parse import quote
import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title='STT Backend v1', version='1.0.0')
RoofMaterial = Literal['tiles','sheet','papp','turf','unknown','extra_1','extra_2','extra_3','extra_4']
ADDRESS_API_BASE = os.getenv('STT_ADDRESS_API_BASE', 'https://ws.geonorge.no/adresser/v1')
WEBHOOK_SECRET = os.getenv('STT_WEBHOOK_SECRET', 'change-me')
MAP_IMAGE_URL_TEMPLATE = os.getenv('STT_MAP_IMAGE_URL_TEMPLATE', '').strip()
MAP_OPEN_URL_TEMPLATE = os.getenv('STT_MAP_OPEN_URL_TEMPLATE', '').strip()
DEFAULT_PLATFORM_THRESHOLD_CM = int(os.getenv('STT_PLATFORM_THRESHOLD_CM', '120'))
BASE_MAPPINGS = {
    'extra_1': os.getenv('STT_MAP_EXTRA_1', 'tiles'),
    'extra_2': os.getenv('STT_MAP_EXTRA_2', 'sheet'),
    'extra_3': os.getenv('STT_MAP_EXTRA_3', 'papp'),
    'extra_4': os.getenv('STT_MAP_EXTRA_4', 'unknown'),
}

class Point(BaseModel):
    x: float
    y: float
class LadderInput(BaseModel):
    id: int
    start: Point
    pipe: Point
class QuoteRequest(BaseModel):
    step: Literal['preview','quote'] = 'preview'
    address: str = Field(min_length=6)
    roof_material_hint: Optional[RoofMaterial] = None
    ladders: Optional[List[LadderInput]] = None
    site_url: Optional[str] = None

def require_secret(header_secret: Optional[str]) -> None:
    if header_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail='Unauthorized')

def resolve_base(variant: str) -> str:
    if variant in ['tiles','sheet','papp','turf','unknown']:
        return variant
    return BASE_MAPPINGS.get(variant, 'unknown')

def guess_roof_material(address: str) -> str:
    normalized = address.lower()
    if 'hytte' in normalized or 'støl' in normalized:
        return 'turf'
    vals = ['tiles','sheet','papp']
    digest = int(hashlib.md5(normalized.encode('utf-8')).hexdigest()[:6], 16)
    return vals[digest % len(vals)]

def address_similarity_score(query: str, item: dict) -> int:
    q = query.lower(); score = 0
    adressetekst = str(item.get('adressetekst', '')).lower()
    postnummer = str(item.get('postnummer', '')).lower()
    kommunenavn = str(item.get('kommunenavn', '')).lower()
    if adressetekst and adressetekst in q: score += 30
    if postnummer and postnummer in q: score += 20
    if kommunenavn and kommunenavn in q: score += 10
    for part in [p for p in q.replace(',', ' ').split() if p]:
        if part in adressetekst: score += 3
        if part in kommunenavn: score += 2
        if part == postnummer: score += 4
    return score

def resolve_address(query: str) -> Dict:
    with httpx.Client(timeout=12.0, follow_redirects=True) as client:
        resp = client.get(f'{ADDRESS_API_BASE}/sok', params={'sok': query, 'fuzzy': 'true', 'asciiKompatibel': 'false', 'utkoordsys': '4258', 'treffPerSide': '5', 'side': '0'})
        resp.raise_for_status(); data = resp.json()
    items = data.get('adresser') or []
    if not items:
        raise HTTPException(status_code=404, detail='Fant ingen adresse i Kartverkets adresse-API.')
    best = sorted(items, key=lambda item: address_similarity_score(query, item), reverse=True)[0]
    point = best.get('representasjonspunkt') or {}
    lat = point.get('lat'); lon = point.get('lon')
    if lat is None or lon is None:
        raise HTTPException(status_code=502, detail='Adresseoppslaget manglet representasjonspunkt.')
    resolved = best.get('adressetekst') or query
    postnummer = str(best.get('postnummer') or '')
    poststed = str(best.get('poststed') or '')
    if postnummer or poststed:
        resolved = f'{resolved}, {postnummer} {poststed}'.strip()
    return {'resolved_address': resolved, 'lat': float(lat), 'lon': float(lon), 'postnummer': postnummer, 'poststed': poststed}

def build_demo_svg(address: str) -> str:
    safe = html.escape(address)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800"><defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="1"><stop offset="0" stop-color="#bfdbfe"/><stop offset="1" stop-color="#bbf7d0"/></linearGradient></defs><rect width="1200" height="800" fill="url(#g)"/><rect x="260" y="150" width="520" height="340" rx="22" fill="#6b7280"/><rect x="490" y="140" width="16" height="360" fill="#111827"/><rect x="610" y="210" width="58" height="58" rx="8" fill="#111827"/><rect x="390" y="335" width="58" height="58" rx="8" fill="#111827"/><text x="36" y="54" font-size="28" font-family="Arial" fill="#111827">Demo-bilde for {safe}</text></svg>'''
    return 'data:image/svg+xml;charset=utf-8,' + quote(svg)

def map_url_from_template(tpl: str, resolved: Dict) -> str:
    return tpl.replace('{lat}', str(resolved['lat'])).replace('{lon}', str(resolved['lon'])).replace('{postnummer}', resolved['postnummer']).replace('{address_encoded}', quote(resolved['resolved_address']))

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.post('/roof-calc')
def roof_calc(payload: QuoteRequest, x_stt_webhook_secret: Optional[str] = Header(default=None)):
    require_secret(x_stt_webhook_secret)
    resolved = resolve_address(payload.address)
    variant = payload.roof_material_hint or guess_roof_material(resolved['resolved_address'])
    base = resolve_base(variant)
    if payload.step == 'preview':
        map_image_url = map_url_from_template(MAP_IMAGE_URL_TEMPLATE, resolved) if MAP_IMAGE_URL_TEMPLATE else build_demo_svg(resolved['resolved_address'])
        map_open_url = map_url_from_template(MAP_OPEN_URL_TEMPLATE, resolved) if MAP_OPEN_URL_TEMPLATE else None
        return {'address': payload.address, 'resolved_address': resolved['resolved_address'], 'lat': resolved['lat'], 'lon': resolved['lon'], 'postnummer': resolved['postnummer'], 'poststed': resolved['poststed'], 'roof_material_hint': variant, 'map_image_url': map_image_url, 'map_open_url': map_open_url, 'preview_note': 'Velg startpunkt ved takfot og deretter bakerste kant av pipa. Takstigen går alltid til møne.'}
    ladders = payload.ladders or []
    if not ladders:
        raise HTTPException(status_code=400, detail='At least one ladder is required')
    pitch_deg = {'tiles':34,'sheet':24,'papp':18,'turf':22,'unknown':28}.get(base, 28)
    results = []
    for ladder in ladders:
        start_x = max(0.02, min(0.98, ladder.start.x)); start_y = max(0.02, min(0.98, ladder.start.y))
        pipe_x = max(0.02, min(0.98, ladder.pipe.x)); pipe_y = max(0.02, min(0.98, ladder.pipe.y))
        roof_run_cm = 300 + int((0.92 - start_y) * 140)
        slope_len_cm = math.sqrt(roof_run_cm**2 + (math.tan(math.radians(pitch_deg))*roof_run_cm)**2)
        recommended_cm = int(math.ceil(slope_len_cm / 6) * 6)
        dist_fraction = max(0.0, min(1.0, abs(pipe_x - 0.50) / 0.50))
        roof_rise_cm = math.tan(math.radians(pitch_deg)) * roof_run_cm
        back_height_cm = 80 + int(round(roof_rise_cm * dist_fraction))
        results.append({'id': ladder.id, 'roof_material': variant, 'roof_material_base': base, 'roof_shape': 'saltak', 'pitch_deg': pitch_deg, 'recommended_length_cm': recommended_cm, 'recommended_length_m': round(recommended_cm / 100, 2), 'estimated_chimney_height_back_cm': back_height_cm, 'platform_needed': back_height_cm > DEFAULT_PLATFORM_THRESHOLD_CM, 'start': {'x': start_x, 'y': start_y}, 'pipe': {'x': pipe_x, 'y': pipe_y}, 'confidence': 0.78 if payload.roof_material_hint else 0.72, 'explanation': ['Takstigen beregnes fra valgt startpunkt ved takfot og helt opp til møne.', 'Pipepunktet brukes til å estimere pipehøyde i bakkant og behov for feieplatå.']})
    return {'address': payload.address, 'resolved_address': resolved['resolved_address'], 'postnummer': resolved['postnummer'], 'poststed': resolved['poststed'], 'roof_material': variant, 'ladders': results}
