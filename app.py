"""PXC-LeadHunter Streamlit paneli.

Phoenix Contact satış ve bayi ekipleri için tarayıcı üzerinden
pazar istihbaratı ve müşteri adayı tarama aracı.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime

import pandas as pd
import streamlit as st

from analyzer import AnalysisReport, FatalAnalyzerError
from config import COMPANY_NAME, COMPANY_SHORT_NAME, ConfigError, get_settings
from main import compose_search_query, run_intelligence
from scraper import ScraperError

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

st.set_page_config(
    page_title=f"{COMPANY_SHORT_NAME}-LeadHunter",
    page_icon=":material/precision_manufacturing:",
    layout="wide",
    initial_sidebar_state="expanded",
)

EXAMPLE_QUERIES = [
    "Gıda fabrikası otomasyon yatırımı Türkiye",
    "Panel imalatı klemens tedarik",
    "EV şarj istasyonu CHARX projesi",
    "Weidmüller WAGO rakip haberleri",
]

FOCUS_OPTIONS = {
    "Tümü": "all",
    "Müşteri adayları": "leads",
    "Pazar istihbaratı": "market",
    "Rakip faaliyetleri": "competitors",
}

TIME_OPTIONS = {
    "Tüm zamanlar": None,
    "Son 1 hafta": "qdr:w",
    "Son 1 ay": "qdr:m",
    "Son 1 yıl": "qdr:y",
}

DISPLAY_COLUMNS = {
    "sirket": "Şirket",
    "puan": "Fırsat skoru",
    "sektor": "Sektör",
    "lokasyon": "Lokasyon",
    "urun_eslesmeleri": "PXC ürün eşleşmeleri",
    "rakipler": "Rakip faaliyetleri",
    "satin_alma_sinyalleri": "Satın alma sinyalleri",
    "ozet": "Özet",
    "sonraki_adim": "Sonraki adım",
    "iletisim": "İletişim ipuçları",
    "kaynak": "Kaynak",
    "baslik": "Başlık",
    "hata": "Hata",
}


def init_state() -> None:
    st.session_state.setdefault("report", None)
    st.session_state.setdefault("frame", pd.DataFrame())
    st.session_state.setdefault("query_used", "")
    st.session_state.setdefault("search_query", "")
    st.session_state.setdefault("last_error", "")


def apply_example() -> None:
    picked = st.session_state.get("example_pill")
    if picked:
        st.session_state.search_query = picked


def config_ready() -> bool:
    try:
        get_settings()
        return True
    except ConfigError:
        return False


def filter_frame(
    frame: pd.DataFrame,
    *,
    min_score: int,
    sectors: list[str],
    competitors_only: bool,
    products_only: bool,
) -> pd.DataFrame:
    if frame.empty:
        return frame

    filtered = frame.copy()
    if "puan" in filtered.columns:
        filtered = filtered[filtered["puan"].fillna(0) >= min_score]
    if sectors and "sektor" in filtered.columns:
        filtered = filtered[filtered["sektor"].isin(sectors)]
    if competitors_only and "rakipler" in filtered.columns:
        filtered = filtered[filtered["rakipler"].fillna("").astype(str).str.len() > 0]
    if products_only and "urun_eslesmeleri" in filtered.columns:
        filtered = filtered[filtered["urun_eslesmeleri"].fillna("").astype(str).str.len() > 0]
    return filtered.reset_index(drop=True)


def to_csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def to_excel_bytes(frame: pd.DataFrame, report: AnalysisReport) -> bytes:
    buffer = io.BytesIO()
    summary = pd.DataFrame(
        {
            "sorgu": [report.query],
            "tarih": [datetime.now().strftime("%Y-%m-%d %H:%M")],
            "aday_sayisi": [len(frame)],
            "yonetici_ozeti": [report.executive_summary],
        }
    )
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Adaylar")
        summary.to_excel(writer, index=False, sheet_name="Ozet")
    return buffer.getvalue()


def column_config_for(frame: pd.DataFrame) -> dict:
    config: dict = {}
    if "sirket" in frame.columns:
        config["sirket"] = st.column_config.TextColumn(DISPLAY_COLUMNS["sirket"])
    if "puan" in frame.columns:
        config["puan"] = st.column_config.ProgressColumn(
            DISPLAY_COLUMNS["puan"],
            min_value=0,
            max_value=100,
            format="%d",
        )
    if "kaynak" in frame.columns:
        config["kaynak"] = st.column_config.LinkColumn(DISPLAY_COLUMNS["kaynak"])
    for key, label in DISPLAY_COLUMNS.items():
        if key in frame.columns and key not in config:
            config[key] = st.column_config.TextColumn(label)
    if "hata" in frame.columns and frame["hata"].fillna("").eq("").all():
        config["hata"] = None
    return config


def competitor_table(report: AnalysisReport) -> pd.DataFrame:
    rows: list[dict[str, str | int]] = []
    for insight in report.insights:
        for activity in insight.competitor_activities:
            rows.append(
                {
                    "Şirket / kaynak": insight.company_name,
                    "Rakip": activity.competitor,
                    "Faaliyet": activity.activity,
                    "PXC için anlamı": activity.implication,
                    "Skor": insight.lead_score,
                }
            )
    return pd.DataFrame(rows)


init_state()

with st.sidebar:
    st.markdown(f"**{COMPANY_SHORT_NAME}-LeadHunter**")
    st.caption(f"{COMPANY_NAME} iç pazar istihbaratı")
    st.divider()
    industry = st.text_input("Sektör", placeholder="Örn. gıda, otomotiv, enerji")
    region = st.text_input("Bölge", placeholder="Örn. İstanbul, Türkiye, EMEA")
    num_results = st.slider("Sonuç sayısı", min_value=3, max_value=20, value=8)
    focus_label = st.segmented_control(
        "Analiz odağı",
        options=list(FOCUS_OPTIONS.keys()),
        default="Tümü",
        required=True,
        width="stretch",
    )
    time_label = st.selectbox("Zaman aralığı", options=list(TIME_OPTIONS.keys()))
    fetch_pages = st.toggle("Sayfa içeriğini çek", value=True)
    st.caption("Kapalıysa yalnızca arama özetleri analiz edilir; daha hızlıdır.")
    st.divider()
    if config_ready():
        st.badge("API hazır", icon=":material/check_circle:", color="green")
    else:
        st.badge("API anahtarı eksik", icon=":material/error:", color="red")
        if st.button("Ayarları yeniden yükle", icon=":material/refresh:"):
            get_settings.cache_clear()
            st.rerun()
        st.caption("`.env` içine OPENAI_API_KEY ve SERPER_API_KEY yazın.")

st.title("PXC-LeadHunter")
st.caption(
    f"{COMPANY_NAME} satış ve bayi ekipleri için müşteri adayı ve rakip istihbaratı paneli."
)

st.pills(
    "Örnek sorgular",
    EXAMPLE_QUERIES,
    selection_mode="single",
    key="example_pill",
    on_change=apply_example,
)

with st.form("search_form", border=True):
    query_input = st.text_input(
        "Arama terimi",
        key="search_query",
        placeholder="Örn. gıda fabrikası otomasyon yatırımı Türkiye",
    )
    submitted = st.form_submit_button(
        "Arama yap",
        type="primary",
        icon=":material/search:",
    )

if submitted:
    st.session_state.last_error = ""
    if not config_ready():
        st.session_state.last_error = (
            "API anahtarları eksik veya geçersiz. `.env` dosyasına "
            "OPENAI_API_KEY ve SERPER_API_KEY ekleyip sayfayı yenileyin."
        )
    else:
        try:
            query = compose_search_query(query_input, industry, region)
        except ValueError as exc:
            st.session_state.last_error = str(exc)
            query = ""

        if query:
            focus = FOCUS_OPTIONS.get(focus_label or "Tümü", "all")
            tbs = TIME_OPTIONS.get(time_label or "Tüm zamanlar")
            try:
                with st.status("PXC istihbaratı taranıyor…", expanded=True) as status:
                    st.write("Web araması yapılıyor.")
                    st.write("Sonuçlar Phoenix Contact ürünleri ve rakiplerle eşleştiriliyor.")
                    report, frame = run_intelligence(
                        query,
                        num_results=num_results,
                        fetch_pages=fetch_pages,
                        focus=focus,
                        tbs=tbs,
                    )
                    st.session_state.report = report
                    st.session_state.frame = frame
                    st.session_state.query_used = query
                    status.update(
                        label=f"{len(frame)} aday hazır",
                        state="complete",
                    )
            except (ConfigError, ScraperError, FatalAnalyzerError) as exc:
                st.session_state.last_error = str(exc)
            except Exception:
                st.session_state.last_error = (
                    "Beklenmeyen bir hata oluştu. Ayrıntı için terminal günlüğüne bakın."
                )

if st.session_state.last_error:
    st.error(st.session_state.last_error)

report: AnalysisReport | None = st.session_state.report
frame: pd.DataFrame = st.session_state.frame

if report is None or frame.empty:
    if not st.session_state.last_error:
        st.info("Arama terimini yazıp **Arama yap** düğmesine basın.")
    st.stop()

with st.container(horizontal=True):
    avg_score = int(frame["puan"].mean()) if "puan" in frame.columns and not frame.empty else 0
    high_count = int((frame["puan"] >= 70).sum()) if "puan" in frame.columns else 0
    competitor_count = (
        int(frame["rakipler"].fillna("").astype(str).str.len().gt(0).sum())
        if "rakipler" in frame.columns
        else 0
    )
    st.metric("Aday", len(frame), border=True)
    st.metric("Ortalama skor", avg_score, border=True)
    st.metric("Yüksek fırsat (70+)", high_count, border=True)
    st.metric("Rakip izi", competitor_count, border=True)

st.caption(f"Sorgu: {st.session_state.query_used}")

filter_bar = st.container(border=True)
with filter_bar:
    st.markdown("**Sonuç filtreleri**")
    sectors = sorted(
        {str(value).strip() for value in frame.get("sektor", pd.Series()).dropna() if str(value).strip()}
    )
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        min_score = st.slider("Minimum skor", 0, 100, 0)
    with f2:
        selected_sectors = st.multiselect("Sektör", sectors)
    with f3:
        competitors_only = st.toggle("Yalnızca rakip kaydı olanlar")
    with f4:
        products_only = st.toggle("Yalnızca PXC ürün eşleşmesi")

filtered = filter_frame(
    frame,
    min_score=min_score,
    sectors=selected_sectors,
    competitors_only=competitors_only,
    products_only=products_only,
)

stamp = datetime.now().strftime("%Y%m%d-%H%M")
with st.container(horizontal=True):
    st.download_button(
        "CSV indir",
        data=to_csv_bytes(filtered),
        file_name=f"pxc-leadhunter-{stamp}.csv",
        mime="text/csv",
        icon=":material/download:",
        disabled=filtered.empty,
    )
    st.download_button(
        "Excel indir",
        data=to_excel_bytes(filtered, report),
        file_name=f"pxc-leadhunter-{stamp}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/table_view:",
        disabled=filtered.empty,
    )

leads_tab, competitors_tab, summary_tab = st.tabs(
    ["Müşteri adayları", "Rakip faaliyetleri", "Yönetici özeti"]
)

with leads_tab:
    if filtered.empty:
        st.warning("Filtrelere uyan kayıt yok.")
    else:
        st.dataframe(
            filtered,
            hide_index=True,
            column_config=column_config_for(filtered),
            height=520,
        )
        if "sirket" in filtered.columns and "puan" in filtered.columns:
            st.bar_chart(
                filtered,
                x="sirket",
                y="puan",
                x_label="Şirket",
                y_label="Fırsat skoru",
                color="#4E7C0F",
                horizontal=True,
            )

with competitors_tab:
    competitors_df = competitor_table(report)
    if competitors_df.empty:
        st.info("Bu taramada rakip faaliyeti bulunamadı.")
    else:
        if competitors_only or min_score > 0:
            allowed = set(filtered.get("sirket", pd.Series()).astype(str))
            competitors_df = competitors_df[competitors_df["Şirket / kaynak"].isin(allowed)]
        st.dataframe(competitors_df, hide_index=True, height=420)

with summary_tab:
    if report.executive_summary:
        st.write(report.executive_summary)
    else:
        st.info("Yönetici özeti üretilemedi.")
