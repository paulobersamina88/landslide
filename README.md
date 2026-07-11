from __future__ import annotations

import hashlib
import io
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
import pdfplumber
from PIL import Image, ImageDraw, ImageFont

# Column ranges were derived from the recurring MGB landscape table layout.
# They are normalized so the parser still works when the PDF page size changes.
COLUMN_RANGES = [
    ("region", 0.000, 0.151),
    ("province", 0.151, 0.252),
    ("municipality", 0.252, 0.361),
    ("barangay", 0.361, 0.504),
    ("vhl", 0.504, 0.577),
    ("hl", 0.577, 0.637),
    ("ml", 0.637, 0.698),
    ("df", 0.698, 0.747),
    ("vhf", 0.747, 0.815),
    ("hf", 0.815, 0.878),
    ("mf", 0.878, 1.001),
]

RISK_RANK = {"None": 0, "Moderate": 1, "High": 2, "Very High": 3}
RISK_COLORS = {
    "Very High": "#d7191c",
    "High": "#f57c00",
    "Moderate": "#fbc02d",
    "None": "#8c8c8c",
}


@dataclass(frozen=True)
class PdfMetadata:
    advisory_date: str = ""
    advisory_time: str = ""
    source_line: str = "MGB barangay list"


def _clean_text(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value


def _group_words_into_rows(words: list[dict], tolerance: float = 1.7) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_top: float | None = None

    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(word["top"])
        if current_top is None or abs(top - current_top) <= tolerance:
            current.append(word)
            if current_top is None:
                current_top = top
            else:
                current_top = (current_top * (len(current) - 1) + top) / len(current)
        else:
            groups.append(current)
            current = [word]
            current_top = top

    if current:
        groups.append(current)
    return groups


def _find_header_bottom(words: list[dict], page_height: float) -> float:
    header_tokens = {"REGION", "PROVINCE", "MUNICIPALITY", "BARANGAY"}
    header_words = [
        w
        for w in words
        if float(w.get("top", page_height)) < page_height * 0.20
        and str(w.get("text", "")).strip() in header_tokens
    ]
    if header_words:
        bottoms = [float(w["bottom"]) for w in header_words]
        return max(bottoms) + 1.0
    return page_height * 0.078


def extract_metadata(first_page_text: str) -> PdfMetadata:
    text = first_page_text or ""
    date_match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
        text,
        flags=re.IGNORECASE,
    )
    date_value = date_match.group(0) if date_match else ""

    time_match = re.search(r"(?m)^\s*([01]?\d|2[0-3])([0-5]\d)\s*$", text)
    time_value = ""
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        suffix = "AM" if hour < 12 else "PM"
        display_hour = hour % 12 or 12
        time_value = f"{display_hour}:{minute:02d} {suffix}"

    source_line = "MGB barangay list"
    first_line = next((line.strip() for line in text.splitlines() if "GSM" in line and "WRF" in line), "")
    if first_line:
        source_line = first_line
    return PdfMetadata(date_value, time_value, source_line)


