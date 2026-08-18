"""Arama sonuçlarını OpenAI ile pazar istihbaratına dönüştürür.

Phoenix Contact ürün aileleri ve rakip faaliyetleriyle eşleştirme yapar.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import APIError, AuthenticationError, OpenAI, RateLimitError
from pydantic import BaseModel, Field, ValidationError

from config import COMPANY_NAME, COMPANY_SHORT_NAME, Settings
from scraper import SearchBundle, SearchResult

logger = logging.getLogger(__name__)

PHOENIX_CONTACT_CATALOG = """
Phoenix Contact ürün aileleri (eşleştirmede bunları kullan):
- Klemensler / terminal blokları: PT, ST, UT, cliX, PUSH-IN
- Endüstriyel konnektörler: HEAVYCON, M8/M12, PRC, PLUSCON
- Güç kaynakları: QUINT POWER, TRIO POWER, UNO POWER, STEP POWER
- Parafudr / aşırı gerilim koruma: TERMITRAB, VAL-MS, FLASHTRAB
- PLC / kontrol: PLCnext Control, PLCnext Engineer
- I/O sistemleri: Axioline, Inline, analog/dijital I/O
- Endüstriyel Ethernet / ağ: FL SWITCH, SPE, TSN, güvenlik duvarı
- Röle ve arayüz: PLC-INTERFACE, RIFLINE, optokuplör
- İşaretleme / kablolama: CLIPLINE, THERMOMARK, printer, yüksük
- Enerji izleme: EMpro, enerji sayaçları
- E-mobilite: CHARX şarj kontrolörleri ve altyapısı
- Functional safety / emniyet röleleri: PSR, SafetyBridge
- Kabin iklimlendirme ve güç dağıtımı: soğutma, busbar
- Sensör/aktüatör kablolama ve saha bağlantısı
"""

COMPETITORS = [
    "Weidmüller",
    "WAGO",
    "Siemens",
    "Schneider Electric",
    "Rockwell Automation",
    "ABB",
    "Harting",
    "Omron",
    "Mitsubishi Electric",
    "Beckhoff",
    "Pilz",
    "Pepperl+Fuchs",
    "Murrelektronik",
    "LAPP",
    "Rittal",
]


class ProductMatch(BaseModel):
    family: str
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class CompetitorActivity(BaseModel):
    competitor: str
    activity: str
    implication: str


class LeadInsight(BaseModel):
    """Tek bir arama sonucu için üretilen istihbarat kaydı."""

    company_name: str = "Bilinmiyor"
    website: str | None = None
    source_url: str
    title: str
    industry: str | None = None
    location: str | None = None
    contact_hints: list[str] = Field(default_factory=list)
    phoenix_product_matches: list[ProductMatch] = Field(default_factory=list)
    competitor_activities: list[CompetitorActivity] = Field(default_factory=list)
    buying_signals: list[str] = Field(default_factory=list)
    opportunity_summary: str = ""
    recommended_next_step: str = ""
    lead_score: int = Field(default=0, ge=0, le=100)
    error: str | None = None

    def to_row(self) -> dict[str, Any]:
        products = "; ".join(
            f"{item.family} ({item.confidence:.0%})" for item in self.phoenix_product_matches
        )
        competitors = "; ".join(
            f"{item.competitor}: {item.activity}" for item in self.competitor_activities
        )
        return {
            "sirket": self.company_name,
            "sektor": self.industry or "",
            "lokasyon": self.location or "",
            "puan": self.lead_score,
            "urun_eslesmeleri": products,
            "rakipler": competitors,
            "satin_alma_sinyalleri": "; ".join(self.buying_signals),
            "ozet": self.opportunity_summary,
            "sonraki_adim": self.recommended_next_step,
            "iletisim": "; ".join(self.contact_hints),
            "kaynak": self.source_url,
            "baslik": self.title,
            "hata": self.error or "",
        }


class AnalysisReport(BaseModel):
    query: str
    insights: list[LeadInsight] = Field(default_factory=list)
    executive_summary: str = ""


class AnalyzerError(RuntimeError):
    """OpenAI analiz hatası."""

    fatal: bool = False


class FatalAnalyzerError(AnalyzerError):
    """Tüm analiz oturumunu durdurması gereken hata (ör. geçersiz API anahtarı)."""

    fatal = True


def _result_payload(result: SearchResult) -> dict[str, Any]:
    page = (result.page_text or "").strip()
    if len(page) > 6000:
        page = page[:6000] + "…"
    return {
        "title": result.title,
        "url": result.url,
        "source": result.source,
        "snippet": result.snippet,
        "page_text": page,
    }


class LeadAnalyzer:
    """OpenAI ile müşteri adayı ve pazar istihbaratı üretir."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key)

    def analyze_result(self, result: SearchResult, *, focus: str = "all") -> LeadInsight:
        """Tek bir arama sonucunu yapılandırılmış içgörüye çevirir."""
        system_prompt = f"""Sen {COMPANY_NAME} ({COMPANY_SHORT_NAME}) için çalışan kıdemli bir pazar istihbaratı analistisin.
Görevin web arama sonuçlarından B2B müşteri adayı ve rakip istihbaratı çıkarmak.
PXC her zaman {COMPANY_NAME} anlamındadır; başka bir kısaltma olarak yorumlama.

{PHOENIX_CONTACT_CATALOG}

Bilinen rakipler: {", ".join(COMPETITORS)}

Kurallar:
- Yalnızca sağlanan metne dayan. Uydurma iletişim bilgisi veya şirket uydurma.
- Şirket adı belirsizse "Bilinmiyor" yaz.
- Phoenix Contact ürün eşleşmesini somut ihtiyaçlara bağla (panel imalatı, otomasyon yatırımı, enerji izleme, EV şarj, vs.).
- Rakip adı geçmiyorsa competitor_activities boş liste olsun.
- lead_score 0-100: alaka, satın alma sinyali, ürün uyumu ve net şirket kimliği.
- Çıktı DİLİ: Türkçe.
- Yalnızca geçerli JSON nesnesi döndür.

JSON şeması:
{{
  "company_name": "string",
  "website": "string veya null",
  "industry": "string veya null",
  "location": "string veya null",
  "contact_hints": ["e-posta, telefon, form, kişi unvanı gibi ipuçları"],
  "phoenix_product_matches": [{{"family": "string", "reason": "string", "confidence": 0.0}}],
  "competitor_activities": [{{"competitor": "string", "activity": "string", "implication": "string"}}],
  "buying_signals": ["string"],
  "opportunity_summary": "2-4 cümle",
  "recommended_next_step": "satış/saha için somut aksiyon",
  "lead_score": 0
}}

Odak: {focus}
- leads: müşteri adayı ve satın alma sinyali
- market: sektör trendi ve yatırım haberleri
- competitors: rakip faaliyetleri
- all: hepsi
"""
        user_prompt = (
            "Aşağıdaki arama sonucunu analiz et ve JSON üret:\n"
            + json.dumps(_result_payload(result), ensure_ascii=False, indent=2)
        )

        try:
            completion = self.client.chat.completions.create(
                model=self.settings.openai_model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except AuthenticationError as exc:
            raise FatalAnalyzerError(
                "OpenAI kimlik doğrulaması başarısız. OPENAI_API_KEY değerini kontrol edin."
            ) from exc
        except RateLimitError as exc:
            raise FatalAnalyzerError(
                "OpenAI kota/rate-limit aşıldı. Bir süre sonra tekrar deneyin."
            ) from exc
        except APIError as exc:
            raise AnalyzerError(f"OpenAI API hatası: {exc}") from exc
        except Exception as exc:  # ağ veya SDK beklenmeyen hataları
            raise AnalyzerError(f"Analiz isteği gönderilemedi: {exc}") from exc

        content = (completion.choices[0].message.content or "").strip()
        if not content:
            return LeadInsight(
                source_url=result.url,
                title=result.title,
                opportunity_summary="Model boş yanıt döndürdü.",
                error="empty_response",
            )

        try:
            parsed = json.loads(content)
            insight = LeadInsight.model_validate(
                {
                    **parsed,
                    "source_url": result.url,
                    "title": result.title,
                }
            )
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Model çıktısı doğrulanamadı (%s): %s", result.url, exc)
            return LeadInsight(
                source_url=result.url,
                title=result.title,
                opportunity_summary=content[:500],
                error=f"parse_error: {exc}",
            )
        return insight

    def summarize(self, query: str, insights: list[LeadInsight]) -> str:
        """Tüm içgörülerden kısa bir yönetici özeti üretir."""
        viable = [item for item in insights if not item.error]
        if not viable:
            return "Analiz edilecek geçerli sonuç yok."

        compact = [
            {
                "sirket": item.company_name,
                "puan": item.lead_score,
                "urunler": [m.family for m in item.phoenix_product_matches],
                "rakipler": [c.competitor for c in item.competitor_activities],
                "ozet": item.opportunity_summary,
            }
            for item in sorted(viable, key=lambda row: row.lead_score, reverse=True)[:12]
        ]
        prompt = (
            "Phoenix Contact satış ekibi için 4-6 cümlelik Türkçe yönetici özeti yaz. "
            "Öne çıkan müşteri adaylarını, ürün fırsatlarını ve rakip hareketlerini belirt. "
            f"Arama sorgusu: {query}\n"
            f"Veri:\n{json.dumps(compact, ensure_ascii=False, indent=2)}"
        )
        try:
            completion = self.client.chat.completions.create(
                model=self.settings.openai_model,
                temperature=0.3,
                messages=[
                    {
                        "role": "system",
                        "content": "Sen Phoenix Contact pazar istihbaratı analistisin. Kısa ve eyleme dönük yaz.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            return (completion.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("Yönetici özeti üretilemedi: %s", exc)
            top = viable[0]
            return (
                f"{len(viable)} aday incelendi. En yüksek puanlı kayıt: "
                f"{top.company_name} ({top.lead_score}/100). {top.opportunity_summary}"
            )

    def analyze_bundle(
        self,
        bundle: SearchBundle,
        *,
        focus: str = "all",
        continue_on_error: bool = True,
    ) -> AnalysisReport:
        """Tüm arama sonuçlarını analiz eder."""
        insights: list[LeadInsight] = []
        for result in bundle.results:
            try:
                insight = self.analyze_result(result, focus=focus)
                insights.append(insight)
                logger.info(
                    "Analiz: %s → %s (%s)",
                    result.title[:60],
                    insight.company_name,
                    insight.lead_score,
                )
            except FatalAnalyzerError:
                raise
            except AnalyzerError:
                if not continue_on_error:
                    raise
                logger.exception("Kayıt analiz edilemedi: %s", result.url)
                insights.append(
                    LeadInsight(
                        source_url=result.url,
                        title=result.title,
                        opportunity_summary="Bu kayıt analiz edilemedi.",
                        error="analyzer_error",
                    )
                )

        insights.sort(key=lambda item: item.lead_score, reverse=True)
        summary = self.summarize(bundle.query, insights) if insights else ""
        return AnalysisReport(query=bundle.query, insights=insights, executive_summary=summary)
