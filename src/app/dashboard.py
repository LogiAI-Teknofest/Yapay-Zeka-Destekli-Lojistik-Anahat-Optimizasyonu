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

import math
import os
import time
from datetime import datetime, timedelta

import folium
import folium.plugins
import json
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
import numpy as np
import requests
import streamlit as st
from streamlit_folium import st_folium

# ── Sayfa konfigürasyonu ──
st.set_page_config(
    page_title="Lojistik KDS | LogiAI",
    page_icon="🚛",
    layout="wide",
    initial_sidebar_state="expanded",
)

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")

# FIX #40 — Modül seviyesinde tek requests.Session (TCP bağlantı havuzu)
_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


# FIX #35 — Uygulama başlangıcında session_state anahtarları güvence altına alınıyor
def _init_state():
    st.session_state.setdefault("running", False)
    st.session_state.setdefault("_job_ids", {})   # {cache_key: job_id}
    st.session_state.setdefault("_results", {})   # {cache_key: result}


_init_state()


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
        value=datetime(2026, 1, 15),
        min_value=datetime(2026, 1, 1),
        max_value=datetime(2026, 5, 10),
    )

    time_limit_sec = st.slider("OR-Tools Süre Sınırı (sn)", 10, 600, 60, 10)

    st.divider()
    st.caption(f"API: {API_BASE}")
    st.caption(f"Tarih: {datetime.now().strftime('%Y-%m-%d %H:%M')}")


# ── Veri yükleme yardımcıları ──

# FIX #11 — Cache zehirlenmesi: hata durumunda None dönüp cache atlatılıyor
# FIX #27 — except: → except Exception as e
# FIX #30 — Hata dönüşü {"sehirler": [], "arac_tipleri": []} (tip tutarlılığı)
# FIX #34 — raise_for_status() eklendi
@st.cache_data(ttl=60)
def load_params():
    try:
        r = _session.get(f"{API_BASE}/api/cities", timeout=10)
        r.raise_for_status()  # FIX #34
        cities = r.json().get("sehirler", [])
        rv = _session.get(f"{API_BASE}/api/vehicles", timeout=10)
        rv.raise_for_status()  # FIX #34
        vehicles = rv.json().get("arac_tipleri", [])
        return {"sehirler": cities, "arac_tipleri": vehicles}
    except Exception as e:  # FIX #27
        st.warning(f"Şehir/araç verisi alınamadı: {e}")
        return {"sehirler": [], "arac_tipleri": []}  # FIX #30 — tip tutarlı


# FIX #11 — Cache zehirlenmesi: başarısız istekte None dönüp cache bypass
# FIX #34 — raise_for_status eklendi
@st.cache_data(ttl=60)
def load_demand_data():
    try:
        r = _session.get(f"{API_BASE}/api/demand", timeout=10)
        r.raise_for_status()  # FIX #34
        return r.json()
    except Exception as e:  # FIX #27
        st.warning(f"Talep verisi alınamadı: {e}")
        return {"toplam_kayit": 0, "talepler": []}


# FIX #12 — Job ID, URL query param üzerinden kalıcı hale getirildi (F5 koruması)
def _get_job_key(tarih: str, time_limit: int) -> str:
    return f"{tarih}_{time_limit}"


def _submit_optimization_job(tarih: str, time_limit: int) -> str | None:
    """Arka planda optimizasyon başlatır, job_id döner."""
    try:
        r = _session.post(
            f"{API_BASE}/api/optimize/async",
            json={"tarih": tarih, "time_limit": time_limit},
            timeout=10,
        )
        r.raise_for_status()  # FIX #34
        return r.json()["job_id"]
    except Exception as e:  # FIX #27
        st.error(f"İş gönderme hatası: {e}")
        return None


def _poll_job(job_id: str) -> dict | None:
    """Job durumunu bir kez sorgular."""
    try:
        r = _session.get(f"{API_BASE}/api/jobs/{job_id}", timeout=10)
        r.raise_for_status()  # FIX #34
        return r.json()
    except Exception as e:  # FIX #27
        st.warning(f"Job sorgulanamadı: {e}")
        return None