def parse_mgb_pdf(pdf_bytes: bytes, progress_callback: Callable[[int, int], None] | None = None) -> tuple[pd.DataFrame, PdfMetadata]:
    """Parse the recurring MGB barangay table layout from an uploaded PDF.

    Returns one row per listed barangay. A row may contain both landslide and
    flooding codes; the app computes the highest landslide risk separately.
    """
    records: list[dict] = []
    first_page_text = ""

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        total_pages = len(pdf.pages)
        for page_number, page in enumerate(pdf.pages, start=1):
            if page_number == 1:
                first_page_text = page.extract_text() or ""

            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            header_bottom = _find_header_bottom(words, float(page.height))
            groups = _group_words_into_rows(words)

            for group in groups:
                row_top = min(float(w["top"]) for w in group)
                row_text = " ".join(str(w["text"]) for w in group).strip()

                if row_top <= header_bottom:
                    continue
                if row_top >= float(page.height) * 0.965:
                    continue
                if row_text.startswith("Barangay Count:") or row_text.startswith("Note:"):
                    continue
                if row_text.isdigit() and len(group) == 1:
                    continue

                record: dict[str, str | int | float] = {"page": page_number, "row_y": row_top}
                for name, left_ratio, right_ratio in COLUMN_RANGES:
                    left = left_ratio * float(page.width)
                    right = right_ratio * float(page.width)
                    selected = [
                        w
                        for w in group
                        if left <= (float(w["x0"]) + float(w["x1"])) / 2.0 < right
                    ]
                    record[name] = _clean_text(
                        " ".join(str(w["text"]) for w in sorted(selected, key=lambda item: float(item["x0"])))
                    )

                if any(record.get(field) for field in ("region", "province", "municipality", "barangay")):
                    records.append(record)

            if progress_callback:
                progress_callback(page_number, total_pages)

    df = pd.DataFrame.from_records(records)
    if df.empty:
        raise ValueError("No MGB table rows were detected. Confirm that this is the standard landscape MGB barangay-list PDF.")

    for column in [name for name, _, _ in COLUMN_RANGES]:
        if column not in df.columns:
            df[column] = ""
        df[column] = df[column].fillna("").map(_clean_text)

    # Remove obvious non-data rows and normalize codes. Some MGB PDFs have
    # text-layer collisions where an exceptionally long municipality name
    # overlaps the barangay column. Preserve those hazard rows with a visible
    # placeholder instead of silently losing them from the totals.
    df = df[df["municipality"] != ""].copy()
    for code_column in ["vhl", "hl", "ml", "df", "vhf", "hf", "mf"]:
        df[code_column] = df[code_column].str.upper().str.strip()

    df["landslide_risk"] = df.apply(highest_landslide_risk, axis=1)
    df["has_landslide"] = df["landslide_risk"].ne("None")
    df["has_flooding"] = df[["vhf", "hf", "mf"]].isin(["VHF", "HF", "MF"]).any(axis=1)
    df["parse_warning"] = False
    missing_barangay = df["barangay"].eq("") & (df["has_landslide"] | df["has_flooding"])
    df.loc[missing_barangay, "parse_warning"] = True
    df.loc[missing_barangay, "barangay"] = df.loc[missing_barangay].apply(
        lambda row: f"[Unparsed row - PDF page {int(row['page'])}, y={float(row['row_y']):.1f}]",
        axis=1,
    )
    df = df[df["barangay"] != ""].copy()
    df["location_key"] = (
        df["barangay"].str.lower()
        + "|"
        + df["municipality"].str.lower()
        + "|"
        + df["province"].str.lower()
    )
    df = df.drop_duplicates(subset=["location_key"], keep="first").reset_index(drop=True)

    return df, extract_metadata(first_page_text)


def highest_landslide_risk(row: pd.Series) -> str:
    if str(row.get("vhl", "")).strip().upper() == "VHL":
        return "Very High"
    if str(row.get("hl", "")).strip().upper() == "HL":
        return "High"
    if str(row.get("ml", "")).strip().upper() == "ML":
        return "Moderate"
    return "None"


def clean_place_name(value: str) -> str:
    value = _clean_text(value)
    # Parenthetical alternate names can help, but long government labels often hurt geocoding.
    value = re.sub(r"\s*\((Capital|Pob\.?|Capital City)\)\s*", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def build_geocode_query(row: pd.Series, level: str) -> str:
    province = clean_place_name(str(row.get("province", "")))
    municipality = clean_place_name(str(row.get("municipality", "")))
    barangay = clean_place_name(str(row.get("barangay", "")))

    if level == "Barangay":
        return ", ".join(part for part in [barangay, municipality, province, "Philippines"] if part)
    return ", ".join(part for part in [municipality, province, "Philippines"] if part)


def aggregate_for_mapping(df: pd.DataFrame, level: str) -> pd.DataFrame:
    landslide_df = df[df["landslide_risk"].ne("None")].copy()
    if landslide_df.empty:
        return pd.DataFrame()

    if level == "Barangay":
        grouped = landslide_df.copy()
        grouped["affected_barangays"] = 1
        grouped["very_high"] = grouped["landslide_risk"].eq("Very High").astype(int)
        grouped["high"] = grouped["landslide_risk"].eq("High").astype(int)
        grouped["moderate"] = grouped["landslide_risk"].eq("Moderate").astype(int)
        grouped["map_label"] = grouped["barangay"] + ", " + grouped["municipality"]
        grouped["geocode_query"] = grouped.apply(lambda row: build_geocode_query(row, level), axis=1)
        return grouped.reset_index(drop=True)

    group_columns = ["region", "province", "municipality"]
    grouped = (
        landslide_df.groupby(group_columns, dropna=False)
        .agg(
            affected_barangays=("barangay", "count"),
            very_high=("landslide_risk", lambda values: int((values == "Very High").sum())),
            high=("landslide_risk", lambda values: int((values == "High").sum())),
            moderate=("landslide_risk", lambda values: int((values == "Moderate").sum())),
        )
        .reset_index()
    )
    grouped["landslide_risk"] = grouped.apply(
        lambda row: "Very High"
        if row["very_high"] > 0
        else ("High" if row["high"] > 0 else "Moderate"),
        axis=1,
    )
    grouped["map_label"] = grouped["municipality"] + ", " + grouped["province"]
    grouped["geocode_query"] = grouped.apply(lambda row: build_geocode_query(row, level), axis=1)
    return grouped


class GeocodeCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, query: str) -> dict | None:
        return self.data.get(query)

    def set(self, query: str, value: dict) -> None:
        self.data[query] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def to_frame(self) -> pd.DataFrame:
        rows = [{"query": query, **value} for query, value in self.data.items()]
        return pd.DataFrame(rows)


