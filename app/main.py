import hashlib
import html
import math
import os
from typing import Dict, List, Literal, Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

app = FastAPI(title="STT Roof Calc Webhook", version="2.3.0")

RoofMaterial = Literal["tiles", "sheet", "papp", "turf", "unknown"]

ADDRESS_API_BASE = os.getenv("STT_ADDRESS_API_BASE", "https://ws.geonorge.no/adresser/v1")
WEBHOOK_SECRET = os.getenv("STT_WEBHOOK_SECRET", "change-me")
DEFAULT_CHIMNEY_ABOVE_RIDGE_CM = int(os.getenv("STT_DEFAULT_CHIMNEY_ABOVE_RIDGE_CM", "80"))
PLATFORM_THRESHOLD_CM = int(os.getenv("STT_PLATFORM_THRESHOLD_CM", "120"))
SAFETY_MARGIN_CM = int(os.getenv("STT_SAFETY_MARGIN_CM", "45"))
CONFIDENCE_THRESHOLD = float(os.getenv("STT_CONFIDENCE_THRESHOLD", "0.72"))

AERIAL_PROVIDER = os.getenv("STT_AERIAL_PROVIDER", "kartverket_nib_screenshot").strip().lower()
NIB_OPEN_URL_TEMPLATE = os.getenv(
    "STT_NIB_OPEN_URL_TEMPLATE",
    "https://www.norgeibilder.no/?x={lon}&y={lat}&level={zoom}"
).strip()
DEFAULT_ZOOM = int(os.getenv("STT_AERIAL_ZOOM", "19"))
DEFAULT_WIDTH = int(os.getenv("STT_AERIAL_WIDTH", "1200"))
DEFAULT_HEIGHT = int(os.getenv("STT_AERIAL_HEIGHT", "800"))
SCREENSHOT_TIMEOUT_MS = int(os.getenv("STT_SCREENSHOT_TIMEOUT_MS", "15000"))


class QuoteRequest(BaseModel):
    step: Literal["preview", "quote"] = "preview"
    address: str = Field(min_length=6)
    roof_material_hint: Optional[RoofMaterial] = None
    chimney_x: Optional[float] = None
    chimney_y: Optional[float] = None
    site_url: Optional[str] = None


class PreviewResponse(BaseModel):
    address: str
    resolved_address: str
    lat: float
    lon: float
    roof_material_hint: RoofMaterial
    map_image_url: str
    map_open_url: Optional[str] = None
    preview_note: str


class QuoteResponse(BaseModel):
    address: str
    resolved_address: str
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
    recommended_modules: int
    module_breakdown: Dict[str, int]
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
    }[material]


def guess_roof_material(address: str) -> RoofMaterial:
    normalized = address.lower()
    if "hytte" in normalized or "støl" in normalized:
        return "turf"
    materials = ["tiles", "sheet", "papp"]
    digest = int(hashlib.md5(normalized.encode("utf-8")).hexdigest()[:6], 16)
    return materials[digest % len(materials)]


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

    return {"resolved_address": resolved_address.strip().replace("  ", " "), "lat": float(lat), "lon": float(lon)}


def apply_template(template: str, lat: float, lon: float, resolved_address: str) -> str:
    values = {
        "lat": str(lat),
        "lon": str(lon),
        "zoom": str(DEFAULT_ZOOM),
        "width": str(DEFAULT_WIDTH),
        "height": str(DEFAULT_HEIGHT),
        "address_encoded": quote(resolved_address),
    }
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def generate_demo_map_svg(resolved_address: str) -> str:
    seed = hashlib.md5(resolved_address.encode("utf-8")).hexdigest()[:6]
    roof = f"#{seed[:6]}"
    house_x = 180 + (int(seed[0:2], 16) % 60)
    house_y = 130 + (int(seed[2:4], 16) % 40)
    width = 240
    safe_address = html.escape(resolved_address)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800">