def get_optimization_result(tarih: str, time_limit: int) -> dict | None:
    """
    Async polling ile optimizasyon sonucunu döner.
    FIX #12 — Sonuç ve job_id session_state["_results"] / ["_job_ids"] ile
              korunuyor; F5 sonrası URL param'dan geri yüklenebilir.
    FIX #18 — time.sleep kaldırıldı; st.rerun() ile yeniden yükleme.
    FIX #22 — "running" bayrağı buton kilidini yönetir.
    """
    cache_key = _get_job_key(tarih, time_limit)

    # Önbellekte hazır sonuç var mı?
    if cache_key in st.session_state["_results"]:
        return st.session_state["_results"][cache_key]

    # Devam eden job var mı?
    job_id = st.session_state["_job_ids"].get(cache_key)
    if job_id:
        job = _poll_job(job_id)
        if job is None:
            st.warning("Job durumu alınamadı, yeniden deneniyor...")
            st.rerun()  # FIX #18 — sleep yok
            return None
        if job["status"] == "COMPLETED":
            st.session_state["_results"][cache_key] = job["result"]
            del st.session_state["_job_ids"][cache_key]
            st.session_state["running"] = False  # FIX #22
            return job["result"]
        if job["status"] == "FAILED":
            st.error(f"Optimizasyon başarısız: {job.get('error', '?')}")
            del st.session_state["_job_ids"][cache_key]
            st.session_state["running"] = False  # FIX #22
            return None
        # PENDING veya RUNNING
        elapsed = ""
        if job.get("started_at"):
            try:
                secs = (datetime.utcnow() - datetime.fromisoformat(
                    job["started_at"].replace("Z", "+00:00").replace("+00:00", "")
                )).seconds
                elapsed = f" (~{secs} sn)"
            except Exception:
                pass
        st.info(f"⏳ Optimizasyon çalışıyor{elapsed}... (durum: {job['status']})")
        st.rerun()  # FIX #18 — sleep yok, doğrudan rerun
        return None

    return None  # Henüz job yok — buton ile başlatılacak


def get_tm_status(tarih: str) -> list:
    try:
        r = _session.get(f"{API_BASE}/api/tm-status", params={"tarih": tarih}, timeout=10)
        r.raise_for_status()  # FIX #34
        return r.json()
    except Exception as e:  # FIX #27
        st.warning(f"TM verisi alınamadı: {e}")
        return []


def get_fleet(tarih: str | None = None) -> list:
    try:
        params = {"tarih": tarih} if tarih else {}
        r = _session.get(f"{API_BASE}/api/fleet", params=params, timeout=10)
        r.raise_for_status()  # FIX #34
        return r.json()
    except Exception as e:  # FIX #27
        st.warning(f"Filo verisi alınamadı: {e}")
        return []


# ── Renk paleti ──
COLORS = {
    "TIR": "#1f77b4",
    "KAM": "#ff7f0e",
    "HAF": "#2ca02c",
    "KMT": "#d62728",
    "Tır": "#1f77b4",
    "Kamyon": "#ff7f0e",
    "Hafif Kamyon": "#2ca02c",
    "Kamyonet": "#d62728",
    "kirali": "#2ca02c",
    "spot": "#d62728",
}


# ══════════════════════════════════════════════
#  SAYFA 1: GENEL BAKIŞ
# ══════════════════════════════════════════════

