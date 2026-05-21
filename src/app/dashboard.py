"""
Streamlit + Folium Karar Destek Sistemi (KDS) Dashboard
Person D: Sistem Mimarı ve Arayüz Geliştiricisi

Sayfalar:
1. Genel Bakış — KPI kartları + maliyet pasta
2. Rota Haritası — Folium interaktif harita
3. Transfer Merkezleri — TM kapasite izleme
4. Filo Yönetimi — Kiralık/spot araç durumu
5. Talep Analizi — Zaman serisi grafikleri
6. Excel Rapor — Çıktı üretim paneli
"""

import streamlit as st
import pandas as pd
import numpy as np
import folium
from streamlit_folium import st_folium
import plotly.express as px
import plotly.graph_objects as go
import requests
import json,os
from datetime import datetime, timedelta

# ── Sayfa konfigürasyonu ──
st.set_page_config(
    page_title="Lojistik KDS | LogiAI",
    page_icon="🚛",
    layout="wide",
    initial_sidebar_state="expanded",
)

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")


# ── Sidebar ──
with st.sidebar:
    st.image("https://img.icons8.com/fluency/96/truck.png", width=80)
    st.title("Lojistik KDS")
    st.caption("Teknofest LogiAI — Karar Destek Sistemi")
    
    st.divider()
    
    page = st.radio(
        "📋 Sayfa",
        ["Genel Bakış", "Rota Haritası", "Transfer Merkezleri", "Filo Yönetimi", "Talep Analizi", "Excel Rapor"],
        label_visibility="collapsed",
    )
    
    st.divider()
    
    st.subheader("⚙️ Parametreler")
    planlama_tarihi = st.date_input(
        "Planlama Tarihi",
        value=datetime(2025, 1, 15),
        min_value=datetime(2025, 1, 1),
        max_value=datetime(2025, 1, 31),
    )
    
    sla_katsayi = st.slider("SLA Ceza Katsayısı", 0.0, 50.0, 15.0, 1.0)
    ellemcele_katsayi = st.slider("TM Elleçleme Ceza Katsayısı", 0.0, 30.0, 8.0, 1.0)
    spot_limit = st.number_input("Maks Spot Araç", min_value=0, max_value=100, value=20)
    filo_kullanim = st.slider("Filo Kullanım Oranı", 0.0, 1.0, 0.7, 0.05)
    
    st.divider()
    st.caption(f"API: {API_BASE}")
    st.caption(f"Tarih: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

# ── Veri yükleme yardımcıları ──

@st.cache_data(ttl=60)
def load_params():
    try:
        r = requests.get(f"{API_BASE}/api/cities", timeout=5)
        cities = r.json().get("sehirler", [])
        rv = requests.get(f"{API_BASE}/api/vehicles", timeout=5)
        vehicles = rv.json().get("arac_tipleri", [])
        return {"sehirler": cities, "arac_tipleri": vehicles}
    except:
        return {"sehirler": [], "arac_tipleri": []}

@st.cache_data(ttl=60)
def load_demand_data():
    try:
        r = requests.get(f"{API_BASE}/api/demand", timeout=10)
        return r.json()
    except:
        return {"toplam_kayit": 0, "talepler": []}

@st.cache_data(ttl=30)
def run_optimization(tarih, sla, ellemcele, spot, filo):
    try:
        r = requests.post(
            f"{API_BASE}/api/optimize",
            json={
                "tarih": tarih,
                "hedef_filo_kullanim": filo,
                "spot_limit": spot,
                "sla_katsayi": sla,
                "ellemcele_katsayi": ellemcele,
            },
            timeout=120,
        )
        return r.json()
    except Exception as e:
        st.error(f"Optimizasyon hatası: {e}")
        return None

def get_tm_status(tarih):
    try:
        r = requests.get(f"{API_BASE}/api/tm-status", params={"tarih": tarih}, timeout=10)
        return r.json()
    except:
        return []

def get_fleet():
    try:
        r = requests.get(f"{API_BASE}/api/fleet", timeout=10)
        return r.json()
    except:
        return []

# ── Renk paleti ──
COLORS = {
    "TIR": "#1f77b4",
    "KAM": "#ff7f0e", 
    "HAF": "#2ca02c",
    "KMT": "#d62728",
    "kirali": "#2ca02c",
    "spot": "#d62728",
}

ARAC_EMOJI = {"TIR": "🚛", "KAM": "🚚", "HAF": "🛻", "KMT": "🚐"}

# ══════════════════════════════════════════════
#  SAYFA 1: GENEL BAKIŞ
# ══════════════════════════════════════════════

if page == "Genel Bakış":
    st.header("📊 Genel Bakış")
    
    tarih_str = planlama_tarihi.strftime("%Y-%m-%d")
    
    with st.spinner("Optimizasyon çalıştırılıyor..."):
        result = run_optimization(tarih_str, sla_katsayi, ellemcele_katsayi, spot_limit, filo_kullanim)
    
    if result and result.get("status") == "completed":
        # KPI Kartları
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("💰 Toplam Maliyet", f"₺{result['toplam_maliyet']:,.0f}")
        with col2:
            st.metric("🚛 Kiralı Maliyet", f"₺{result['kirali_maliyet']:,.0f}")
        with col3:
            st.metric("🔄 Spot Maliyet", f"₺{result['spot_maliyet']:,.0f}")
        with col4:
            st.metric("⚠️ Ceza Maliyet", f"₺{result['ceza_maliyet']:,.0f}")
        
        st.divider()
        
        # Maliyet dağılımı
        col_left, col_right = st.columns(2)
        
        with col_left:
            st.subheader("Maliyet Dağılımı")
            cost_data = pd.DataFrame({
                "Kalem": ["Kiralık Filo", "Spot Araçlar", "SLA Cezası", "TM Aşım Cezası"],
                "Tutar": [result["kirali_maliyet"], result["spot_maliyet"],
                          result["ceza_maliyet"] * 0.6, result["ceza_maliyet"] * 0.4],
            })
            fig_pie = px.pie(cost_data, values="Tutar", names="Kalem",
                           color_discrete_sequence=px.colors.qualitative.Set2)
            fig_pie.update_layout(height=350)
            st.plotly_chart(fig_pie, use_container_width=True)
        
        with col_right:
            st.subheader("Rota Özeti")
            rotalar_df = pd.DataFrame(result["rotalar"])
            if not rotalar_df.empty:
                tip_count = rotalar_df.groupby(["tip", "arac_tipi"]).size().reset_index(name="sayi")
                tip_count["label"] = tip_count["arac_tipi"].map(ARAC_EMOJI) + " " + tip_count["arac_tipi"]
                fig_bar = px.bar(tip_count, x="label", y="sayi", color="tip",
                               color_discrete_map={"kirali": "#2ca02c", "spot": "#d62728"},
                               title="Araç Tipi Dağılımı")
                fig_bar.update_layout(height=350)
                st.plotly_chart(fig_bar, use_container_width=True)
        
        # Çalışma süresi
        st.caption(f"⏱️ Optimizasyon süresi: {result['calisma_suresi_sn']:.3f} sn | Tarih: {tarih_str}")

# ══════════════════════════════════════════════
#  SAYFA 2: ROTA HARİTASI
# ══════════════════════════════════════════════

elif page == "Rota Haritası":
    st.header("🗺️ Rota Haritası")
    
    tarih_str = planlama_tarihi.strftime("%Y-%m-%d")
    result = run_optimization(tarih_str, sla_katsayi, ellemcele_katsayi, spot_limit, filo_kullanim)
    
    params = load_params()
    cities = {c["id"]: c for c in params.get("sehirler", [])}
    
    # Türkiye merkezli harita
    m = folium.Map(location=[39.0, 35.0], zoom_start=6, tiles="CartoDB positron")
    
    # TM noktaları
    for c in params.get("sehirler", []):
        if c.get("tm_var"):
            color = "green" if c.get("tir_yanasma") else "orange"
            folium.Marker(
                location=[c["lat"], c["lon"]],
                popup=f"<b>{c['ad']}</b><br>Kapasite: {c['tm_kapasite']:,} desi<br>Tır: {'✅' if c.get('tir_yanasma') else '❌'}",
                icon=folium.Icon(color=color, icon="warehouse", prefix="fa"),
            ).add_to(m)
        else:
            folium.Marker(
                location=[c["lat"], c["lon"]],
                popup=f"<b>{c['ad']}</b><br>(Şube - TM yok)",
                icon=folium.Icon(color="gray", icon="circle", prefix="fa"),
            ).add_to(m)
    
    # Rota çizgileri
    if result and result.get("rotalar"):
        for r in result["rotalar"]:
            src = cities.get(r["kaynak"])
            dst = cities.get(r["hedef"])
            if src and dst:
                color = COLORS.get(r["arac_tipi"], "#333")
                weight = 4 if r["tip"] == "kirali" else 2
                dash = "solid" if r["tip"] == "kirali" else "10, 5"
                
                folium.PolyLine(
                    locations=[[src["lat"], src["lon"]], [dst["lat"], dst["lon"]]],
                    color=color,
                    weight=weight,
                    opacity=0.7,
                    dash_array=dash if r["tip"] == "spot" else None,
                    popup=f"{r['arac_id']} ({r['tip']}) | {r['kaynak']}→{r['hedef']} | {r['yuk_desi']} desi | ₺{r['maliyet']:,.0f}",
                ).add_to(m)
    
    st_data = st_folium(m, width=900, height=600)
    
    # Rota tablosu
    if result and result.get("rotalar"):
        st.subheader("Rota Detayları")
        df_routes = pd.DataFrame(result["rotalar"])
        display_cols = ["arac_id", "tip", "arac_tipi", "kaynak", "hedef", "yuk_desi", "mesafe_km", "sure_saat", "maliyet"]
        st.dataframe(df_routes[[c for c in display_cols if c in df_routes.columns]],
                    use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════
#  SAYFA 3: TRANSFER MERKEZLERİ
# ══════════════════════════════════════════════

elif page == "Transfer Merkezleri":
    st.header("🏭 Transfer Merkezi İzleme")
    
    tarih_str = planlama_tarihi.strftime("%Y-%m-%d")
    tm_data = get_tm_status(tarih_str)
    
    if tm_data:
        cols = st.columns(min(len(tm_data), 3))
        for i, tm in enumerate(tm_data):
            with cols[i % 3]:
                usage_pct = (tm["yuk"] / tm["kapasite"] * 100) if tm["kapasite"] > 0 else 0
                color = "🟢" if usage_pct < 70 else "🟡" if usage_pct < 90 else "🔴"
                st.subheader(f"{color} {tm['tm_ad']}")
                st.metric("Kapasite", f"{tm['kapasite']:,} desi")
                st.metric("Güncel Yük", f"{tm['yuk']:,} desi")
                st.progress(min(usage_pct / 100, 1.0))
                st.caption(f"Doluluk: %{usage_pct:.1f}")
                if tm["asim"] > 0:
                    st.error(f"⚠️ Aşım: {tm['asim']:,} desi | Ceza: ₺{tm['asim_maliyet']:,.0f}")
        
        # Kapasite bar chart
        st.divider()
        st.subheader("Kapasite Karşılaştırması")
        df_tm = pd.DataFrame(tm_data)
        fig_tm = go.Figure()
        fig_tm.add_trace(go.Bar(name="Kapasite", x=df_tm["tm_ad"], y=df_tm["kapasite"], marker_color="lightblue"))
        fig_tm.add_trace(go.Bar(name="Yük", x=df_tm["tm_ad"], y=df_tm["yuk"], marker_color="coral"))
        fig_tm.update_layout(barmode="group", height=400)
        st.plotly_chart(fig_tm, use_container_width=True)

# ══════════════════════════════════════════════
#  SAYFA 4: FİLO YÖNETİMİ
# ══════════════════════════════════════════════

elif page == "Filo Yönetimi":
    st.header("🚛 Filo Yönetimi")
    
    fleet = get_fleet()
    
    if fleet:
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Kiralık Filo")
            df_fleet = pd.DataFrame(fleet)
            st.dataframe(df_fleet, use_container_width=True, hide_index=True)
        
        with col2:
            st.subheader("Araç Tipi Bilgisi")
            params = load_params()
            vehicles = params.get("arac_tipleri", [])
            df_vehicles = pd.DataFrame(vehicles)
            if not df_vehicles.empty:
                st.dataframe(df_vehicles, use_container_width=True, hide_index=True)
                
        st.divider()
        
        col3, col4 = st.columns(2)
        with col3:
            tip_summary = df_fleet.groupby("tip").agg(
                sayi=("arac_id", "count"),
                toplam_maliyet=("sabit_gunluk", "sum"),
            ).reset_index()
            fig_fleet = px.bar(tip_summary, x="tip", y="toplam_maliyet",
                             color="tip", color_discrete_map=COLORS,
                             title="Tip Bazlı Günlük Sabit Maliyet")
            st.plotly_chart(fig_fleet, use_container_width=True)
            
        with col4:
            if not df_vehicles.empty:
                fig_cap = px.bar(df_vehicles, x="ad", y="kapasite_desi",
                               color="id", color_discrete_map=COLORS,
                               title="Araç Kapasiteleri (desi)")
                st.plotly_chart(fig_cap, use_container_width=True)

# ══════════════════════════════════════════════
#  SAYFA 5: TALEP ANALİZİ
# ══════════════════════════════════════════════

elif page == "Talep Analizi":
    st.header("📈 Talep Analizi")
    
    demand = load_demand_data()
    if demand.get("talepler"):
        df_demand = pd.DataFrame(demand["talepler"])
        df_demand["talep_desi"] = df_demand["talep_desi"].astype(int)
        df_demand["gun"] = df_demand["gun"].astype(int)
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Günlük Toplam Talep Trendi")
            daily = df_demand.groupby("gun")["talep_desi"].sum().reset_index()
            fig_trend = px.line(daily, x="gun", y="talep_desi",
                              title="Günlük Toplam Talep (desi)",
                              markers=True)
            fig_trend.update_layout(height=400)
            st.plotly_chart(fig_trend, use_container_width=True)
        
        with col2:
            st.subheader("Şehir Bazlı Gönderim")
            city_demand = df_demand.groupby("gonderen_id")["talep_desi"].sum().reset_index()
            fig_city = px.bar(city_demand, x="gonderen_id", y="talep_desi",
                            title="Toplam Gönderim (desi)", color="gonderen_id")
            fig_city.update_layout(height=400)
            st.plotly_chart(fig_city, use_container_width=True)
        
        # Haftanın günü etkisi
        st.subheader("Haftalık Desen")
        df_demand["hafta_gunu"] = df_demand["gun"].apply(lambda x: ((x - 1) % 7))
        day_names = {0: "Pzt", 1: "Sal", 2: "Çar", 3: "Per", 4: "Cum", 5: "Cmt", 6: "Paz"}
        df_demand["gun_ad"] = df_demand["hafta_gunu"].map(day_names)
        weekly = df_demand.groupby("gun_ad")["talep_desi"].mean().reindex(
            ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
        ).reset_index()
        fig_weekly = px.bar(weekly, x="gun_ad", y="talep_desi",
                          title="Gün Ortalaması Talep", color="gun_ad")
        st.plotly_chart(fig_weekly, use_container_width=True)

# ══════════════════════════════════════════════
#  SAYFA 6: EXCEL RAPOR
# ══════════════════════════════════════════════

elif page == "Excel Rapor":
    st.header("📄 Excel Rapor Üretimi")
    
    tarih_str = planlama_tarihi.strftime("%Y-%m-%d")
    
    st.info(f"{tarih_str} tarihi için Excel raporu oluşturulacak.")
    
    if st.button("📥 Rapor Oluştur", type="primary"):
        with st.spinner("Rapor oluşturuluyor..."):
            try:
                r = requests.post(f"{API_BASE}/api/excel", params={"tarih": tarih_str}, timeout=30)
                if r.status_code == 200:
                    data = r.json()
                    st.success(f"✅ Rapor oluşturuldu: {data['dosya']}")
                    st.json(data)
                else:
                    st.error(f"❌ Hata: {r.text}")
            except Exception as e:
                st.error(f"Bağlantı hatası: {e}")
    
    # Mevcut raporlar
    st.divider()
    st.subheader("📁 Üretilmiş Raporlar")
    st.caption("Raporlar `output/` klasöründe saklanır.")
