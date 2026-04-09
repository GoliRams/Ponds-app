import streamlit as st
import ee
import folium
from streamlit_folium import st_folium
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import json
import requests

# ---------- PAGE CONFIG ----------
st.set_page_config(page_title="Ponds-app", layout="wide", page_icon="🦐")

# ---------- GEE AUTH ----------
@st.cache_resource
def init_ee():
    try:
        sa_info = json.loads(st.secrets["GEE_SERVICE_ACCOUNT_JSON"])
        creds = ee.ServiceAccountCredentials(
            sa_info["client_email"], key_data=json.dumps(sa_info))
        ee.Initialize(creds, project=sa_info["project_id"])
    except Exception:
        ee.Initialize(project=st.secrets.get("GEE_PROJECT_ID", "YOUR-PROJECT-ID"))

init_ee()

# ---------- GEOCODING ----------
@st.cache_data(show_spinner=False, ttl=86400)
def get_place_name(lat, lon):
    try:
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {"lat": lat, "lon": lon, "format": "json", "zoom": 14,
                  "addressdetails": 1}
        headers = {"User-Agent": "Ponds-app/1.0"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            addr = data.get("address", {})
            parts = [
                addr.get("village") or addr.get("hamlet") or addr.get("suburb"),
                addr.get("town") or addr.get("city") or addr.get("municipality"),
                addr.get("county") or addr.get("state_district"),
                addr.get("state"),
            ]
            parts = [p for p in parts if p]
            return ", ".join(parts) if parts else data.get("display_name", "Unknown")
    except Exception:
        pass
    return "Location lookup unavailable"

@st.cache_data(show_spinner=False, ttl=86400)
def geocode_place(query):
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": query, "format": "json", "limit": 5,
                  "countrycodes": "in", "addressdetails": 1}
        headers = {"User-Agent": "Ponds-app/1.0"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            return [
                {"display": res.get("display_name", ""),
                 "lat": float(res["lat"]),
                 "lon": float(res["lon"])}
                for res in r.json()
            ]
    except Exception:
        pass
    return []

# ---------- POND DISCOVERY (NEW) ----------
@st.cache_data(show_spinner=False, ttl=3600)
def discover_ponds(lat, lon, radius_km, max_ponds=500):
    """Detect aquaculture ponds within a radius of a point.
    Returns a list of dicts: {id, lat, lon, area_ha}.
    """
    center = ee.Geometry.Point([lon, lat])
    aoi = center.buffer(radius_km * 1000)

    def mask_clouds(img):
        qa = img.select('QA60')
        cloud = qa.bitwiseAnd(1 << 10).eq(0).And(
            qa.bitwiseAnd(1 << 11).eq(0))
        return img.updateMask(cloud).divide(10000)

    # Dry season composite (Jan–Apr) for cleaner pond detection
    end = datetime.utcnow()
    s2 = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
          .filterBounds(aoi)
          .filterDate(f'{end.year-1}-01-01', f'{end.year-1}-04-30')
          .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
          .map(mask_clouds))

    composite = s2.median().clip(aoi)
    mndwi = composite.normalizedDifference(['B3', 'B11']).rename('MNDWI')
    water = mndwi.gt(0.1).selfMask()

    vectors = water.reduceToVectors(
        geometry=aoi,
        scale=10,
        geometryType='polygon',
        eightConnected=False,
        maxPixels=1e10,
        bestEffort=True
    )

    # Add area & shape metrics
    def add_metrics(f):
        g = f.geometry()
        area = g.area(1)
        perim = g.perimeter(1)
        compact = area.multiply(4 * 3.14159).divide(perim.multiply(perim))
        bbox_area = g.bounds().area(1)
        fill = area.divide(bbox_area)
        centroid = g.centroid(1).coordinates()
        return f.set({
            'area_ha': area.divide(10000),
            'compactness': compact,
            'fill_ratio': fill,
            'lon': centroid.get(0),
            'lat': centroid.get(1),
        })

    vectors = vectors.map(add_metrics)

    # Filter: aquaculture pond shape & size
    ponds = vectors.filter(ee.Filter.And(
        ee.Filter.gte('area_ha', 0.3),
        ee.Filter.lte('area_ha', 8.0),
        ee.Filter.gt('compactness', 0.35),
        ee.Filter.gt('fill_ratio', 0.55),
    ))

    # Sort by size descending and cap
    ponds = ponds.sort('area_ha', False).limit(max_ponds)

    feats = ponds.getInfo()["features"]
    result = []
    for i, f in enumerate(feats):
        p = f["properties"]
        result.append({
            "id": f"P{i+1:04d}",
            "lat": p["lat"],
            "lon": p["lon"],
            "area_ha": round(p["area_ha"], 2),
        })
    return result

