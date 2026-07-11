from __future__ import annotations

import io
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from folium.plugins import HeatMap, MarkerCluster
from streamlit_folium import st_folium

from mgb_core import (
    RISK_COLORS,
    aggregate_for_mapping,
    create_facebook_poster,
    dataframe_to_csv_bytes,
    dataframe_to_geojson_bytes,
    geocode_locations,
    hash_bytes,
    image_to_png_bytes,
    merge_uploaded_geocode_cache,
    parse_mgb_pdf,
)

st.set_page_config(page_title="MGB PDF to OSM Visualizer", page_icon="🗺️", layout="wide")

st.title("MGB PDF to OpenStreetMap Visualizer")
st.caption(
    "Upload an MGB barangay-list PDF, extract landslide entries, geocode the listed places, "
    "view them on OpenStreetMap, and export a Facebook-friendly 1080 × 1350 PNG."
)

with st.expander("Important accuracy note", expanded=False):
    st.info(
        "The uploaded MGB PDF contains place names and susceptibility codes, not coordinates or hazard polygons. "
        "This app therefore geocodes place-name centroids. The result is an approximate location visualization, "
        "not a replacement for official MGB geohazard maps or GIS layers. For precise barangay boundaries, upload "
        "an authoritative PSGC/MGB coordinate cache or GeoJSON in a future enhancement."
    )

uploaded_pdf = st.file_uploader("Upload the updated MGB PDF", type=["pdf"])

if uploaded_pdf is None:
    st.stop()

pdf_bytes = uploaded_pdf.getvalue()
pdf_id = hash_bytes(pdf_bytes)

parse_status = st.empty()
parse_progress = st.progress(0)


def parse_progress_callback(page: int, total: int) -> None:
    parse_progress.progress(page / total)
    parse_status.caption(f"Reading page {page} of {total}…")


try:
    with st.spinner("Extracting the MGB table…"):
        parsed_df, metadata = parse_mgb_pdf(pdf_bytes, progress_callback=parse_progress_callback)
except Exception as exc:
    st.error(f"The PDF could not be parsed: {exc}")
    st.stop()
finally:
    parse_progress.empty()
    parse_status.empty()

landslide_df = parsed_df[parsed_df["has_landslide"]].copy()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Rows extracted", f"{len(parsed_df):,}")
c2.metric("Landslide-listed barangays", f"{len(landslide_df):,}")
c3.metric("Regions", f"{landslide_df['region'].nunique():,}")
c4.metric("Provinces", f"{landslide_df['province'].nunique():,}")

if metadata.advisory_date or metadata.advisory_time:
    st.caption(f"Detected advisory: {metadata.advisory_date or 'date not detected'} • {metadata.advisory_time or 'time not detected'}")

st.subheader("1. Review and filter")
filter_col1, filter_col2, filter_col3 = st.columns([1.4, 1.4, 1])

regions = sorted(value for value in landslide_df["region"].dropna().unique() if value)
selected_regions = filter_col1.multiselect("Regions", regions, default=regions)

region_filtered = landslide_df[landslide_df["region"].isin(selected_regions)].copy()
provinces = sorted(value for value in region_filtered["province"].dropna().unique() if value)
selected_provinces = filter_col2.multiselect("Provinces", provinces, default=provinces)

minimum_risk = filter_col3.selectbox("Minimum landslide risk", ["Moderate", "High", "Very High"], index=0)
risk_allowed = {
    "Moderate": ["Moderate", "High", "Very High"],
    "High": ["High", "Very High"],
    "Very High": ["Very High"],
}[minimum_risk]

filtered_df = region_filtered[
    region_filtered["province"].isin(selected_provinces)
    & region_filtered["landslide_risk"].isin(risk_allowed)
].copy()

st.dataframe(
    filtered_df[["region", "province", "municipality", "barangay", "landslide_risk", "page"]],
    use_container_width=True,
    height=300,
)

summary = (
    filtered_df.groupby(["region", "province"], dropna=False)
    .size()
    .reset_index(name="affected_barangays")
    .sort_values("affected_barangays", ascending=False)
)
st.download_button(
    "Download extracted barangay CSV",
    dataframe_to_csv_bytes(filtered_df),
    file_name=f"mgb_landslide_barangays_{pdf_id}.csv",
    mime="text/csv",
)

st.subheader("2. Choose mapping detail")
level = st.radio(
    "Geocoding level",
    ["Municipality", "Barangay"],
    horizontal=True,
    help=(
        "Municipality is recommended: it aggregates barangays and is much faster. "
        "Barangay mode may require thousands of rate-limited requests and OSM coverage varies."
    ),
)

mapping_df = aggregate_for_mapping(filtered_df, level)
st.write(
    f"This selection contains **{int(mapping_df['affected_barangays'].sum()) if not mapping_df.empty else 0:,} affected barangays** "
    f"across **{len(mapping_df):,} locations to geocode**."
)

st.subheader("3. Add coordinates")
st.caption(
    "Use the public Nominatim service for a small/occasional run, or upload a saved coordinate cache to avoid repeated queries."
)

cache_upload = st.file_uploader(
    "Optional coordinate cache CSV",
    type=["csv"],
    help="Required columns: query, lat, lon. Optional: display_name, status.",
)

geocoded_df = None
if cache_upload is not None:
    try:
        uploaded_cache = pd.read_csv(cache_upload)
        geocoded_df = merge_uploaded_geocode_cache(mapping_df, uploaded_cache)
        st.success("Coordinate cache applied.")
    except Exception as exc:
        st.error(f"Coordinate cache could not be applied: {exc}")