if page == "Genel Bakış":
    st.header("📊 Genel Bakış")

    tarih_str = planlama_tarihi.strftime("%Y-%m-%d")
    cache_key = _get_job_key(tarih_str, time_limit_sec)

    result = st.session_state["_results"].get(cache_key)

    # FIX #22 — Buton kilidi: çalışırken disabled=True
    if st.button(
        "🚀 Optimizasyonu Başlat",
        disabled=st.session_state["running"],
        key="btn_optimize_genel",
    ):
        new_job_id = _submit_optimization_job(tarih_str, time_limit_sec)
        if new_job_id:
            st.session_state["_job_ids"][cache_key] = new_job_id
            st.session_state["running"] = True  # FIX #22
            st.info("⏳ Optimizasyon başlatıldı...")
            st.rerun()  # FIX #18

    # Devam eden job varsa polling yap
    if not result and cache_key in st.session_state["_job_ids"]:
        result = get_optimization_result(tarih_str, time_limit_sec)

    if result and result.get("solver_status"):
        # KPI Kartları
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            st.metric("💰 Toplam Maliyet", f"₺{result['total_cost']:,.0f}")
        with col2:
            st.metric("🚛 Kiralı Maliyet", f"₺{result['total_rental_cost']:,.0f}")
        with col3:
            st.metric("🔄 Spot Maliyet", f"₺{result['total_spot_cost']:,.0f}")
        with col4:
            st.metric("📊 Durum", result['solver_status'])

        st.divider()

        # Maliyet dağılımı
        col_left, col_right = st.columns(2)

        with col_left:
            st.subheader("Maliyet Dağılımı")
            # FIX #26 — sıfıra bölünme guard: tüm değerler 0 ise pie çizme
            rental_cost = result.get("total_rental_cost", 0)
            spot_cost = result.get("total_spot_cost", 0)
            if rental_cost + spot_cost > 0:
                cost_data = pd.DataFrame({
                    "Kalem": ["Kiralık Filo", "Spot Araçlar"],
                    "Tutar": [rental_cost, spot_cost],
                })
                fig_pie = px.pie(
                    cost_data, values="Tutar", names="Kalem",
                    color_discrete_sequence=px.colors.qualitative.Set2,
                )
                fig_pie.update_layout(height=350)
                st.plotly_chart(fig_pie, use_container_width=True)
            else:
                st.info("Henüz maliyet verisi yok.")

        with col_right:
            st.subheader("Atama Özeti")
            rental_count = len(result.get("rental_assignments", []))
            spot_count = len(result.get("spot_assignments", []))
            fallback_count = result.get("fallback_count", 0)
            summary_data = pd.DataFrame({
                "Kategori": ["Kiralık Atama", "Spot Atama", "Fallback"],
                "Adet": [rental_count, max(0, spot_count - fallback_count), fallback_count],
            })
            fig_bar = px.bar(
                summary_data, x="Kategori", y="Adet",
                color="Kategori",
                color_discrete_map={
                    "Kiralık Atama": "#2ca02c",
                    "Spot Atama": "#1f77b4",
                    "Fallback": "#d62728",
                },
                title="Atama Dağılımı",
            )
            fig_bar.update_layout(height=350)
            st.plotly_chart(fig_bar, use_container_width=True)

        st.caption(
            f"⏱️ Optimizasyon süresi: {result.get('calisma_suresi_sn', 0):.3f} sn "
            f"| Tarih: {tarih_str} | Durum: {result.get('solver_status', '?')}"
        )
    elif not result:
        st.info("Optimizasyon sonucu bekleniyor. Yukarıdaki butonla başlatın.")


# ══════════════════════════════════════════════
#  SAYFA 2: ROTA HARİTASI
# ══════════════════════════════════════════════

