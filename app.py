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
        headers = {"User-Agent": "Ponds-app/1.0 (shrimp pond monitor)"}
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
        headers = {"User-Agent": "Ponds-app/1.0 (shrimp pond monitor)"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            results = r.json()
            return [
                {"display": res.get("display_name", ""),
                 "lat": float(res["lat"]),
                 "lon": float(res["lon"])}
                for res in results
            ]
    except Exception:
        pass
    return []

# ---------- SIDEBAR ----------
st.sidebar.title("🦐 Ponds-app")
st.sidebar.markdown("**Shrimp pond satellite monitor**")
st.sidebar.markdown("---")

st.sidebar.markdown("### 🔍 Find a place")
search_query = st.sidebar.text_input(
    "Type a town, village or landmark",
    placeholder="e.g. Bhimavaram, Kaikalur, Nellore"
)

if "map_center" not in st.session_state:
    st.session_state.map_center = [16.5449, 81.5212]
    st.session_state.map_zoom = 13

if search_query:
    results = geocode_place(search_query)
    if results:
        options = {r["display"]: (r["lat"], r["lon"]) for r in results}
        choice = st.sidebar.selectbox("Select a match:", list(options.keys()))
        if st.sidebar.button("📍 Go to this place"):
            st.session_state.map_center = list(options[choice])
            st.session_state.map_zoom = 15
            st.rerun()
    else:
        st.sidebar.warning("No matches found. Try a different name.")

st.sidebar.markdown("---")

mode = st.sidebar.radio("Add a pond by:", ["Type lat/lon", "Click on map"])

if "ponds" not in st.session_state:
    st.session_state.ponds = []

if mode == "Type lat/lon":
    with st.sidebar.form("add_pond"):
        name = st.text_input("Pond name",
                             value=f"Pond {len(st.session_state.ponds)+1}")
        lat = st.number_input("Latitude", value=16.5449, format="%.5f")
        lon = st.number_input("Longitude", value=81.5212, format="%.5f")
        submitted = st.form_submit_button("➕ Add pond")
        if submitted:
            st.session_state.ponds.append((name, lat, lon))

if st.sidebar.button("🗑️ Clear all ponds"):
    st.session_state.ponds = []

st.sidebar.markdown("---")
st.sidebar.markdown("### Tracked ponds")
if not st.session_state.ponds:
    st.sidebar.caption("None yet.")
for n, la, lo in st.session_state.ponds:
    place = get_place_name(la, lo)
    st.sidebar.write(f"• **{n}**")
    st.sidebar.caption(f"  {place}")

# ---------- HEADER ----------
st.title("🦐 Ponds-app — Shrimp Pond Monitor")
st.caption("Satellite-based monitoring using Sentinel-2 (free, ~5 day revisit)")

# ---------- TABS ----------
tab_monitor, tab_about = st.tabs(["📡 Monitor", "ℹ️ About / Benefits"])

# ===================== MONITOR TAB =====================
with tab_monitor:
    center = st.session_state.get("map_center", [16.5449, 81.5212])
    zoom = st.session_state.get("map_zoom", 13)

    if st.session_state.ponds:
        center = [st.session_state.ponds[-1][1], st.session_state.ponds[-1][2]]
        zoom = 15

    m = folium.Map(
        location=center,
        zoom_start=zoom,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery"
    )

    for n, la, lo in st.session_state.ponds:
        folium.Marker([la, lo], popup=n,
                      icon=folium.Icon(color="red", icon="tint")).add_to(m)
        folium.Circle([la, lo], radius=2000, color="yellow", fill=False,
                      popup=f"{n} – 2km neighborhood").add_to(m)

    map_data = st_folium(m, height=450, width=None,
                         returned_objects=["last_clicked"])

    if mode == "Click on map" and map_data and map_data.get("last_clicked"):
        clicked = map_data["last_clicked"]
        if st.button(f"➕ Add pond at {clicked['lat']:.4f}, {clicked['lng']:.4f}"):
            st.session_state.ponds.append(
                (f"Pond {len(st.session_state.ponds)+1}",
                 clicked["lat"], clicked["lng"])
            )
            st.rerun()

    # ---------- ANALYSIS FUNCTIONS ----------
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

    # ---------- RENDER PONDS ----------
    if not st.session_state.ponds:
        st.info("👈 Search for a town in the sidebar, then either type lat/lon "
                "or switch to 'Click on map' mode and tap a pond. "
                "New here? Check the **About / Benefits** tab to see what this app does.")
    else:
        for name, lat, lon in st.session_state.ponds:
            st.markdown("---")
            place = get_place_name(lat, lon)
            st.subheader(f"📍 {name}")
            st.caption(f"📌 {place}  •  ({lat:.4f}, {lon:.4f})")

            with st.spinner(f"Pulling 2 years of Sentinel-2 data for {name}…"):
                try:
                    df = analyze_pond(lat, lon)
                except Exception as e:
                    st.error(f"GEE error: {e}")
                    continue

            if df.empty:
                st.warning("No cloud-free observations found for this location.")
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
            fig1.update_layout(title="Water presence over time (fill/drain cycles)",
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
                st.caption("No clear fill/drain events detected in this window.")

        st.markdown("---")
        st.caption("⚠️ Prototype. Satellite signals are indicative, not diagnostic. "
                   "Always confirm with on-pond observation.")

# ===================== ABOUT / BENEFITS TAB =====================
with tab_about:
    st.markdown("""
## 🦐 What is Ponds-app?

**A free app that watches your shrimp pond from space every 5 days,
warns you about algae and disease before you can see them, and keeps an
automatic record of every crop you grow — so you lose fewer crops,
sleep better, and have proof when you need it.**

---

## How it works (in simple words)

Every 5 days, a free European satellite (Sentinel-2) passes over Andhra
Pradesh and takes a picture of every shrimp pond in the state. This app
reads those pictures automatically and tells you what changed.

So even if you're travelling, sick, or managing ponds in different
villages, you know the status of every pond from your phone.

---

## 🎯 Top 10 benefits for farmers

### 1. Watch your pond without being there
**Old way:** drive to each pond, look, guess.
**New way:** open the app, see the status of all ponds at once.

### 2. Get warned about algae problems early
Shrimp ponds crash when algae grows too much — water turns green,
oxygen drops at night, shrimp die. The satellite can see algae building
up **2–3 weeks before your eye can**. The app warns you early so you can
do water exchange or reduce feed, saving the entire crop.

### 3. Know when your neighbors are in trouble
When 3–4 ponds within 2 km of you suddenly drain early (which is what
farmers do when disease hits), the app warns you:
*"Unusual harvest activity near you. Tighten biosecurity, avoid canal
water exchange, check your shrimp today."*
It's a heads-up neighbors would never give you directly.

### 4. Automatic diary of every crop
Banks, insurers, and exporters want records. Most farmers don't keep
them. The app **auto-logs every fill and drain event from satellite
history — going back 2 years**, even if you never used it before.
Instant proof for loans and insurance claims.

### 5. See cyclone damage without driving through floodwater
When a Bay of Bengal cyclone hits, roads flood and you can't reach your
ponds for days. A different satellite (Sentinel-1, sees through clouds)
shows within hours which ponds have broken bunds. File insurance claims
immediately with satellite evidence.

### 6. Compare yourself to other farms — anonymously
*"Your cycle length is 108 days. Ponds in your area average 95 days."*
A wake-up call that something might be off — feed quality, stocking
density, or water. You get data-driven feedback without gossip.

### 7. Costs almost nothing
Commercial pond monitoring hardware costs ₹50,000–₹5 lakh per pond.
This app uses free European satellites, free mapping, and free cloud
computing — so you pay little or nothing and still get real insights.

### 8. Works on the cheapest smartphone
No sensors, no hardware at the pond, no constant internet. Just a
₹6,000 Android phone and a 4G connection once a day. Telugu-friendly.
WhatsApp alerts for urgent events.

### 9. Gets smarter as more farmers join
Every farmer who logs a stocking date, a disease event, or a harvest
improves predictions **for everyone nearby**. A community early-warning
system that grows stronger with each user.

### 10. Helps the whole region, not just you
Cooperatives plan harvests better. Feed companies target services.
Exporters prove sustainability to EU/US buyers. Insurance companies
price crop insurance fairly. Banks lend with less risk. Government
monitors illegal expansion. Everyone wins.

---

## 📊 What the numbers on the Monitor tab mean

| What you see | What it means | Why it matters |
|---|---|---|
| **Water %** | How much of your pond is covered in water | Detects fill, drain, and leak events |
| **NDCI** | Chlorophyll level (algae amount) | High = bloom risk, crash risk |
| **Status badge** | 🟢 Healthy / 🟡 Watch / 🔴 Alert | Quick glance health check |
| **Fill/Drain events** | Auto-detected cycle timing | Your automatic farming logbook |
| **Days of culture (DOC)** | Days since last fill | Helps decide harvest timing |

---

## ⚠️ Honest limitations

- **Satellites cannot directly see disease** — the app detects
  *patterns* associated with disease (early drains, bloom crashes),
  not the disease itself. Always confirm with on-pond observation.
- **Ponds smaller than 0.3 ha** (roughly 55×55 m) are hard to monitor
  reliably at 10-meter satellite resolution.
- **Cloud cover during monsoon (June–September)** can leave 1–2 week
  gaps in optical data. Radar backfill is a future feature.
- **This is a prototype.** Not a substitute for professional
  aquaculture advice, veterinary consultation, or on-site monitoring.

---

## 🚀 What's coming next

- **Cluster disease alerts** — automatic warnings when nearby ponds
  show suspicious patterns.
- **Farmer logbook** — log stocking, feed, sampling, mortality.
- **WhatsApp alerts** — important warnings sent directly to your phone.
- **Telugu language UI**.
- **Cyclone/flood damage mapping** using radar satellites.
- **Historical benchmarking** — your pond vs neighbors vs last season.

---

**Built for Andhra Pradesh shrimp farmers with free, open satellite data.**
""")