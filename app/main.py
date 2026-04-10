import math
import os
from io import BytesIO
from typing import Dict, List, Literal, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Response
from PIL import Image
from pydantic import BaseModel, Field

app = FastAPI(title="STT Backend", version="1.1.0")

RoofMaterial = Literal["tiles", "sheet", "papp", "turf", "unknown", "extra_1", "extra_2", "extra_3", "extra_4"]

ADDRESS_API_BASE = os.getenv("STT_ADDRESS_API_BASE", "https://ws.geonorge.no/adresser/v1")
WEBHOOK_SECRET = os.getenv("STT_WEBHOOK_SECRET", "change-me")

DEFAULT_CHIMNEY_ABOVE_RIDGE_CM = int(os.getenv("STT_DEFAULT_CHIMNEY_ABOVE_RIDGE_CM", "80"))
PLATFORM_THRESHOLD_CM = int(os.getenv("STT_PLATFORM_THRESHOLD_CM", "120"))
SAFETY_MARGIN_CM = int(os.getenv("STT_SAFETY_MARGIN_CM", "45"))
CONFIDENCE_THRESHOLD = float(os.getenv("STT_CONFIDENCE_THRESHOLD", "0.72"))

# Kartverket tile provider
TILE_TEMPLATE = os.getenv(
    "STT_TILE_TEMPLATE",
    "https://cache.kartverket.no/v1/wmts/1.0.0/ortofoto/default/webmercator/{z}/{y}/{x}.jpeg"
).strip()
DEFAULT_ZOOM = int(os.getenv("STT_AERIAL_ZOOM", "19"))
DEFAULT_WIDTH = int(os.getenv("STT_AERIAL_WIDTH", "1024"))
DEFAULT_HEIGHT = int(os.getenv("STT_AERIAL_HEIGHT", "768"))
TILE_SIZE = 256

class QuoteRequest(BaseModel):
    step: Literal["preview", "quote"] = "preview"
    address: str = Field(min_length=6)
    roof_material_hint: Optional[RoofMaterial] = None
    start_x: Optional[float] = None
    start_y: Optional[float] = None
    chimney_x: Optional[float] = None
    chimney_y: Optional[float] = None
    site_url: Optional[str] = None

class PreviewResponse(BaseModel):
    address: str
    resolved_address: str
    postnummer: Optional[str] = None
    poststed: Optional[str] = None
    lat: float
    lon: float
    roof_material_hint: RoofMaterial
    map_image_url: str
    map_open_url: Optional[str] = None
    preview_note: str

class QuoteResponse(BaseModel):
    address: str
    resolved_address: str
    postnummer: Optional[str] = None
    poststed: Optional[str] = None
    lat: float
    lon: float
    roof_shape: str
    roof_material: RoofMaterial
    system: str
    system_label: str
    confidence: float
    raw_length_cm: int
    recommended_length_cm: int
    recommended_length_m: float
    approx_roof_angle_deg: int
    chimney_above_ridge_cm: int
    estimated_chimney_height_back_cm: int
    needs_manual_review: bool
    explanation: List[str]

def require_secret(header_secret: Optional[str]) -> None:
    if header_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

def roof_system_config(material: RoofMaterial) -> Dict:
    return {
        "tiles": {"system": "tb", "label": "TB Taksikring", "approx_angle_deg": 34},
        "sheet": {"system": "tb", "label": "TB Taksikring", "approx_angle_deg": 24},
        "papp": {"system": "lobas", "label": "Lobas", "approx_angle_deg": 18},
        "turf": {"system": "lobas", "label": "Lobas", "approx_angle_deg": 22},
        "unknown": {"system": "unknown", "label": "Ukjent", "approx_angle_deg": 28},
        "extra_1": {"system": "tb", "label": "Ekstra 1", "approx_angle_deg": 34},
        "extra_2": {"system": "tb", "label": "Ekstra 2", "approx_angle_deg": 24},
        "extra_3": {"system": "lobas", "label": "Ekstra 3", "approx_angle_deg": 18},
        "extra_4": {"system": "unknown", "label": "Ekstra 4", "approx_angle_deg": 28},
    }[material]

def guess_roof_material(address: str) -> RoofMaterial:
    normalized = address.lower()
    if "hytte" in normalized or "støl" in normalized:
        return "turf"
    materials = ["tiles", "sheet", "papp"]
    idx = abs(hash(normalized)) % len(materials)
    return materials[idx]