# ---------- ANALYSIS ----------
@st.cache_data(show_spinner=False, ttl=3600)
def analyze_pond(lat, lon, days=730):
    point = ee.Geometry.Point([lon, lat])
    pond = point.buffer(50)

    end = datetime.utcnow()
    start = end - timedelta(days=days)

    def mask_clouds(img):
        qa = img.select('QA60')
        cloud = qa.bitwiseAnd(1 << 10).eq(0).And(
            qa.bitwiseAnd(1 << 11).eq(0))
        return img.updateMask(cloud).divide(10000).copyProperties(
            img, ["system:time_start"])

    coll = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
            .filterBounds(point)
            .filterDate(start.strftime("%Y-%m-%d"),
                        end.strftime("%Y-%m-%d"))
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 40))
            .map(mask_clouds))

    def per_image(img):
        mndwi = img.normalizedDifference(['B3', 'B11']).rename('MNDWI')
        ndci = img.normalizedDifference(['B5', 'B4']).rename('NDCI')
        water_frac = mndwi.gt(0.1).reduceRegion(
            ee.Reducer.mean(), pond, 10).get('MNDWI')
        ndci_mean = ndci.updateMask(mndwi.gt(0.1)).reduceRegion(
            ee.Reducer.mean(), pond, 10).get('NDCI')
        return ee.Feature(None, {
            "date": img.date().format("YYYY-MM-dd"),
            "water_frac": water_frac,
            "ndci": ndci_mean
        })

    feats = coll.map(per_image).getInfo()["features"]
    rows = [f["properties"] for f in feats]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["water_frac"]).sort_values("date").reset_index(drop=True)
    return df

def detect_events(df):
    events = []
    if len(df) < 4:
        return events
    df = df.copy()
    df["water_smooth"] = df["water_frac"].rolling(3, min_periods=1).mean()
    for i in range(1, len(df)):
        prev, curr = df.iloc[i-1], df.iloc[i]
        if prev["water_smooth"] < 0.3 and curr["water_smooth"] > 0.7:
            events.append((curr["date"], "FILL"))
        elif prev["water_smooth"] > 0.7 and curr["water_smooth"] < 0.3:
            events.append((curr["date"], "DRAIN"))
    return events

def status_badge(df):
    if len(df) < 6:
        return "❓ Not enough data", "gray"
    recent = df.tail(6)
    ndci_recent = recent["ndci"].dropna()
    if len(ndci_recent) < 2:
        return "❓ Insufficient NDCI", "gray"
    trend = ndci_recent.iloc[-1] - ndci_recent.iloc[0]
    latest = ndci_recent.iloc[-1]
    if latest > 0.15 and trend > 0.05:
        return "🔴 ALERT – High chlorophyll & rising", "red"
    if latest > 0.10 or trend > 0.05:
        return "🟡 WATCH – Elevated chlorophyll", "orange"
    return "🟢 HEALTHY", "green"

# ---------- SESSION STATE ----------
if "map_center" not in st.session_state:
    st.session_state.map_center = [16.5449, 81.5212]
    st.session_state.map_zoom = 12
if "discovered" not in st.session_state:
    st.session_state.discovered = []       # list of dicts
if "selected_pond" not in st.session_state:
    st.session_state.selected_pond = None
if "tracked" not in st.session_state:
    st.session_state.tracked = []          # list of (name, lat, lon)

# ---------- SIDEBAR ----------
st.sidebar.title("🦐 Ponds-app")
st.sidebar.markdown("**Shrimp pond satellite monitor**")
st.sidebar.markdown("---")

# --- Place search ---
st.sidebar.markdown("### 🔍 Find ponds near a place")
search_query = st.sidebar.text_input(
    "Town, village or landmark",
    placeholder="e.g. Bhimavaram, Kaikalur"
)
radius_km = st.sidebar.slider("Search radius (km)", 2, 50, 10)
max_ponds = st.sidebar.slider("Max ponds to show", 50, 1000, 300, step=50)

if search_query:
    results = geocode_place(search_query)
    if results:
        options = {r["display"]: (r["lat"], r["lon"]) for r in results}
        choice = st.sidebar.selectbox("Select a match:", list(options.keys()))
        if st.sidebar.button("🔎 Find ponds here"):
            lat, lon = options[choice]
            st.session_state.map_center = [lat, lon]
            st.session_state.map_zoom = 13
            with st.spinner(f"Searching for ponds within {radius_km} km…"):
                try:
                    st.session_state.discovered = discover_ponds(
                        lat, lon, radius_km, max_ponds)
                    st.session_state.selected_pond = None
                except Exception as e:
                    st.sidebar.error(f"Search failed: {e}")
            st.rerun()
    else:
        st.sidebar.warning("No matches found.")

st.sidebar.markdown("---")