def geocode_locations(
    mapping_df: pd.DataFrame,
    email: str,
    cache_path: str | Path,
    progress_callback: Callable[[int, int, str], None] | None = None,
    min_delay_seconds: float = 1.1,
) -> pd.DataFrame:
    """Geocode unique place-name queries using Nominatim with a persistent cache.

    The default public Nominatim endpoint is intentionally rate-limited. For
    large or repeated institutional workflows, use a hosted geocoder or upload
    a prepared coordinate cache instead of repeatedly querying the public API.
    """
    from geopy.extra.rate_limiter import RateLimiter
    from geopy.geocoders import Nominatim

    if not email or "@" not in email:
        raise ValueError("Enter a contact email for the Nominatim user-agent.")

    cache = GeocodeCache(cache_path)
    user_agent = f"mgb-hazard-visualizer/1.0 ({email.strip()})"
    geolocator = Nominatim(user_agent=user_agent, timeout=15)
    geocode = RateLimiter(
        geolocator.geocode,
        min_delay_seconds=min_delay_seconds,
        swallow_exceptions=True,
        max_retries=2,
        error_wait_seconds=3.0,
    )

    unique_queries = [query for query in mapping_df["geocode_query"].dropna().unique() if query]
    total = len(unique_queries)

    for index, query in enumerate(unique_queries, start=1):
        if cache.get(query) is None:
            location = geocode(query, country_codes="ph", addressdetails=True, exactly_one=True)
            if location:
                value = {
                    "lat": float(location.latitude),
                    "lon": float(location.longitude),
                    "display_name": str(location.address),
                    "status": "matched",
                }
            else:
                value = {"lat": None, "lon": None, "display_name": "", "status": "not_found"}
            cache.set(query, value)
        if progress_callback:
            progress_callback(index, total, query)

    geocoded = mapping_df.copy()
    geocoded["lat"] = geocoded["geocode_query"].map(
        lambda query: (cache.get(query) or {}).get("lat")
    )
    geocoded["lon"] = geocoded["geocode_query"].map(
        lambda query: (cache.get(query) or {}).get("lon")
    )
    geocoded["geocode_status"] = geocoded["geocode_query"].map(
        lambda query: (cache.get(query) or {}).get("status", "not_attempted")
    )
    return geocoded


def merge_uploaded_geocode_cache(mapping_df: pd.DataFrame, uploaded_cache: pd.DataFrame) -> pd.DataFrame:
    required = {"query", "lat", "lon"}
    missing = required.difference(uploaded_cache.columns)
    if missing:
        raise ValueError(f"Uploaded coordinate cache is missing columns: {', '.join(sorted(missing))}")

    cache = uploaded_cache.copy()
    cache = cache.rename(columns={"query": "geocode_query"})
    keep = [column for column in ["geocode_query", "lat", "lon", "display_name", "status"] if column in cache.columns]
    merged = mapping_df.merge(cache[keep].drop_duplicates("geocode_query"), on="geocode_query", how="left")
    if "status" in merged.columns:
        merged = merged.rename(columns={"status": "geocode_status"})
    else:
        merged["geocode_status"] = merged["lat"].notna().map({True: "matched", False: "not_found"})
    return merged


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _text_fit(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, min_size: int = 18, bold: bool = False):
    for size in range(start_size, min_size - 1, -1):
        font = get_font(size, bold=bold)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font
    return get_font(min_size, bold=bold)


def _render_static_map(points: pd.DataFrame, width: int, height: int) -> Image.Image:
    try:
        from staticmap import CircleMarker, StaticMap
    except ImportError as exc:
        raise RuntimeError("Install the 'staticmap' package to generate the Facebook image.") from exc

    tile_url = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    static_map = StaticMap(width, height, url_template=tile_url)

    for _, row in points.dropna(subset=["lat", "lon"]).iterrows():
        risk = str(row.get("landslide_risk", "Moderate"))
        count = max(1, int(row.get("affected_barangays", 1)))
        radius = int(min(18, 5 + math.sqrt(count) * 1.4))
        static_map.add_marker(CircleMarker((float(row["lon"]), float(row["lat"])), RISK_COLORS.get(risk, "#fbc02d"), radius))

    return static_map.render(zoom=None)