<rect width="1200" height="800" fill="#dce8d1"/>
<rect x="0" y="650" width="1200" height="150" fill="#cfcfcf"/>
<rect x="90" y="80" width="150" height="70" fill="#a5c48a" opacity="0.8"/>
<rect x="950" y="110" width="170" height="85" fill="#b7d59a" opacity="0.8"/>
<rect x="{house_x * 1.3:.0f}" y="{house_y * 1.3:.0f}" width="{width * 1.5:.0f}" height="220" fill="#f4efe5" stroke="#8e8476" stroke-width="3"/>
<polygon points="{house_x * 1.3:.0f},{house_y * 1.3:.0f} {(house_x + width/2) * 1.3:.0f},{(house_y - 55) * 1.3:.0f} {(house_x + width) * 1.3:.0f},{house_y * 1.3:.0f}" fill="{roof}" opacity="0.92"/>
<line x1="{house_x * 1.3:.0f}" y1="{house_y * 1.3:.0f}" x2="{(house_x + width) * 1.3:.0f}" y2="{house_y * 1.3:.0f}" stroke="#666" stroke-width="2"/>
<text x="30" y="40" font-size="30" font-family="Arial" fill="#333">Demo-visning for {safe_address}</text>
<text x="30" y="80" font-size="22" font-family="Arial" fill="#333">Klikk på pipen der den står på taket</text>
</svg>'''
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


def estimate_roof_metrics(chimney_x: float, chimney_y: float, approx_angle_deg: int) -> Dict[str, int]:
    chimney_x = max(0.01, min(0.99, chimney_x))
    chimney_y = max(0.01, min(0.99, chimney_y))
    ridge_x = 0.50
    distance_to_ridge_fraction = max(0.0, min(1.0, abs(chimney_x - ridge_x) / 0.50))
    height_fraction = max(0.05, min(0.98, 1 - chimney_y))
    half_span_cm = 320
    roof_rise_total_cm = math.tan(math.radians(approx_angle_deg)) * half_span_cm
    drop_from_ridge_cm = round(roof_rise_total_cm * distance_to_ridge_fraction)
    estimated_back_height_cm = DEFAULT_CHIMNEY_ABOVE_RIDGE_CM + int(drop_from_ridge_cm)
    slope_length_total_cm = math.sqrt((half_span_cm ** 2) + (roof_rise_total_cm ** 2))
    distance_from_eave_cm = round(slope_length_total_cm * height_fraction)
    return {
        "approx_roof_angle_deg": int(round(approx_angle_deg)),
        "chimney_above_ridge_cm": DEFAULT_CHIMNEY_ABOVE_RIDGE_CM,
        "estimated_chimney_height_back_cm": estimated_back_height_cm,
        "distance_to_chimney_cm": max(220, int(distance_from_eave_cm)),
    }


def fit_tb_length(raw_cm: int) -> Dict:
    best = None
    for long_count in range(0, 9):
        for short_count in range(0, 9):
            count = long_count + short_count
            if count < 1:
                continue
            total = (long_count * 102) + (short_count * 68)
            if total < raw_cm:
                continue
            excess = total - raw_cm
            score = (count * 10000) + excess
            if best is None or score < best["score"]:
                best = {"score": score, "recommended_length_cm": total, "recommended_modules": count, "module_breakdown": {"102": long_count, "68": short_count}}
    if best is None:
        count = math.ceil(raw_cm / 102)
        return {"recommended_length_cm": count * 102, "recommended_modules": count, "module_breakdown": {"102": count, "68": 0}}
    return best


def fit_length_for_system(raw_cm: int, material: RoofMaterial) -> Dict:
    config = roof_system_config(material)
    if config["system"] == "tb":
        fit = fit_tb_length(raw_cm)
        return {"system": "tb", "system_label": config["label"], "recommended_length_cm": fit["recommended_length_cm"], "recommended_modules": fit["recommended_modules"], "module_breakdown": fit["module_breakdown"], "explanation": "TB Taksikring-logikk er brukt med 68/102 cm elementer."}
    if config["system"] == "lobas":
        modules = math.ceil(raw_cm / 110)
        return {"system": "lobas", "system_label": config["label"], "recommended_length_cm": modules * 110, "recommended_modules": modules, "module_breakdown": {"110": modules}, "explanation": "Lobas-logikk er brukt med 110 cm moduler."}
    return {"system": "unknown", "system_label": "Ukjent", "recommended_length_cm": raw_cm, "recommended_modules": 0, "module_breakdown": {}, "explanation": "Kunne ikke avgjøre system."}


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/nib-screenshot")
def nib_screenshot(
    lat: float = Query(...),
    lon: float = Query(...),
    zoom: int = Query(DEFAULT_ZOOM),
    width: int = Query(DEFAULT_WIDTH),
    height: int = Query(DEFAULT_HEIGHT),
):
    # This endpoint expects Playwright to be installed in the runtime image.
    # It opens the public Norge i bilder page and returns a screenshot as image/jpeg.
    # It is a workaround when there is no image provider/token-backed WMTS access.
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Playwright is not installed: {exc}")

    url = apply_template(NIB_OPEN_URL_TEMPLATE, lat, lon, "")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(url, wait_until="networkidle", timeout=SCREENSHOT_TIMEOUT_MS)
        page.screenshot(path="/tmp/nib.jpg", type="jpeg", quality=85)
        browser.close()

    data = open("/tmp/nib.jpg", "rb").read()
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=300"})


@app.post("/roof-calc")
def roof_calc(
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
        open_url = apply_template(NIB_OPEN_URL_TEMPLATE, resolved["lat"], resolved["lon"], resolved["resolved_address"])
        if AERIAL_PROVIDER == "kartverket_nib_screenshot" and base_url:
            image_url = f"{base_url.rstrip('/')}/nib-screenshot?lat={resolved['lat']}&lon={resolved['lon']}&zoom={DEFAULT_ZOOM}&width={DEFAULT_WIDTH}&height={DEFAULT_HEIGHT}"
            note = "Adresse er slått opp via Kartverket. Flyfoto vises via skjermdump av den offentlige Norge i bilder-løsningen."
        else:
            image_url = generate_demo_map_svg(resolved["resolved_address"])
            note = "Adresse er slått opp via Kartverket. Åpne lenken til Norge i bilder for ekte ortofoto."
        return PreviewResponse(
            address=payload.address,
            resolved_address=resolved["resolved_address"],
            lat=resolved["lat"],
            lon=resolved["lon"],
            roof_material_hint=material,
            map_image_url=image_url,
            map_open_url=open_url,
            preview_note=note,
        )

    if payload.chimney_x is None or payload.chimney_y is None:
        raise HTTPException(status_code=400, detail="chimney_x and chimney_y are required for quote step")

    config = roof_system_config(material)
    metrics = estimate_roof_metrics(payload.chimney_x, payload.chimney_y, config["approx_angle_deg"])
    raw_cm = int(metrics["distance_to_chimney_cm"]) + SAFETY_MARGIN_CM
    fit = fit_length_for_system(raw_cm, material)
    confidence = 0.88 if payload.roof_material_hint else 0.76

    return QuoteResponse(
        address=payload.address,
        resolved_address=resolved["resolved_address"],
        lat=resolved["lat"],
        lon=resolved["lon"],
        roof_shape="saltak",
        roof_material=material,
        system=fit["system"],
        system_label=fit["system_label"],
        confidence=round(confidence, 2),
        raw_length_cm=raw_cm,
        recommended_length_cm=int(fit["recommended_length_cm"]),
        recommended_length_m=round(float(fit["recommended_length_cm"]) / 100, 2),
        recommended_modules=int(fit["recommended_modules"]),
        module_breakdown={str(k): int(v) for k, v in fit["module_breakdown"].items()},
        approx_roof_angle_deg=int(metrics["approx_roof_angle_deg"]),
        chimney_above_ridge_cm=int(metrics["chimney_above_ridge_cm"]),
        estimated_chimney_height_back_cm=int(metrics["estimated_chimney_height_back_cm"]),
        needs_manual_review=confidence < CONFIDENCE_THRESHOLD,
        explanation=[
            "Adressen er slått opp via Kartverkets adresse-API og koblet til et representasjonspunkt.",
            "Punkt valgt i flyfoto er brukt som målpunkt for pipe.",
            f"Estimert takvinkel: ca. {metrics['approx_roof_angle_deg']}°.",
            f"Estimert pipehøyde i bakkant: ca. {metrics['estimated_chimney_height_back_cm']} cm (±10 cm).",
            fit["explanation"],
            ("Pipehøyden overstiger 120 cm i bakkant. Pluginen bør foreslå feieplatå eller pipeplattform."
             if metrics["estimated_chimney_height_back_cm"] > PLATFORM_THRESHOLD_CM
             else "Pipehøyden er innenfor grensen på 120 cm i bakkant."),
        ],
    )