def address_similarity_score(query: str, item: dict) -> int:
    q = query.lower()
    score = 0
    adressetekst = str(item.get("adressetekst", "")).lower()
    postnummer = str(item.get("postnummer", "")).lower()
    kommunenavn = str(item.get("kommunenavn", "")).lower()
    if adressetekst and adressetekst in q:
        score += 30
    if postnummer and postnummer in q:
        score += 20
    if kommunenavn and kommunenavn in q:
        score += 10
    for part in [p for p in q.replace(",", " ").split() if p]:
        if part in adressetekst:
            score += 3
        if part in kommunenavn:
            score += 2
        if part == postnummer:
            score += 4
    return score

def resolve_address(query: str) -> Dict:
    with httpx.Client(timeout=12.0, follow_redirects=True) as client:
        resp = client.get(
            f"{ADDRESS_API_BASE}/sok",
            params={
                "sok": query,
                "fuzzy": "true",
                "asciiKompatibel": "false",
                "utkoordsys": "4258",
                "treffPerSide": "5",
                "side": "0",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    items = data.get("adresser") or []
    if not items:
        raise HTTPException(status_code=404, detail="Fant ingen adresse i Kartverkets adresse-API.")

    best = sorted(items, key=lambda item: address_similarity_score(query, item), reverse=True)[0]
    point = best.get("representasjonspunkt") or {}
    lat = point.get("lat")
    lon = point.get("lon")
    if lat is None or lon is None:
        raise HTTPException(status_code=502, detail="Adresseoppslaget manglet representasjonspunkt.")

    resolved_address = best.get("adressetekst") or query
    if best.get("postnummer") or best.get("poststed"):
        resolved_address = f"{resolved_address}, {best.get('postnummer', '')} {best.get('poststed', '')}".strip()

    return {
        "resolved_address": resolved_address.strip().replace("  ", " "),
        "postnummer": best.get("postnummer"),
        "poststed": best.get("poststed"),
        "lat": float(lat),
        "lon": float(lon),
    }

def latlon_to_global_pixel(lat: float, lon: float, zoom: int):
    siny = math.sin(math.radians(lat))
    siny = min(max(siny, -0.9999), 0.9999)
    scale = TILE_SIZE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * scale
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * scale
    return x, y

def tile_url(z: int, x: int, y: int) -> str:
    return TILE_TEMPLATE.format(z=z, x=x, y=y)

def fetch_tile(client: httpx.Client, z: int, x: int, y: int) -> Image.Image:
    url = tile_url(z, x, y)
    r = client.get(url, timeout=20.0)
    r.raise_for_status()
    return Image.open(BytesIO(r.content)).convert("RGB")

def build_ortho_image(lat: float, lon: float, zoom: int, width: int, height: int) -> bytes:
    center_x, center_y = latlon_to_global_pixel(lat, lon, zoom)
    left = center_x - width / 2
    top = center_y - height / 2

    start_tx = int(math.floor(left / TILE_SIZE))
    start_ty = int(math.floor(top / TILE_SIZE))
    end_tx = int(math.floor((left + width - 1) / TILE_SIZE))
    end_ty = int(math.floor((top + height - 1) / TILE_SIZE))

    canvas = Image.new("RGB", ((end_tx - start_tx + 1) * TILE_SIZE, (end_ty - start_ty + 1) * TILE_SIZE))

    with httpx.Client(follow_redirects=True) as client:
        for ty in range(start_ty, end_ty + 1):
            for tx in range(start_tx, end_tx + 1):
                tile = fetch_tile(client, zoom, tx, ty)
                canvas.paste(tile, ((tx - start_tx) * TILE_SIZE, (ty - start_ty) * TILE_SIZE))

    crop_x = int(left - start_tx * TILE_SIZE)
    crop_y = int(top - start_ty * TILE_SIZE)
    cropped = canvas.crop((crop_x, crop_y, crop_x + width, crop_y + height))

    buf = BytesIO()
    cropped.save(buf, format="JPEG", quality=90)
    return buf.getvalue()

def estimate_roof_metrics(start_y: float, chimney_x: float, approx_angle_deg: int) -> Dict[str, int]:
    chimney_x = max(0.01, min(0.99, chimney_x))
    start_y = max(0.01, min(0.99, start_y))
    ridge_x = 0.50
    distance_to_ridge_fraction = max(0.0, min(1.0, abs(chimney_x - ridge_x) / 0.50))
    half_span_cm = 320
    roof_rise_total_cm = math.tan(math.radians(approx_angle_deg)) * half_span_cm
    drop_from_ridge_cm = round(roof_rise_total_cm * distance_to_ridge_fraction)
    estimated_back_height_cm = DEFAULT_CHIMNEY_ABOVE_RIDGE_CM + int(drop_from_ridge_cm)
    slope_length_total_cm = math.sqrt((half_span_cm ** 2) + (roof_rise_total_cm ** 2))
    distance_from_eave_cm = round(slope_length_total_cm * (1.0 - start_y))
    return {
        "approx_roof_angle_deg": int(round(approx_angle_deg)),
        "chimney_above_ridge_cm": DEFAULT_CHIMNEY_ABOVE_RIDGE_CM,
        "estimated_chimney_height_back_cm": estimated_back_height_cm,
        "distance_to_ridge_cm": max(220, int(distance_from_eave_cm)),
    }

def pick_complete_length(raw_cm: int, system: str) -> int:
    if system == "tb":
        options = [204, 306, 408, 510, 612, 714]
    elif system == "lobas":
        options = [220, 330, 440, 550, 660, 770]
    else:
        options = [250, 350, 450, 550, 650]
    for opt in options:
        if opt >= raw_cm:
            return opt
    return options[-1]

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

@app.get("/ortho-image")
def ortho_image(
    lat: float = Query(...),
    lon: float = Query(...),
    zoom: int = Query(DEFAULT_ZOOM),
    width: int = Query(DEFAULT_WIDTH),
    height: int = Query(DEFAULT_HEIGHT),
):
    try:
        data = build_ortho_image(lat, lon, zoom, width, height)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Kunne ikke hente ortofoto: {exc}")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

@app.post("/calculate")
def calculate(
    payload: QuoteRequest,
    x_stt_webhook_secret: Optional[str] = Header(default=None),
    x_forwarded_proto: Optional[str] = Header(default=None),
    x_forwarded_host: Optional[str] = Header(default=None),
):
    require_secret(x_stt_webhook_secret)
    resolved = resolve_address(payload.address)
    material: RoofMaterial = payload.roof_material_hint or guess_roof_material(resolved["resolved_address"])

    base_url = None
    if x_forwarded_host:
        base_url = f"{x_forwarded_proto or 'https'}://{x_forwarded_host}"

    if payload.step == "preview":
        image_url = (
            f"{base_url.rstrip('/')}/ortho-image?lat={resolved['lat']}&lon={resolved['lon']}"
            f"&zoom={DEFAULT_ZOOM}&width={DEFAULT_WIDTH}&height={DEFAULT_HEIGHT}"
            if base_url else ""
        )
        return PreviewResponse(
            address=payload.address,
            resolved_address=resolved["resolved_address"],
            postnummer=resolved["postnummer"],
            poststed=resolved["poststed"],
            lat=resolved["lat"],
            lon=resolved["lon"],
            roof_material_hint=material,
            map_image_url=image_url,
            map_open_url=None,
            preview_note="Ekte ortofoto hentet via Kartverket-fliser.",
        )

    if payload.start_x is None or payload.start_y is None or payload.chimney_x is None or payload.chimney_y is None:
        raise HTTPException(status_code=400, detail="start_x, start_y, chimney_x and chimney_y are required for quote step")

    config = roof_system_config(material)
    metrics = estimate_roof_metrics(payload.start_y, payload.chimney_x, config["approx_angle_deg"])
    raw_cm = int(metrics["distance_to_ridge_cm"]) + SAFETY_MARGIN_CM
    recommended_cm = pick_complete_length(raw_cm, config["system"])
    confidence = 0.88 if payload.roof_material_hint else 0.76

    return QuoteResponse(
        address=payload.address,
        resolved_address=resolved["resolved_address"],
        postnummer=resolved["postnummer"],
        poststed=resolved["poststed"],
        lat=resolved["lat"],
        lon=resolved["lon"],
        roof_shape="saltak",
        roof_material=material,
        system=config["system"],
        system_label=config["label"],
        confidence=round(confidence, 2),
        raw_length_cm=raw_cm,
        recommended_length_cm=recommended_cm,
        recommended_length_m=round(recommended_cm / 100.0, 2),
        approx_roof_angle_deg=int(metrics["approx_roof_angle_deg"]),
        chimney_above_ridge_cm=int(metrics["chimney_above_ridge_cm"]),
        estimated_chimney_height_back_cm=int(metrics["estimated_chimney_height_back_cm"]),
        needs_manual_review=confidence < CONFIDENCE_THRESHOLD,
        explanation=[
            "Adressen er slått opp via Kartverkets adresse-API.",
            "Ortofoto hentes fra Kartverket-fliser.",
            "Takstigen beregnes fra valgt startpunkt ved takfot og opp til møne.",
            f"Estimert takvinkel: ca. {metrics['approx_roof_angle_deg']}°.",
            f"Estimert pipehøyde i bakkant: ca. {metrics['estimated_chimney_height_back_cm']} cm.",
        ],
    )
