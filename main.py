#!/usr/bin/env python3
"""PXC-LeadHunter CLI.

İnternetten pazar istihbaratı ve Phoenix Contact müşteri adayı toplar.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from analyzer import AnalysisReport, FatalAnalyzerError, LeadAnalyzer
from config import COMPANY_NAME, COMPANY_SHORT_NAME, ConfigError, get_settings
from scraper import ScraperError, WebScraper

LOGGER = logging.getLogger("pxc_leadhunter")

FOCUS_CHOICES = ("all", "leads", "market", "competitors")


def configure_stdio() -> None:
    """Windows konsolunda Türkçe karakterlerin bozulmasını önler."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pxc-leadhunter",
        description=(
            f"PXC-LeadHunter — {COMPANY_SHORT_NAME} ({COMPANY_NAME}) için web araması, "
            "pazar istihbaratı ve müşteri adayı toplama aracı."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Serbest arama sorgusu (ör. 'gıda fabrikası otomasyon yatırımı Türkiye').",
    )
    parser.add_argument("-q", "--query-opt", dest="query_opt", help="Sorguyu seçenek olarak verin.")
    parser.add_argument("-i", "--industry", help="Sektör filtresi (sorguya eklenir).")
    parser.add_argument("-r", "--region", help="Bölge / ülke (sorguya eklenir).")
    parser.add_argument(
        "-n",
        "--num",
        type=int,
        default=8,
        help="Çekilecek organik arama sonucu sayısı (1-20).",
    )
    parser.add_argument(
        "--focus",
        choices=FOCUS_CHOICES,
        default="all",
        help="Analiz odağı: leads, market, competitors veya all.",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Sayfa içeriği çekmeden yalnızca arama özetlerini analiz et.",
    )
    parser.add_argument(
        "--tbs",
        help="Serper zaman filtresi (ör. qdr:w = son 1 hafta, qdr:m = son 1 ay).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Sonuç dosyası (.csv veya .json).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Ayrıntılı log göster.",
    )
    return parser


def compose_search_query(
    query: str,
    industry: str = "",
    region: str = "",
    *,
    add_product_context: bool = True,
) -> str:
    """CLI ve Streamlit arayüzünün ortak sorgu birleştiricisi."""
    parts = [part.strip() for part in (query, industry, region) if part and part.strip()]
    if not parts:
        raise ValueError("Arama sorgusu gerekli.")

    context = "endüstriyel otomasyon elektrik bağlantı klemens PLC"
    joined = " ".join(parts)
    lowered = joined.lower()
    if (
        add_product_context
        and "phoenix contact" not in lowered
        and "klemens" not in lowered
    ):
        joined = f"{joined} {context}"
    return " ".join(joined.split())


def compose_query(args: argparse.Namespace) -> str:
    try:
        return compose_search_query(
            args.query or args.query_opt or "",
            args.industry or "",
            args.region or "",
        )
    except ValueError:
        raise SystemExit(
            "Arama sorgusu gerekli. Örnek:\n"
            '  python main.py "panel imalatı klemens tedarik Türkiye" -n 8 -o leads.csv'
        ) from None


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)


def report_to_frame(report: AnalysisReport) -> pd.DataFrame:
    rows = [insight.to_row() for insight in report.insights]
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    preferred = [
        "sirket",
        "puan",
        "sektor",
        "lokasyon",
        "urun_eslesmeleri",
        "rakipler",
        "satin_alma_sinyalleri",
        "ozet",
        "sonraki_adim",
        "iletisim",
        "kaynak",
        "baslik",
        "hata",
    ]
    columns = [col for col in preferred if col in frame.columns]
    return frame[columns]


def run_intelligence(
    query: str,
    *,
    num_results: int = 8,
    fetch_pages: bool = True,
    focus: str = "all",
    tbs: str | None = None,
) -> tuple[AnalysisReport, pd.DataFrame]:
    """Arama + analiz boru hattı (CLI ve Streamlit ortak)."""
    if focus not in FOCUS_CHOICES:
        raise ValueError(f"Geçersiz odak: {focus}")
    num_results = max(1, min(int(num_results), 20))

    get_settings.cache_clear()
    settings = get_settings()
    scraper = WebScraper(settings)
    analyzer = LeadAnalyzer(settings)

    LOGGER.info("Arama başlıyor: %s", query)
    bundle = scraper.collect(
        query,
        num_results=num_results,
        fetch_pages=fetch_pages,
        tbs=tbs,
    )
    if not bundle.results:
        raise ScraperError("Arama sonucu bulunamadı. Sorguyu sadeleştirmeyi deneyin.")

    LOGGER.info("%s sonuç analiz ediliyor…", len(bundle.results))
    report = analyzer.analyze_bundle(bundle, focus=focus)
    return report, report_to_frame(report)


def save_report(frame: pd.DataFrame, report: AnalysisReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame.to_csv(path, index=False, encoding="utf-8-sig")
    elif suffix == ".json":
        payload = {
            "query": report.query,
            "executive_summary": report.executive_summary,
            "leads": [insight.model_dump() for insight in report.insights],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise SystemExit("Çıktı uzantısı .csv veya .json olmalıdır.")
    LOGGER.info("Sonuç kaydedildi: %s", path.resolve())


def print_report(frame: pd.DataFrame, report: AnalysisReport) -> None:
    print()
    print("=" * 72)
    print(f"PXC-LeadHunter  |  {COMPANY_NAME} pazar istihbaratı")
    print("=" * 72)
    print(f"Sorgu : {report.query}")
    print(f"Aday  : {len(report.insights)}")
    print("-" * 72)
    if report.executive_summary:
        print("Yönetici özeti")
        print(report.executive_summary)
        print("-" * 72)
    if frame.empty:
        print("Gösterilecek kayıt yok.")
        return
    display = frame.copy()
    for column in ("ozet", "urun_eslesmeleri", "rakipler", "kaynak", "baslik"):
        if column in display.columns:
            display[column] = display[column].astype(str).str.slice(0, 80)
    with pd.option_context("display.max_colwidth", 80, "display.width", 140):
        print(display.to_string(index=False))
    print("=" * 72)


def run(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    query = compose_query(args)
    num_results = max(1, min(args.num, 20))

    try:
        report, frame = run_intelligence(
            query,
            num_results=num_results,
            fetch_pages=not args.no_fetch,
            focus=args.focus,
            tbs=args.tbs,
        )
    except (ScraperError, ConfigError, FatalAnalyzerError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("Kullanıcı işlemi iptal etti.")
        return 130
    except Exception:
        LOGGER.exception("Beklenmeyen hata.")
        return 1

    print_report(frame, report)

    if args.output:
        try:
            save_report(frame, report, args.output)
        except OSError as exc:
            LOGGER.error("Dosya yazılamadı: %s", exc)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(run())