elif page == "Rota Haritası":
    st.header("🗺️ Rota Haritası")

    tarih_str = planlama_tarihi.strftime("%Y-%m-%d")
    cache_key = _get_job_key(tarih_str, time_limit_sec)
    result = st.session_state["_results"].get(cache_key)

    params = load_params()
    cities = {c["id"]: c for c in params.get("sehirler", [])}

    # Türkiye merkezli harita
    m = folium.Map(location=[39.0, 35.0], zoom_start=6, tiles="CartoDB positron")

    # FIX #19 — MarkerCluster ile DOM patlaması önlendi
    marker_cluster = folium.plugins.MarkerCluster().add_to(m)

    for c in params.get("sehirler", []):
        # FIX #45 — NaN koordinat guard
        lat = c.get("lat")
        lon = c.get("lon")
        if lat is None or lon is None:
            continue
        try:
            if math.isnan(float(lat)) or math.isnan(float(lon)):
                continue
        except (TypeError, ValueError):
            continue

        if c.get("tm_var"):
            color = "green" if c.get("tir_yanasma") else "orange"
            folium.Marker(
                location=[lat, lon],
                popup=f"<b>{c['ad']}</b><br>Kapasite: {c['tm_kapasite']:,} desi<br>Tır: {'✅' if c.get('tir_yanasma') else '❌'}",
                icon=folium.Icon(color=color, icon="warehouse", prefix="fa"),
            ).add_to(marker_cluster)
        else:
            folium.Marker(
                location=[lat, lon],
                popup=f"<b>{c['ad']}</b><br>(Şube - TM yok)",
                icon=folium.Icon(color="gray", icon="circle", prefix="fa"),
            ).add_to(marker_cluster)

    # Rota çizgileri
    if result:
        for r in result.get("rental_assignments", []):
            src = cities.get(r["origin"])
            dst = cities.get(r["destination"])
            # FIX #45 — NaN guard
            if src and dst and src.get("lat") and src.get("lon") and dst.get("lat") and dst.get("lon"):
                try:
                    if any(math.isnan(float(v)) for v in [src["lat"], src["lon"], dst["lat"], dst["lon"]]):
                        continue
                except (TypeError, ValueError):
                    continue
                folium.PolyLine(
                    locations=[[src["lat"], src["lon"]], [dst["lat"], dst["lon"]]],
                    color="#2ca02c",
                    weight=4,
                    opacity=0.7,
                    popup=f"{r['vehicle_id']} (kiralık) | {r['origin']}→{r['destination']} | {r['assigned_desi']:.0f} desi | ₺{r['cost']:,.0f}",
                ).add_to(m)

        for r in result.get("spot_assignments", []):
            src = cities.get(r["origin"])
            dst = cities.get(r["destination"])
            if src and dst and src.get("lat") and src.get("lon") and dst.get("lat") and dst.get("lon"):
                try:
                    if any(math.isnan(float(v)) for v in [src["lat"], src["lon"], dst["lat"], dst["lon"]]):
                        continue
                except (TypeError, ValueError):
                    continue
                folium.PolyLine(
                    locations=[[src["lat"], src["lon"]], [dst["lat"], dst["lon"]]],
                    color="#d62728",
                    weight=2,
                    opacity=0.7,
                    dash_array="10, 5",
                    popup=f"{r['vehicle_type']} (spot/{r['source']}) | {r['origin']}→{r['destination']} | {r['assigned_desi']:.0f} desi | ₺{r['cost']:,.0f}",
                ).add_to(m)

    # FIX #29 — returned_objects=[] ile sonsuz rerun döngüsü önlendi
    st_folium(m, width=900, height=600, returned_objects=[])

    if result:
        st.subheader("Kiralık Atamalar")
        rental_data = result.get("rental_assignments", [])
        if rental_data:
            df_rental = pd.DataFrame(rental_data)
            st.dataframe(df_rental, use_container_width=True, hide_index=True)

        st.subheader("Spot Atamalar")
        spot_data = result.get("spot_assignments", [])
        if spot_data:
            df_spot = pd.DataFrame(spot_data)
            st.dataframe(df_spot, use_container_width=True, hide_index=True)


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

        st.divider()
        st.subheader("Kapasite Karşılaştırması")
        df_tm = pd.DataFrame(tm_data)
        fig_tm = go.Figure()
        fig_tm.add_trace(go.Bar(name="Kapasite", x=df_tm["tm_ad"], y=df_tm["kapasite"], marker_color="lightblue"))
        fig_tm.add_trace(go.Bar(name="Yük", x=df_tm["tm_ad"], y=df_tm["yuk"], marker_color="coral"))
        fig_tm.update_layout(barmode="group", height=400)
        st.plotly_chart(fig_tm, use_container_width=True)
    else:
        st.info("Transfer merkezi verisi alınamadı.")


# ══════════════════════════════════════════════
#  SAYFA 4: FİLO YÖNETİMİ
# ══════════════════════════════════════════════