with st.form("geocode_form"):
    email = st.text_input(
        "Contact email for the Nominatim user-agent",
        placeholder="name@example.com",
        help="Required when using the public OpenStreetMap Nominatim geocoder.",
    )
    start_geocoding = st.form_submit_button("Geocode unmatched locations")

if start_geocoding:
    geocode_status = st.empty()
    geocode_progress = st.progress(0)

    def geocode_progress_callback(index: int, total: int, query: str) -> None:
        geocode_progress.progress(index / max(total, 1))
        geocode_status.caption(f"Geocoding {index} of {total}: {query}")

    cache_path = Path(".cache") / f"mgb_geocode_{level.lower()}.json"
    try:
        with st.spinner("Geocoding. Keep this tab open until the run finishes…"):
            geocoded_df = geocode_locations(
                mapping_df,
                email=email,
                cache_path=cache_path,
                progress_callback=geocode_progress_callback,
            )
        st.session_state[f"geocoded_{pdf_id}_{level}"] = geocoded_df
        st.success("Geocoding finished.")
    except Exception as exc:
        st.error(f"Geocoding failed: {exc}")
    finally:
        geocode_progress.empty()
        geocode_status.empty()

session_key = f"geocoded_{pdf_id}_{level}"
if geocoded_df is None and session_key in st.session_state:
    geocoded_df = st.session_state[session_key]

if geocoded_df is None:
    st.info("Add coordinates to continue to the map and Facebook image.")
    st.stop()

matched_df = geocoded_df.dropna(subset=["lat", "lon"]).copy()
match_rate = len(matched_df) / max(len(geocoded_df), 1)
st.progress(match_rate, text=f"Geocoded {len(matched_df):,} of {len(geocoded_df):,} locations ({match_rate:.1%})")

unmatched = geocoded_df[geocoded_df[["lat", "lon"]].isna().any(axis=1)]
if not unmatched.empty:
    with st.expander(f"Unmatched locations ({len(unmatched):,})"):
        st.dataframe(unmatched[["map_label", "geocode_query"]], use_container_width=True)

st.subheader("4. Interactive OpenStreetMap")
if matched_df.empty:
    st.warning("No locations were geocoded successfully.")
    st.stop()

center = [float(matched_df["lat"].mean()), float(matched_df["lon"].mean())]
folium_map = folium.Map(location=center, zoom_start=6, tiles="OpenStreetMap", control_scale=True)

cluster = MarkerCluster(name="Mapped locations").add_to(folium_map)
heat_data = []
for _, row in matched_df.iterrows():
    risk = str(row.get("landslide_risk", "Moderate"))
    count = int(row.get("affected_barangays", 1))
    tooltip = (
        f"<b>{row.get('map_label', '')}</b><br>"
        f"Affected barangays: {count}<br>"
        f"Very High: {int(row.get('very_high', 0))}<br>"
        f"High: {int(row.get('high', 0))}<br>"
        f"Moderate: {int(row.get('moderate', 0))}"
    )
    folium.CircleMarker(
        location=[float(row["lat"]), float(row["lon"])],
        radius=min(18, 5 + count ** 0.5),
        color=RISK_COLORS.get(risk, "#fbc02d"),
        fill=True,
        fill_color=RISK_COLORS.get(risk, "#fbc02d"),
        fill_opacity=0.72,
        weight=2,
        tooltip=folium.Tooltip(tooltip),
    ).add_to(cluster)
    heat_data.append([float(row["lat"]), float(row["lon"]), max(1, count)])

HeatMap(heat_data, name="Heat intensity", radius=24, blur=20, min_opacity=0.3).add_to(folium_map)
folium.LayerControl(collapsed=False).add_to(folium_map)
st_folium(folium_map, use_container_width=True, height=650)

export_c1, export_c2 = st.columns(2)
export_c1.download_button(
    "Download mapped CSV",
    dataframe_to_csv_bytes(geocoded_df),
    file_name=f"mgb_mapped_locations_{pdf_id}.csv",
    mime="text/csv",
)
export_c2.download_button(
    "Download GeoJSON",
    dataframe_to_geojson_bytes(geocoded_df),
    file_name=f"mgb_mapped_locations_{pdf_id}.geojson",
    mime="application/geo+json",
)

st.subheader("5. Facebook-friendly visualization")
default_title = "Possible Rain-Induced Landslide Locations"
if len(selected_regions) == 1:
    default_title = f"{selected_regions[0]} Rain-Induced Landslide Locations"
elif len(selected_provinces) == 1:
    default_title = f"{selected_provinces[0]} Rain-Induced Landslide Locations"

poster_title = st.text_input("Poster title", value=default_title)
date_time = " • ".join(part for part in [metadata.advisory_date, metadata.advisory_time] if part)
poster_subtitle = st.text_input(
    "Poster subtitle",
    value=f"MGB barangay list{(' • ' + date_time) if date_time else ''}",
)

if st.button("Generate 1080 × 1350 PNG", type="primary"):
    try:
        with st.spinner("Downloading OpenStreetMap tiles and composing the image…"):
            poster = create_facebook_poster(
                geocoded_df,
                title=poster_title,
                subtitle=poster_subtitle,
                source_note="Source: uploaded MGB barangay list",
            )
        st.image(poster, caption="Facebook-friendly 4:5 visualization", use_container_width=True)
        st.download_button(
            "Download Facebook PNG",
            image_to_png_bytes(poster),
            file_name=f"mgb_facebook_visual_{pdf_id}.png",
            mime="image/png",
        )
    except Exception as exc:
        st.error(f"The poster could not be generated: {exc}")
