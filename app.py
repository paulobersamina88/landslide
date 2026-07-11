from __future__ import annotations

import io
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from folium.plugins import HeatMap, MarkerCluster
from streamlit_folium import folium_static

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

@st.cache_data(show_spinner=False)
def cached_parse_mgb_pdf(pdf_bytes: bytes):
    return parse_mgb_pdf(pdf_bytes)


try:
    with st.spinner("Extracting the MGB table…"):
        parsed_df, metadata = cached_parse_mgb_pdf(pdf_bytes)
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

if level == "Municipality" and not mapping_df.empty:
    st.subheader("Municipality landslide summary")

    municipality_summary = (
        mapping_df[
            [
                "region",
                "province",
                "municipality",
                "affected_barangays",
                "very_high",
                "high",
                "moderate",
                "landslide_risk",
            ]
        ]
        .sort_values(
            ["affected_barangays", "province", "municipality"],
            ascending=[False, True, True],
        )
        .reset_index(drop=True)
    )

    st.dataframe(
        municipality_summary,
        use_container_width=True,
        height=350,
        column_config={
            "region": "Region",
            "province": "Province",
            "municipality": "Municipality",
            "affected_barangays": "Affected Barangays",
            "very_high": "Very High",
            "high": "High",
            "moderate": "Moderate",
            "landslide_risk": "Highest Risk",
        },
    )

    st.subheader("Top municipalities by affected barangays")

    chart_df = municipality_summary.head(20).copy()
    chart_df["Municipality"] = (
        chart_df["municipality"] + ", " + chart_df["province"]
    )

    st.bar_chart(
        chart_df.set_index("Municipality")["affected_barangays"],
        height=500,
    )

    st.download_button(
        "Download municipality summary CSV",
        dataframe_to_csv_bytes(municipality_summary),
        file_name=f"mgb_municipality_summary_{pdf_id}.csv",
        mime="text/csv",
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

    municipality = str(row.get("municipality", ""))
    province = str(row.get("province", ""))

    barangay_names = row.get("barangay_names", [])

    if not isinstance(barangay_names, list):
        barangay_names = []

    barangay_names = [
        str(name).strip()
        for name in barangay_names
        if str(name).strip()
    ]

    visible_barangays = barangay_names[:3]
    remaining_barangays = max(
        0,
        len(barangay_names) - len(visible_barangays),
    )

    barangay_label = ", ".join(visible_barangays)

    if remaining_barangays > 0:
        barangay_label += f" +{remaining_barangays} more"

    full_barangay_list = ", ".join(barangay_names)

    if not full_barangay_list:
        full_barangay_list = "Barangay names not available"

    if not barangay_label:
        barangay_label = "Barangay names unavailable"

    very_high = int(row.get("very_high", 0))
    high = int(row.get("high", 0))
    moderate = int(row.get("moderate", 0))

    tooltip = (
        f"<div style='font-size:12px; min-width:220px;'>"
        f"<b>{municipality}, {province}</b><br>"
        f"<b>Total affected barangays: {count}</b><br>"
        f"Highest risk: {risk}<br>"
        f"Very High: {very_high}<br>"
        f"High: {high}<br>"
        f"Moderate: {moderate}<br><br>"
        f"<b>Affected barangays:</b><br>"
        f"{full_barangay_list}"
        f"</div>"
    )

    marker_radius = min(26, 7 + count ** 0.65)

    folium.CircleMarker(
        location=[float(row["lat"]), float(row["lon"])],
        radius=marker_radius,
        color=RISK_COLORS.get(risk, "#fbc02d"),
        fill=True,
        fill_color=RISK_COLORS.get(risk, "#fbc02d"),
        fill_opacity=0.78,
        weight=2,
        tooltip=folium.Tooltip(tooltip),
        popup=folium.Popup(tooltip, max_width=380),
    ).add_to(cluster)

    if level == "Municipality":
        folium.Marker(
            location=[float(row["lat"]), float(row["lon"])],
            tooltip=folium.Tooltip(tooltip),
            popup=folium.Popup(tooltip, max_width=380),
            icon=folium.DivIcon(
                icon_size=(180, 64),
                icon_anchor=(90, 32),
                html=f"""
                <div style="
                    display:inline-block;
                    transform:translate(-50%, -50%);
                    width:170px;
                    background:rgba(255,255,255,0.92);
                    border:1.5px solid {RISK_COLORS.get(risk, '#fbc02d')};
                    border-radius:5px;
                    padding:3px 4px;
                    color:#111;
                    text-align:center;
                    line-height:1.10;
                    box-shadow:0 2px 6px rgba(0,0,0,0.30);
                ">
                    <div style="
                        font-size:12px;
                        font-weight:bold;
                        white-space:nowrap;
                        overflow:hidden;
                        text-overflow:ellipsis;
                    ">
                        {municipality}: {count}
                    </div>

                    <div style="
                        margin-top:4px;
                        font-size:9px;
                        font-weight:normal;
                        white-space:normal;
                        overflow:hidden;
                        max-height:34px;
                    ">
                        {barangay_label}
                    </div>
                </div>
                """,
            ),
        ).add_to(folium_map)

    heat_data.append(
        [
            float(row["lat"]),
            float(row["lon"]),
            max(1, count),
        ]
    )

# if level == "Barangay" and heat_data:
#     HeatMap(
#         heat_data,
#         name="Heat intensity",
#         radius=24,
#         blur=20,
#         min_opacity=0.3,
#     ).add_to(folium_map)
# NEW VERSION — heatmap in both Municipality and Barangay modes
if heat_data:
    HeatMap(
        heat_data,
        name="Heat intensity",
        radius=24,
        blur=20,
        min_opacity=0.3,
        show=True,
    ).add_to(folium_map)

folium.LayerControl(collapsed=False).add_to(folium_map)
folium_static(folium_map, width=None, height=650)

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