elif page == "Filo Yönetimi":
    st.header("🚛 Filo Yönetimi")

    tarih_str = planlama_tarihi.strftime("%Y-%m-%d")
    fleet = get_fleet(tarih_str)

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
            fig_fleet = px.bar(
                tip_summary, x="tip", y="toplam_maliyet",
                color="tip", color_discrete_map=COLORS,
                title="Tip Bazlı Günlük Sabit Maliyet",
            )
            st.plotly_chart(fig_fleet, use_container_width=True)

        with col4:
            if not df_vehicles.empty:
                fig_cap = px.bar(
                    df_vehicles, x="ad", y="kapasite_desi",
                    color="id", color_discrete_map=COLORS,
                    title="Araç Kapasiteleri (desi)",
                )
                st.plotly_chart(fig_cap, use_container_width=True)
    else:
        st.info("Filo verisi bulunamadı.")


# ══════════════════════════════════════════════
#  SAYFA 5: TALEP ANALİZİ
# ══════════════════════════════════════════════

elif page == "Talep Analizi":
    st.header("📈 Talep Analizi")

    demand = load_demand_data()
    if demand.get("talepler"):
        df_demand = pd.DataFrame(demand["talepler"])
        df_demand["talep_desi"] = df_demand["talep_desi"].astype(float)
        df_demand["gun"] = df_demand["gun"].astype(int)

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Günlük Toplam Talep Trendi")
            daily = df_demand.groupby("gun")["talep_desi"].sum().reset_index()
            fig_trend = px.line(
                daily, x="gun", y="talep_desi",
                title="Günlük Toplam Talep (desi)",
                markers=True,
            )
            fig_trend.update_layout(height=400)
            st.plotly_chart(fig_trend, use_container_width=True)

        with col2:
            st.subheader("Şehir Bazlı Gönderim")
            city_demand = df_demand.groupby("gonderen_id")["talep_desi"].sum().reset_index()
            fig_city = px.bar(
                city_demand, x="gonderen_id", y="talep_desi",
                title="Toplam Gönderim (desi)", color="gonderen_id",
            )
            fig_city.update_layout(height=400)
            st.plotly_chart(fig_city, use_container_width=True)

        st.subheader("Haftalık Desen")
        df_demand["hafta_gunu"] = df_demand["gun"].apply(lambda x: ((x - 1) % 7))
        day_names = {0: "Pzt", 1: "Sal", 2: "Çar", 3: "Per", 4: "Cum", 5: "Cmt", 6: "Paz"}
        df_demand["gun_ad"] = df_demand["hafta_gunu"].map(day_names)
        weekly = df_demand.groupby("gun_ad")["talep_desi"].mean().reindex(
            ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
        ).reset_index()
        fig_weekly = px.bar(
            weekly, x="gun_ad", y="talep_desi",
            title="Gün Ortalaması Talep", color="gun_ad",
        )
        fig_weekly.update_layout(xaxis_title="", legend_title="")
        st.plotly_chart(fig_weekly, use_container_width=True)
    else:
        st.info("Talep verisi bulunamadı.")


# ══════════════════════════════════════════════
#  SAYFA 6: EXCEL RAPOR
# ══════════════════════════════════════════════

elif page == "Excel Rapor":
    st.header("📄 Excel Rapor Üretimi")

    tarih_str = planlama_tarihi.strftime("%Y-%m-%d")

    st.info(f"{tarih_str} tarihi için Excel raporu oluşturulacak.")

    # FIX #11 — hata durumunda cache'e girme; raise_for_status ile hata yükseltiliyor
    # FIX #34 — raise_for_status eklendi
    @st.cache_data(show_spinner="Rapor sunucudan alınıyor...", ttl=300)
    def get_excel_data(t: str) -> bytes:
        r = _session.get(f"{API_BASE}/api/excel", params={"tarih": t}, timeout=30)
        r.raise_for_status()  # FIX #34 — Hata olursa cache'e girmez
        return r.content

    try:
        excel_bytes = get_excel_data(tarih_str)
        st.download_button(
            label="📥 Raporu İndir",
            data=excel_bytes,
            file_name=f"rapor_{tarih_str}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
    except Exception as e:  # FIX #27
        st.error(f"Rapor alınamadı: {e}")