def create_facebook_poster(
    geocoded_df: pd.DataFrame,
    title: str,
    subtitle: str,
    source_note: str,
    width: int = 1080,
    height: int = 1350,
) -> Image.Image:
    """Generate a 4:5 Facebook-friendly PNG with an OSM basemap."""
    canvas = Image.new("RGB", (width, height), "#f4f7fa")
    draw = ImageDraw.Draw(canvas)

    navy = "#0b2d55"
    body = "#18344f"
    line = "#d4dde6"

    margin = 34
    title_font = _text_fit(draw, title, width - margin * 2, 54, min_size=32, bold=True)
    draw.text((margin, 28), title, fill=navy, font=title_font)
    subtitle_font = _text_fit(draw, subtitle, width - margin * 2, 28, min_size=18, bold=False)
    draw.text((margin, 96), subtitle, fill=body, font=subtitle_font)

    map_top = 145
    map_height = 820
    map_width = width - margin * 2
    map_image = _render_static_map(geocoded_df, map_width, map_height)
    map_image = map_image.convert("RGB")
    canvas.paste(map_image, (margin, map_top))
    draw.rounded_rectangle((margin, map_top, margin + map_width, map_top + map_height), radius=10, outline=navy, width=3)

    # Legend.
    legend_x = margin + 20
    legend_y = map_top + 18
    legend_w = 260
    legend_h = 150
    draw.rounded_rectangle((legend_x, legend_y, legend_x + legend_w, legend_y + legend_h), radius=12, fill="#ffffff", outline=navy, width=2)
    draw.text((legend_x + 16, legend_y + 12), "LANDSLIDE RISK", fill=navy, font=get_font(23, bold=True))
    legend_items = [("Very High", RISK_COLORS["Very High"]), ("High", RISK_COLORS["High"]), ("Moderate", RISK_COLORS["Moderate"])]
    for i, (label, color) in enumerate(legend_items):
        y = legend_y + 52 + i * 30
        draw.ellipse((legend_x + 18, y, legend_x + 38, y + 20), fill=color)
        draw.text((legend_x + 48, y - 2), label, fill=body, font=get_font(20))

    stats_top = map_top + map_height + 20
    matched = geocoded_df.dropna(subset=["lat", "lon"]).copy()
    total_affected = int(geocoded_df["affected_barangays"].sum()) if not geocoded_df.empty else 0
    total_locations = int(len(geocoded_df))
    matched_locations = int(len(matched))

    draw.rounded_rectangle((margin, stats_top, width - margin, height - 72), radius=14, fill="#ffffff", outline=line, width=2)
    draw.text((margin + 24, stats_top + 20), f"{total_affected:,}", fill=navy, font=get_font(62, bold=True))
    draw.text((margin + 24, stats_top + 89), "affected barangays represented", fill=body, font=get_font(23))
    draw.text((margin + 24, stats_top + 132), f"Mapped locations: {matched_locations:,} of {total_locations:,}", fill=body, font=get_font(21))

    # Top provinces summary.
    if "province" in geocoded_df.columns:
        province_summary = (
            geocoded_df.groupby("province", dropna=False)["affected_barangays"]
            .sum()
            .sort_values(ascending=False)
            .head(5)
        )
        table_x = 500
        draw.text((table_x, stats_top + 23), "TOP AREAS", fill=navy, font=get_font(26, bold=True))
        for idx, (province, count) in enumerate(province_summary.items(), start=1):
            y = stats_top + 62 + (idx - 1) * 37
            label = str(province) if str(province).strip() else "Unspecified"
            label_font = _text_fit(draw, f"{idx}. {label}", 390, 22, min_size=16)
            draw.text((table_x, y), f"{idx}. {label}", fill=body, font=label_font)
            count_text = f"{int(count):,}"
            count_box = draw.textbbox((0, 0), count_text, font=get_font(22, bold=True))
            draw.text((width - margin - 25 - (count_box[2] - count_box[0]), y), count_text, fill=navy, font=get_font(22, bold=True))

    footer_font = get_font(16)
    footer = f"{source_note}  |  Approximate centroid-based visualization  |  © OpenStreetMap contributors"
    footer_font = _text_fit(draw, footer, width - margin * 2, 16, min_size=12)
    draw.text((margin, height - 48), footer, fill="#526779", font=footer_font)
    return canvas


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def dataframe_to_geojson_bytes(df: pd.DataFrame) -> bytes:
    features = []
    for _, row in df.dropna(subset=["lat", "lon"]).iterrows():
        properties = {
            key: (None if pd.isna(value) else value)
            for key, value in row.items()
            if key not in {"lat", "lon"}
        }
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
                "properties": properties,
            }
        )
    payload = {"type": "FeatureCollection", "features": features}
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def image_to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]