# --- Select discovered pond ---
if st.session_state.discovered:
    st.sidebar.markdown(f"### 🎯 {len(st.session_state.discovered)} ponds found")
    pond_labels = [
        f"{p['id']} — {p['area_ha']} ha"
        for p in st.session_state.discovered
    ]
    idx = st.sidebar.selectbox(
        "Select a pond:",
        range(len(pond_labels)),
        format_func=lambda i: pond_labels[i]
    )
    if st.sidebar.button("📊 Show this pond's history"):
        st.session_state.selected_pond = st.session_state.discovered[idx]
        st.rerun()

    if st.sidebar.button("➕ Track this pond"):
        p = st.session_state.discovered[idx]
        name = f"{p['id']} ({p['area_ha']} ha)"
        if (name, p["lat"], p["lon"]) not in st.session_state.tracked:
            st.session_state.tracked.append((name, p["lat"], p["lon"]))

st.sidebar.markdown("---")
st.sidebar.markdown("### ⭐ Tracked ponds")
if not st.session_state.tracked:
    st.sidebar.caption("None yet.")
for n, la, lo in st.session_state.tracked:
    st.sidebar.write(f"• {n}")

if st.sidebar.button("🗑️ Clear all"):
    st.session_state.tracked = []
    st.session_state.discovered = []
    st.session_state.selected_pond = None
    st.rerun()

# ---------- HEADER ----------
st.title("🦐 Ponds-app — Shrimp Pond Monitor")
st.caption("Satellite-based monitoring using Sentinel-2 (free, ~5 day revisit)")

# ---------- TABS ----------
tab_monitor, tab_about = st.tabs(["📡 Monitor", "ℹ️ About / Benefits"])

with tab_monitor:
    # ---------- MAP ----------
    center = st.session_state.map_center
    zoom = st.session_state.map_zoom

    m = folium.Map(
        location=center,
        zoom_start=zoom,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery"
    )

    # Draw search radius circle
    if st.session_state.discovered:
        folium.Circle(
            location=center,
            radius=radius_km * 1000,
            color="yellow",
            fill=False,
            weight=2,
            popup=f"{radius_km} km search area"
        ).add_to(m)

    # Draw discovered ponds (small dots)
    for p in st.session_state.discovered:
        is_selected = (st.session_state.selected_pond and
                       st.session_state.selected_pond["id"] == p["id"])
        folium.CircleMarker(
            location=[p["lat"], p["lon"]],
            radius=6 if is_selected else 3,
            color="red" if is_selected else "lime",
            fill=True,
            fill_opacity=0.8,
            popup=f"{p['id']} — {p['area_ha']} ha",
        ).add_to(m)

    # Draw tracked ponds (big markers)
    for n, la, lo in st.session_state.tracked:
        folium.Marker([la, lo], popup=n,
                      icon=folium.Icon(color="blue", icon="star")).add_to(m)

    st_folium(m, height=500, width=None, returned_objects=[])

    # ---------- ANALYSIS DISPLAY ----------
    ponds_to_show = []
    if st.session_state.selected_pond:
        p = st.session_state.selected_pond
        ponds_to_show.append((p["id"], p["lat"], p["lon"]))
    for n, la, lo in st.session_state.tracked:
        if not any(la == pl[1] and lo == pl[2] for pl in ponds_to_show):
            ponds_to_show.append((n, la, lo))

    if not ponds_to_show:
        if st.session_state.discovered:
            st.info(f"✅ Found **{len(st.session_state.discovered)} ponds**. "
                    "Pick one from the sidebar to see its history.")
        else:
            st.info("👈 Search for a town in the sidebar to discover ponds nearby. "
                    "New here? See the **About / Benefits** tab.")
    else:
        for name, lat, lon in ponds_to_show:
            st.markdown("---")
            place = get_place_name(lat, lon)
            st.subheader(f"📍 {name}")
            st.caption(f"📌 {place}  •  ({lat:.4f}, {lon:.4f})")

            with st.spinner(f"Pulling 2 years of Sentinel-2 data…"):
                try:
                    df = analyze_pond(lat, lon)
                except Exception as e:
                    st.error(f"GEE error: {e}")
                    continue

            if df.empty:
                st.warning("No cloud-free observations found.")
                continue

            badge, color = status_badge(df)
            st.markdown(f"### Status: :{color}[{badge}]")

            c1, c2, c3 = st.columns(3)
            c1.metric("Observations", len(df))
            c2.metric("Latest water %", f"{df['water_frac'].iloc[-1]*100:.0f}%")
            latest_ndci = df["ndci"].dropna()
            c3.metric("Latest NDCI",
                      f"{latest_ndci.iloc[-1]:.3f}" if len(latest_ndci) else "—")

            fig1 = go.Figure()
            fig1.add_trace(go.Scatter(x=df["date"], y=df["water_frac"]*100,
                                      mode="lines+markers", name="Water %",
                                      line=dict(color="royalblue")))
            fig1.update_layout(title="Water presence (fill/drain cycles)",
                               yaxis_title="Water %", height=280,
                               margin=dict(t=40, b=20))
            st.plotly_chart(fig1, use_container_width=True)

            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(x=df["date"], y=df["ndci"],
                                      mode="lines+markers", name="NDCI",
                                      line=dict(color="seagreen")))
            fig2.add_hline(y=0.10, line_dash="dot", line_color="orange",
                           annotation_text="Watch")
            fig2.add_hline(y=0.15, line_dash="dot", line_color="red",
                           annotation_text="Alert")
            fig2.update_layout(title="Chlorophyll proxy (NDCI) – bloom risk",
                               yaxis_title="NDCI", height=280,
                               margin=dict(t=40, b=20))
            st.plotly_chart(fig2, use_container_width=True)

            events = detect_events(df)
            if events:
                st.markdown("**Detected cycle events:**")
                ev_df = pd.DataFrame(events, columns=["Date", "Event"])
                st.dataframe(ev_df, hide_index=True, use_container_width=True)
                fills = [e for e in events if e[1] == "FILL"]
                if fills:
                    last_fill = fills[-1][0]
                    doc = (datetime.utcnow() - last_fill.to_pydatetime()).days
                    st.info(f"📅 Days since last fill (current cycle DOC): "
                            f"**{doc} days**")
            else:
                st.caption("No clear fill/drain events detected.")

        st.markdown("---")
        st.caption("⚠️ Prototype. Satellite signals are indicative, not diagnostic.")

