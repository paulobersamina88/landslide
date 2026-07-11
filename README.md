# MGB PDF to OpenStreetMap Visualizer

A Streamlit app that:

1. accepts the recurring MGB barangay-list PDF;
2. extracts landslide susceptibility entries (`VHL`, `HL`, `ML`);
3. filters by region, province, and risk level;
4. geocodes municipality or barangay place names using OpenStreetMap Nominatim;
5. displays clustered markers and a heat layer on OpenStreetMap;
6. exports CSV and GeoJSON; and
7. generates a Facebook-friendly 1080 x 1350 PNG.

## Important limitation

The MGB PDF contains place names and hazard codes, not coordinates or hazard polygons. The app therefore creates an **approximate centroid-based visualization**. It is not a substitute for official MGB geohazard maps or GIS layers.

For routine institutional use, municipality-level aggregation is recommended. Barangay-level geocoding can require thousands of rate-limited requests and some barangays may not exist or may be named differently in OpenStreetMap.

## Run locally

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

Then open the local Streamlit URL shown in the terminal.

## Deploy to Streamlit Community Cloud

1. Put `app.py`, `mgb_core.py`, and `requirements.txt` in a GitHub repository.
2. Create a Streamlit Community Cloud app from that repository.
3. Set `app.py` as the entry point.

## Recommended workflow

- Use **Municipality** mode first.
- Enter a real contact email when using the public Nominatim geocoder.
- Download the mapped CSV after the first successful run.
- Re-upload that CSV as the coordinate cache in future sessions to avoid repeated geocoding.

The cache CSV must contain at least:

```text
query,lat,lon
```

Optional columns:

```text
display_name,status
```

## Parser validation

The parser was checked against the provided `10JULY2026_8PM_MGB_Brgy_List.pdf` and should extract the same landslide totals used in the conversation, including:

- nationwide landslide-listed barangays: 3,607;
- CALABARZON: 439;
- Mindoro Island: 102.

Future MGB PDFs with a substantially different table layout may require updating the normalized column ranges in `mgb_core.py`.