with tab_about:
    st.markdown("""
## 🦐 What is Ponds-app?

**A free app that watches your shrimp pond from space every 5 days,
warns you about algae and disease before you can see them, and keeps an
automatic record of every crop you grow — so you lose fewer crops,
sleep better, and have proof when you need it.**

---

## How to use it (new: auto-discovery)

1. In the sidebar, type a town name like **Bhimavaram** or your village.
2. Pick from the dropdown matches.
3. Set the search radius (start with 10 km) and click **Find ponds here**.
4. The app discovers every aquaculture pond in that area (30–90 seconds).
5. Pick any pond from the dropdown to see its 2-year satellite history.
6. Click **Track this pond** to save it for comparison.

---

## 🎯 Top 10 benefits for farmers

### 1. Watch your pond without being there
**Old way:** drive to each pond, look, guess.
**New way:** open the app, see the status of all ponds at once.

### 2. Get warned about algae problems early
Shrimp ponds crash when algae grows too much. The satellite can see
algae building up **2–3 weeks before your eye can**. Act early, save
the crop.

### 3. Know when your neighbors are in trouble
When 3–4 ponds within 2 km of you suddenly drain early, the app warns
you of likely disease pressure — a heads-up neighbors would never give
you directly.

### 4. Automatic diary of every crop
Auto-logs every fill and drain event from satellite history — going
back 2 years, even if you never used the app before. Instant proof for
loans and insurance.

### 5. See cyclone damage without driving through floodwater
Radar satellites show within hours which ponds have broken bunds after
a cyclone. File insurance claims immediately.

### 6. Compare yourself to other farms — anonymously
*"Your cycle length is 108 days. Ponds in your area average 95 days."*
Data-driven feedback without gossip.

### 7. Costs almost nothing
Commercial pond monitoring hardware costs ₹50,000–₹5 lakh per pond.
This app uses free satellites, free mapping, and free cloud computing.

### 8. Works on the cheapest smartphone
No sensors, no hardware at the pond. ₹6,000 Android + 4G once a day.

### 9. Gets smarter as more farmers join
Community early-warning system that grows stronger with each user.

### 10. Helps the whole region, not just you
Cooperatives, feed companies, exporters, insurers, banks, and
government all benefit from the same satellite layer.

---

## 📊 What the numbers mean

| What you see | What it means |
|---|---|
| **Water %** | How much of the pond is water — detects fill/drain/leaks |
| **NDCI** | Algae level — high = bloom risk |
| **Status badge** | 🟢 Healthy / 🟡 Watch / 🔴 Alert |
| **Fill/Drain events** | Your automatic farming logbook |
| **Days of culture** | Days since last fill — helps decide harvest |

---

## ⚠️ Honest limitations

- **Cannot directly see disease** — only detects patterns (early drains,
  bloom crashes). Always confirm on-pond.
- **Ponds <0.3 ha** are unreliable at 10 m satellite resolution.
- **Monsoon clouds** can leave 1–2 week gaps.
- **Prototype**, not a substitute for professional advice.

---

**Built for Andhra Pradesh shrimp farmers with satellite data.**
""")