"""LLM-экстрактор реквизитов, резолюции и сроков. Без ключа работает fallback."""

from __future__ import annotations

import json
import logging
import os
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import ValidationError

from app.models.schemas import (
    CourtOrderItem,
    DeadlineItem,
    DocumentAnalysisResult,
    DocumentType,
    ExtractionOutcome,
    NextHearingInfo,
    PageText,
    PartyInfo,
    PartyRole,
    PdfParseResult,
    QuoteReference,
    StageCode,
)
from app.services.legal_knowledge import STAGE_TITLES, LegalKnowledgeService
from app.services.pdf_parser import canonical_case_number, ocr_search_copy

logger = logging.getLogger(__name__)

_DOCUMENT_TYPES: set[str] = {
    "Определение",
    "Решение",
    "Постановление",
    "Требование кредитора",
    "Выписка",
}
_MONEY_RE = re.compile(
    r"(\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{2})?|\d+(?:[.,]\d{2})?)\s*(?:руб(?:лей|ля|\.)?|₽)",
    re.IGNORECASE,
)
_LAW_RE = re.compile(
    r"стать(?:ья|и|ями|ей|е|ю)\s+\d[\d\.\s,и]{0,80}?"
    r"\s*(?:АПК\s*РФ|ГК\s*РФ|ГПК\s*РФ|"
    r"Федерального\s+закона(?:\s+от\s+\d{2}\.\d{2}\.\d{4})?"
    r"(?:\s*№\s*[\d\-\u00ad\u2010\u2011\u2013\u2014ФЗ]+)?(?:\s*[«\"][^»\"\n]{0,80}[»\"])?)",
    re.IGNORECASE,
)
_WORD_DAYS = {
    "один": 1,
    "одного": 1,
    "два": 2,
    "двух": 2,
    "три": 3,
    "трех": 3,
    "четыре": 4,
    "четырех": 4,
    "пять": 5,
    "пяти": 5,
    "шесть": 6,
    "шести": 6,
    "семь": 7,
    "семи": 7,
    "восемь": 8,
    "восьми": 8,
    "девять": 9,
    "девяти": 9,
    "десять": 10,
    "десяти": 10,
    "одиннадцать": 11,
    "одиннадцати": 11,
    "двенадцать": 12,
    "двенадцати": 12,
    "тринадцать": 13,
    "тринадцати": 13,
    "четырнадцать": 14,
    "четырнадцати": 14,
    "пятнадцать": 15,
    "пятнадцати": 15,
    "шестнадцать": 16,
    "шестнадцати": 16,
    "семнадцать": 17,
    "семнадцати": 17,
    "восемнадцать": 18,
    "восемнадцати": 18,
    "девятнадцать": 19,
    "девятнадцати": 19,
    "двадцать": 20,
    "двадцати": 20,
    "тридцать": 30,
    "тридцати": 30,
}
_STAGE_CODES = set(STAGE_TITLES)
_ROLES = {
    "Истец",
    "Ответчик",
    "Должник",
    "Кредитор",
    "Третье лицо",
    "СРО АУ",
    "Финансовый управляющий",
}
_KNOWLEDGE = LegalKnowledgeService()
_SYSTEM_PROMPT = """Ты — юрист-арбитражник. Верни один JSON и ничего кроме JSON.
Только то, что прямо написано в [PAGE N]. Запрещено додумывать факты, даты, суммы, участников, залы и статьи.
Нет сведения — поле null, список []. null внутрь массивов не клади. Не достраивай карточку «по типичному делу». legal_knowledge всегда null.
stage_code: bankruptcy_individual_accept | bankruptcy_individual_restructuring | bankruptcy_individual_realization | bankruptcy_legal_entity | creditor_registry_claim | transaction_challenge | subsidiary_liability | general_arbitration_dispute.
принять заявление к производству = bankruptcy_individual_accept; ввести реструктуризацию долгов = bankruptcy_individual_restructuring; ввести процедуру реализации имущества или признать банкротом = bankruptcy_individual_realization.
case_stage — короткое русское название стадии. ФИО в именительном падеже. «финансовым управляющим должника ФИО» — Финансовый управляющий, не Должник.
key_points: 2–3 тезиса по существу. Без электронной подписи судьи, kad.arbitr.ru, копий в Росреестр и пересказа ст. 65, 121, 123 АПК РФ.
court_orders и deadlines — только действия должника, кредиторов, ФУ, СРО, истца, ответчика. Не включай обязанности суда (бумажная копия в пятидневный срок по ходатайству), условия до подачи заявления и разъяснения без действия.
financial_claims: «кто кому» и общие суммы из акта. Две разные суммы верни обе дословно.
act_date — дата акта YYYY-MM-DD. next_hearing.date — будущее заседание. Дата рождения, штамп электронной подписи и дата закона или пленума — не act_date и не заседание.
Конечная дата суда («до 11 мая 2027 года», «до 06 ноября 2026 года»): anchor_event=fixed_date, due_date=YYYY-MM-DD, не считай act_date+N. «в течение месяца»: months_limit=1, days_limit=0. «с даты получения»: receipt_date, due_date=null. Рабочие дни: days_type=business, иначе calendar. Пропись: девяти=9, четырнадцати=14, пяти=5, семи=7, пятнадцати=15.
exact_quote дословно из [PAGE N], page_number = N. regex_hints — подсказка; при расхождении верь тексту страницы.
role: Истец | Ответчик | Должник | Кредитор | Третье лицо | СРО АУ | Финансовый управляющий.
document_type: Определение | Решение | Постановление | Требование кредитора | Выписка.
"""

_RESPONSE_EXAMPLE: dict[str, object] = {
    "case_number": "<номер дела из [PAGE N]>",
    "court_name": "<название суда из [PAGE N]>",
    "judge_name": None,
    "document_type": "Определение",
    "document_date": "<дата как в шапке акта>",
    "stage_code": "general_arbitration_dispute",
    "case_stage": "<короткое русское название стадии по тексту>",
    "act_date": None,
    "parties": [
        {
            "role": "Кредитор",
            "name": "<наименование или ФИО только из [PAGE N]>",
            "inn": None,
            "ogrn": None,
            "snils": None,
            "address": None,
        }
    ],
    "verdict_summary": "<1–2 предложения только из резолютивной части>",
    "financial_claims": [],
    "next_hearing": {
        "date": None,
        "time": None,
        "location": None,
        "purpose": None,
        "source_quote": None,
    },
    "court_orders": [
        {
            "actor": "СРО",
            "action": "Что именно сделать",
            "deadline_text": "в течение девяти дней с даты получения",
            "days_limit": 9,
            "days_type": "calendar",
            "anchor_event": "receipt_date",
            "due_date": None,
            "source_quote": {
                "page_number": 1,
                "exact_quote": "дословная цитата из блока [PAGE N]",
                "verified": True,
            },
        }
    ],
    "deadlines": [
        {
            "description": "Подача апелляционной жалобы",
            "due_date": None,
            "days_limit": 10,
            "days_type": "calendar",
            "anchor_event": "act_date",
            "urgency": "critical",
            "source_quote": {
                "page_number": 1,
                "exact_quote": "дословная цитата из блока [PAGE N]",
                "verified": True,
            },
        }
    ],
    "legal_basis": [],
    "key_points": [
        {
            "page_number": 1,
            "exact_quote": "дословная цитата из блока [PAGE N]",
            "verified": True,
        }
    ],
    "legal_knowledge": None,
}

_RESPONSE_RULES = """days_type: calendar | business | unknown. anchor_event: act_date | receipt_date | hearing_date | fixed_date | unknown. urgency: critical | normal.
Значения в угловых скобках — пустые места, не факты: не копируй их и не подставляй вымышленные названия.
"""


class AILegalExtractor:
    """Структурирует судебный акт через OpenAI-совместимый API или локальные regex."""

    def __init__(self) -> None:
        load_dotenv()
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        self.project = os.getenv("OPENAI_PROJECT", "").strip()
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        self._last_llm_raw = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def provider_name(self) -> str:
        if not self.is_configured:
            return "fallback"
        if "yandex" in self.base_url.lower():
            return "yandex"
        return "openai-compatible"

    def extract(self, parsed: PdfParseResult) -> ExtractionOutcome:
        token_usage: Optional[dict[str, int]] = None
        if not self.is_configured:
            analysis = build_fallback_analysis(parsed)
            warnings = ["OPENAI_API_KEY не задан: карточка собрана регулярными выражениями, без LLM."]
            warnings.extend(_fallback_warnings(parsed, analysis))
            mode = "fallback"
        else:
            try:
                analysis, llm_notes, token_usage = self._call_llm(parsed)
                warnings = list(llm_notes)
                mode = "llm"
            except Exception as exc:
                self._log_llm_failure(exc)
                analysis = build_fallback_analysis(parsed)
                warnings = [
                    f"LLM недоступен ({_safe_error(exc)}). Карточка собрана регулярными выражениями.",
                    *_fallback_warnings(parsed, analysis),
                ]
                mode = "fallback"
                token_usage = None
        warnings.extend(apply_layer3(parsed, analysis))
        return ExtractionOutcome(
            analysis=analysis,
            extraction_mode=mode,
            warnings=warnings,
            token_usage=token_usage,
        )

    def _call_llm(self, parsed: PdfParseResult) -> tuple[DocumentAnalysisResult, list[str], Optional[dict[str, int]]]:
        self._last_llm_raw = ""
        client = OpenAI(**self._client_kwargs())
        example = json.dumps(_RESPONSE_EXAMPLE, ensure_ascii=False, separators=(",", ":"))
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Верни один JSON-объект строго в такой форме. Имена полей не переименовывай.\n"
                    f"{_RESPONSE_RULES}\n"
                    f"Пример:\n{example}\n\n"
                    f"regex_hints:\n{parsed.metadata.model_dump_json()}\n\n"
                    f"Текст документа:\n{_prompt_text(parsed.pages)}"
                ),
            },
        ]
        completion = self._complete(client, messages)
        content = completion.choices[0].message.content or ""
        self._last_llm_raw = content
        fallback = build_fallback_analysis(parsed)
        payload = _sanitize_llm_payload(_unwrap_payload(_load_json(content)), parsed, fallback)
        notes = [str(item) for item in payload.pop("_sanitize_warnings", [])]
        usage = _read_token_usage(completion)
        return DocumentAnalysisResult.model_validate(payload), notes, usage

    def _log_llm_failure(self, exc: Exception) -> None:
        raw = _public_llm_text(getattr(self, "_last_llm_raw", ""))
        if isinstance(exc, ValidationError):
            logger.warning(
                "Ответ LLM не прошёл проверку DocumentAnalysisResult. Сырой ответ: %s. Поля: %s",
                raw,
                _validation_field_list(exc),
            )
            return
        logger.warning("LLM extraction failed: %s. Сырой ответ: %s", _safe_error(exc), raw)

    def _client_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {"api_key": self.api_key, "timeout": 120.0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.project:
            kwargs["project"] = self.project
        return kwargs

    def _complete(self, client: OpenAI, messages: list[dict[str, str]]) -> object:
        try:
            return client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=messages,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            if not _response_format_rejected(exc):
                raise
            logger.info("Провайдер не принял response_format, повтор без него")
            return client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=messages,
            )


def build_fallback_analysis(parsed: PdfParseResult) -> DocumentAnalysisResult:
    """Собирает карточку из текста PDF, если LLM не вызывается."""

    pages = parsed.pages
    metadata = parsed.metadata
    full_text = "\n".join(page.text for page in pages)
    if not full_text.strip():
        return DocumentAnalysisResult(
            case_number=metadata.case_number or "НЕ ИЗВЛЕЧЁН",
            court_name=metadata.court_name or "Суд не указан в текстовом слое",
            judge_name=metadata.judge_name,
            document_type="Выписка",
            document_date=None,
            stage_code="general_arbitration_dispute",
            case_stage=STAGE_TITLES["general_arbitration_dispute"],
            act_date=metadata.act_date,
            parties=metadata.parties,
            verdict_summary=(
                "Текстовый слой PDF пуст. Вероятно, это скан без OCR, "
                "поэтому резолютивная часть и сроки не извлечены."
            ),
            financial_claims=[],
            deadlines=[],
            legal_basis=[],
            key_points=[],
        )

    document_type = _refine_document_type(metadata.document_type, full_text)
    stage_code, case_stage = _classify_stage(full_text)
    act_date = metadata.act_date
    document_date = _iso_to_ru(act_date) or (metadata.dates[0] if metadata.dates else None)
    verdict = _verdict_summary(pages, document_type)
    return DocumentAnalysisResult(
        case_number=metadata.case_number or "НЕ ИЗВЛЕЧЁН",
        court_name=metadata.court_name or "Суд не указан в текстовом слое",
        judge_name=metadata.judge_name,
        document_type=document_type,
        document_date=document_date,
        stage_code=stage_code,
        case_stage=case_stage,
        act_date=act_date,
        parties=metadata.parties,
        verdict_summary=verdict,
        financial_claims=_financial_claims(metadata.parties, pages),
        next_hearing=_next_hearing(pages),
        court_orders=_court_orders(pages),
        deadlines=_deadlines(pages, document_type, act_date or document_date),
        legal_basis=_legal_basis(full_text),
        key_points=_key_points(pages),
    )


def _fallback_warnings(parsed: PdfParseResult, analysis: DocumentAnalysisResult) -> list[str]:
    warnings: list[str] = []
    if not any(page.text.strip() for page in parsed.pages):
        warnings.append("В PDF нет текстового слоя.")
    elif parsed.metadata.document_type is None and analysis.document_type == "Выписка":
        warnings.append("Тип акта по заголовку не найден, в карточке указана «Выписка».")
    if analysis.case_number == "НЕ ИЗВЛЕЧЁН":
        warnings.append("Арбитражный номер дела на первой странице не найден.")
    return warnings


def _refine_document_type(current: Optional[DocumentType], text: str) -> DocumentType:
    if current is not None:
        return current
    lowered = text.lower()
    if "определил" in lowered:
        return "Определение"
    if "постановил" in lowered:
        return "Постановление"
    if re.search(r"\bрешил\b", lowered):
        return "Решение"
    return "Выписка"


def _verdict_summary(pages: list[PageText], document_type: DocumentType) -> str:
    for page in pages:
        section = _between(page.text, ("определил", "решил", "постановил"), ("судья",))
        sentences = [item for item in _sentences(section) if not _is_marker(item)]
        if sentences:
            chosen = sentences[:1] if len(sentences[0]) >= 40 else sentences[:2]
            summary = re.sub(
                r"^(?:определил|решил|постановил)\s*:\s*",
                "",
                " ".join(chosen),
                count=1,
                flags=re.IGNORECASE,
            )
            return summary[:700]
    for page in pages:
        sentences = _sentences(page.text)
        if sentences:
            return sentences[0][:700]
    return f"Резолютивная часть в тексте акта «{document_type}» не выделена."


def _financial_claims(parties: list, pages: list[PageText]) -> list[str]:
    amounts: list[str] = []
    for page in pages:
        for sentence in _sentences(page.text):
            found = _MONEY_RE.findall(sentence)
            if not found:
                continue
            amount = found[0]
            claim = _format_claim(parties, amount, sentence)
            if claim not in amounts:
                amounts.append(claim)
    return amounts[:8]


def _format_claim(parties: list, amount: str, sentence: str) -> str:
    creditors = [party.name for party in parties if party.role == "Кредитор"]
    debtors = [party.name for party in parties if party.role == "Должник"]
    plaintiffs = [party.name for party in parties if party.role == "Истец"]
    defendants = [party.name for party in parties if party.role == "Ответчик"]
    left = (creditors or plaintiffs or [None])[0]
    right = (debtors or defendants or [None])[0]
    money = amount if re.search(r"руб|₽", amount, re.IGNORECASE) else f"{amount} руб."
    if left and right:
        return f"{left} → {right}: {money}"
    compact = re.sub(r"\s+", " ", sentence).strip()
    return compact[:400]


def _deadlines(pages: list[PageText], document_type: DocumentType, document_date: Optional[str]) -> list[DeadlineItem]:
    items: list[DeadlineItem] = []
    publication = _sentence_quote(pages, re.compile(r"ефрсб|федресурс", re.IGNORECASE))
    if publication is not None:
        days = _days_near(publication.exact_quote, default=3)
        business = "рабоч" in publication.exact_quote.lower()
        items.append(
            DeadlineItem(
                description="Публикация на ЕФРСБ",
                due_date=None,
                days_limit=days,
                days_type="business" if business else "calendar",
                anchor_event="act_date",
                urgency="critical" if days <= 10 else "normal",
                source_quote=publication,
            )
        )

    appeal = _sentence_quote(pages, re.compile(r"апелляционн|обжалован", re.IGNORECASE))
    if appeal is not None:
        default_days = 10 if document_type == "Определение" else 30
        days = _days_near(appeal.exact_quote, default=default_days)
        items.append(
            DeadlineItem(
                description="Подача апелляционной жалобы",
                due_date=None,
                days_limit=days,
                days_type="business" if "рабоч" in appeal.exact_quote.lower() else "calendar",
                anchor_event="receipt_date" if re.search(r"получен", appeal.exact_quote, re.IGNORECASE) else "act_date",
                urgency="critical" if days <= 10 else "normal",
                source_quote=appeal,
            )
        )
    return items


def _legal_basis(text: str) -> list[str]:
    found: list[str] = []
    for match in _LAW_RE.finditer(text):
        clause = _readable(match.group(0)).strip(" ,.;")
        if clause and clause not in found:
            found.append(clause)
    return found[:12]


def _key_points(pages: list[PageText]) -> list[QuoteReference]:
    points: list[QuoteReference] = []
    for page in pages:
        section = _between(page.text, ("установил", "установила"), ("определил", "решил", "постановил"))
        for sentence in _sentences(section):
            if _is_marker(sentence):
                continue
            quote = _exact_substring(page.text, sentence)
            if quote is None:
                continue
            quote = _without_marker(page.text, quote)
            points.append(QuoteReference(page_number=page.page, exact_quote=quote[:700]))
            if len(points) >= 4:
                return points
    return points


def _sentence_quote(pages: list[PageText], pattern: re.Pattern[str]) -> Optional[QuoteReference]:
    for page in pages:
        for sentence in _sentences(page.text):
            if not pattern.search(sentence):
                continue
            quote = _exact_substring(page.text, sentence)
            if quote is not None:
                return QuoteReference(page_number=page.page, exact_quote=quote[:700])
    return None


def _between(text: str, starts: tuple[str, ...], ends: tuple[str, ...]) -> str:
    lowered = text.lower()
    start_at = -1
    start_len = 0
    for marker in starts:
        index = lowered.find(marker)
        if index >= 0 and (start_at < 0 or index < start_at):
            start_at = index
            start_len = len(marker)
    if start_at < 0:
        return ""
    end_at = -1
    for marker in ends:
        index = lowered.find(marker, start_at + start_len)
        if index >= 0 and (end_at < 0 or index < end_at):
            end_at = index
    return text[start_at: end_at if end_at > 0 else start_at + 1800]


def _sentences(text: str) -> list[str]:
    flattened = re.sub(r"\s+", " ", text).strip()
    if not flattened:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[А-ЯA-Z«\"])", flattened)
    return [part.strip() for part in parts if len(part.strip()) >= 25]


def _exact_substring(page_text: str, sentence: str) -> Optional[str]:
    tokens = sentence.split()
    if len(tokens) < 2:
        return None
    pattern = r"\s+".join(re.escape(token) for token in tokens[:50])
    match = re.search(pattern, page_text, flags=re.IGNORECASE)
    if match is None:
        return None
    return match.group(0).strip()


def _is_marker(sentence: str) -> bool:
    lowered = sentence.lower().strip(" :.")
    return lowered in {"установил", "установила", "определил", "решил", "постановил"}


def _days_near(text: str, default: int) -> int:
    match = re.search(r"(\d+)\s+(?:рабочих\s+|календарных\s+)?дн", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    lowered = text.lower().replace("ё", "е")
    for word, value in _WORD_DAYS.items():
        if re.search(rf"\b{word.replace('ё', 'е')}\b", lowered):
            return value
    return default


def _shift_date(document_date: Optional[str], days: int) -> Optional[str]:
    if not document_date:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(document_date, fmt).date()
        except ValueError:
            continue
        return (parsed + timedelta(days=days)).isoformat()
    return None


def _prompt_text(pages: list[PageText]) -> str:
    """Шапка и резолюция сохраняются, даже если середина документа не помещается."""

    pieces = [_clip_page(page) for page in pages]
    total = sum(len(piece) for piece in pieces)
    if len(pages) <= 15 and total <= 100_000:
        return "\n\n".join(pieces)
    tail_count = min(3, len(pages))
    tail = pieces[-tail_count:]
    head: list[str] = []
    used = sum(len(piece) for piece in tail)
    source = pieces[:-tail_count] if tail_count else pieces
    omitted = False
    for piece in source:
        if used + len(piece) > 100_000:
            omitted = True
            break
        head.append(piece)
        used += len(piece)
    if omitted:
        head.append("…[средние страницы опущены; резолютивная часть сохранена в последних страницах]")
    return "\n\n".join(head + tail)


def _clip_page(page: PageText) -> str:
    body = page.text.strip() or "[пустая страница или скан без текстового слоя]"
    if len(body) > 8000:
        body = body[:8000] + "\n…[страница обрезана]"
    return f"[PAGE {page.page}]\n{body}"


def _load_json(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match is None:
            raise ValueError("Ответ модели не содержит JSON") from None
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("JSON модели должен быть объектом")
    return payload


def _unwrap_payload(payload: dict) -> dict:
    if "case_number" in payload:
        return payload
    for key in ("result", "analysis", "data", "document"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return nested
    return payload


def _coerce_document_type(value: str) -> DocumentType:
    if value in _DOCUMENT_TYPES:
        return value  # type: ignore[return-value]
    lowered = value.lower()
    for label in _DOCUMENT_TYPES:
        if label.lower() in lowered:
            return label  # type: ignore[return-value]
    raise ValueError(f"Неизвестный тип акта: {value}")


def _verify_quotes(pages: list[PageText], analysis: DocumentAnalysisResult) -> list[str]:
    warnings: list[str] = []
    references = list(analysis.key_points)
    references.extend(item.source_quote for item in analysis.deadlines)
    references.extend(item.source_quote for item in analysis.court_orders)
    if analysis.next_hearing and analysis.next_hearing.source_quote:
        references.append(analysis.next_hearing.source_quote)
    for reference in references:
        if _quote_matches(pages, reference.page_number, reference.exact_quote):
            reference.verified = True
            continue
        relocated = _find_page(pages, reference.exact_quote)
        if relocated is not None:
            reference.page_number = relocated
            reference.verified = True
            continue
        reference.verified = False
        warnings.append(
            "Цитата не найдена дословно в тексте PDF: "
            f"«{reference.exact_quote[:120]}»"
        )
    return warnings


def _read_token_usage(completion: object) -> Optional[dict[str, int]]:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total = usage.get("total_tokens")
    else:
        prompt = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total = getattr(usage, "total_tokens", None)
    if prompt is None and completion_tokens is None and total is None:
        return None
    counted = {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total or (int(prompt or 0) + int(completion_tokens or 0))),
    }
    logger.info(
        "LLM tokens: prompt=%s completion=%s total=%s",
        counted["prompt_tokens"],
        counted["completion_tokens"],
        counted["total_tokens"],
    )
    return counted


def _quote_matches(pages: list[PageText], page_number: int, quote: str) -> bool:
    page = next((item for item in pages if item.page == page_number), None)
    if page is not None and _norm(quote) in _norm(page.text):
        return True
    return _quote_on_page_seam(pages, quote, page_number)


def _quote_on_page_seam(pages: list[PageText], quote: str, page_number: Optional[int] = None) -> bool:
    normalized = _norm(quote)
    if len(normalized) < 12:
        return False
    ordered = sorted(pages, key=lambda item: item.page)
    for index in range(len(ordered) - 1):
        left = ordered[index]
        right = ordered[index + 1]
        if page_number is not None and page_number not in {left.page, right.page}:
            continue
        if normalized in _norm(f"{left.text} {right.text}"):
            return True
    return False


def _find_page(pages: list[PageText], quote: str) -> Optional[int]:
    normalized = _norm(quote)
    if len(normalized) < 12:
        return None
    for page in pages:
        if normalized in _norm(page.text):
            return page.page
    ordered = sorted(pages, key=lambda item: item.page)
    for index in range(len(ordered) - 1):
        if normalized in _norm(f"{ordered[index].text} {ordered[index + 1].text}"):
            return ordered[index].page
    return None


def _readable(value: str) -> str:
    cleaned = value.translate(
        str.maketrans(
            {
                "\u00ad": "-",
                "\u2010": "-",
                "\u2011": "-",
                "\u2012": "-",
                "\u2013": "-",
                "\u2014": "-",
                "\u2212": "-",
                "\u00a0": " ",
                "\u202f": " ",
            }
        )
    )
    return " ".join(cleaned.split())


def _without_marker(page_text: str, quote: str) -> str:
    stripped = re.sub(
        r"^(?:установил[аи]?|определил|решил|постановил)\s*:\s*",
        "",
        quote,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    if stripped and stripped in page_text:
        return stripped
    return quote.strip()


def _norm(value: str) -> str:
    compact = ocr_search_copy(value).replace("ё", "е").replace("Ё", "Е")
    compact = compact.translate(
        str.maketrans(
            {
                "\u00ad": "",
                "\u2010": "-",
                "\u2011": "-",
                "\u2013": "-",
                "\u2014": "-",
                "\u00a0": " ",
            }
        )
    )
    compact = re.sub(r"\s+", " ", compact)
    compact = re.sub(r"[^\w\s]", "", compact, flags=re.UNICODE)
    return compact.strip().lower()


def _response_format_rejected(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = ("response_format", "json_object", "not supported", "unsupported", "400")
    return any(marker in text for marker in markers)


def apply_layer3(parsed: PdfParseResult, analysis: DocumentAnalysisResult) -> list[str]:
    """Проверяет цитаты, пересчитывает даты и подставляет справку из базы норм."""

    _drop_noise(analysis)
    _drop_duplicate_deadlines(analysis)
    warnings = _verify_quotes(parsed.pages, analysis)
    _merge_regex_facts(parsed, analysis)
    _dedupe_parties(analysis)
    warnings.extend(_drop_unseen_parties(parsed, analysis))
    _ensure_stage(analysis, "\n".join(page.text for page in parsed.pages))
    _dedupe_legal_basis(analysis)
    _recalculate_due_dates(analysis)
    analysis.legal_knowledge = _KNOWLEDGE.build(analysis.stage_code, analysis.legal_basis)
    return warnings


def _sanitize_llm_payload(
    payload: dict,
    parsed: PdfParseResult,
    fallback: DocumentAnalysisResult,
) -> dict:
    """Приводит сырой JSON модели к полям, которые принимает DocumentAnalysisResult."""

    del parsed
    warnings: list[str] = []
    stage = _match_stage(payload.get("stage_code"))
    if stage is None:
        stage = fallback.stage_code
        case_stage = _filled_text(payload.get("case_stage"), fallback.case_stage)
    else:
        case_stage = _filled_text(payload.get("case_stage"), STAGE_TITLES[stage])
    return {
        "case_number": _canonical_or_fallback(
            payload.get("case_number"),
            fallback.case_number,
        ),
        "court_name": _filled_text(payload.get("court_name"), fallback.court_name),
        "judge_name": _optional_str(payload.get("judge_name")) or fallback.judge_name,
        "document_type": _coerce_document_type_soft(payload.get("document_type"), fallback.document_type),
        "document_date": _optional_str(payload.get("document_date")) or fallback.document_date,
        "stage_code": stage,
        "case_stage": case_stage,
        "act_date": _iso_or_none(payload.get("act_date")) or _iso_or_none(fallback.act_date),
        "parties": _clean_parties(payload.get("parties"), warnings),
        "verdict_summary": _filled_text(payload.get("verdict_summary"), fallback.verdict_summary),
        "financial_claims": _string_list(payload.get("financial_claims")),
        "next_hearing": _clean_hearing(payload.get("next_hearing")),
        "court_orders": _clean_orders(payload.get("court_orders"), warnings),
        "deadlines": _clean_deadlines(payload.get("deadlines"), warnings),
        "legal_basis": _string_list(payload.get("legal_basis")),
        "key_points": _clean_key_points(payload.get("key_points"), warnings),
        "legal_knowledge": None,
        "_sanitize_warnings": warnings,
    }


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return [item for item in value if item is not None]


def _string_list(value: object) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


def _canonical_or_fallback(value: object, fallback: str) -> str:
    chosen = _filled_text(value, fallback)
    if not chosen or chosen == "НЕ ИЗВЛЕЧЁН" or "<" in chosen:
        return chosen
    return canonical_case_number(chosen)


def _filled_text(value: object, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _optional_str(value: object) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _clean_parties(value: object, warnings: list[str]) -> list[dict[str, object]]:
    cleaned: list[dict[str, object]] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            warnings.append("Отброшен участник: элемент не является объектом")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            warnings.append("Отброшен участник: нет имени")
            continue
        party = {
            "role": _coerce_role(str(item.get("role") or "")),
            "name": name.strip(),
            "inn": _optional_str(item.get("inn")),
            "ogrn": _optional_str(item.get("ogrn")),
            "snils": _optional_str(item.get("snils")),
            "address": _optional_str(item.get("address")),
        }
        _keep_model(party, PartyInfo, cleaned, warnings, "участник")
    return cleaned


def _clean_key_points(value: object, warnings: list[str]) -> list[dict[str, object]]:
    cleaned: list[dict[str, object]] = []
    for item in _as_list(value):
        quote = _clean_quote(item, None)
        if quote is None:
            warnings.append("Отброшен key_point: пустая цитата")
            continue
        _keep_model(quote, QuoteReference, cleaned, warnings, "key_point")
    return cleaned


def _clean_deadlines(value: object, warnings: list[str]) -> list[dict[str, object]]:
    cleaned: list[dict[str, object]] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            warnings.append("Отброшен срок: элемент не является объектом")
            continue
        description = _optional_str(item.get("description")) or _optional_str(item.get("deadline_text"))
        days = _parse_days_limit(item.get("days_limit"))
        months = _parse_days_limit(item.get("months_limit"))
        if days is None:
            days = 0
        quote = _clean_quote(item.get("source_quote"), description)
        if description is None or quote is None:
            warnings.append("Отброшен срок: нет описания или цитаты")
            continue
        data = {
            "description": description,
            "due_date": _iso_or_none(item.get("due_date")),
            "days_limit": days,
            "months_limit": months,
            "days_type": _coerce_days_type(item.get("days_type")),
            "anchor_event": _coerce_anchor(item.get("anchor_event")),
            "urgency": _coerce_urgency(item.get("urgency"), days, months),
            "source_quote": quote,
        }
        _keep_model(data, DeadlineItem, cleaned, warnings, "срок")
    return cleaned


def _clean_orders(value: object, warnings: list[str]) -> list[dict[str, object]]:
    cleaned: list[dict[str, object]] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            warnings.append("Отброшено поручение суда: элемент не является объектом")
            continue
        action = _optional_str(item.get("action"))
        quote = _clean_quote(item.get("source_quote"), action or _optional_str(item.get("deadline_text")))
        if action is None or quote is None:
            warnings.append("Отброшено поручение суда: нет действия или цитаты")
            continue
        days = _parse_days_limit(item.get("days_limit"))
        actor = _optional_str(item.get("actor")) or "Лица участвующие в деле"
        if actor.lower() in {"не указан", "неизвестно", "none", "null"}:
            actor = "Лица участвующие в деле"
        data = {
            "actor": actor,
            "action": action,
            "deadline_text": _optional_str(item.get("deadline_text")),
            "days_limit": days,
            "months_limit": _parse_days_limit(item.get("months_limit")),
            "days_type": _coerce_days_type(item.get("days_type")),
            "anchor_event": _coerce_anchor(item.get("anchor_event")),
            "due_date": _iso_or_none(item.get("due_date")),
            "source_quote": quote,
        }
        _keep_model(data, CourtOrderItem, cleaned, warnings, "поручение суда")
    return cleaned


def _clean_quote(value: object, backup: Optional[str]) -> Optional[dict[str, object]]:
    exact: object = None
    page_raw: object = 1
    if isinstance(value, str):
        exact = value
    elif isinstance(value, dict):
        exact = value.get("exact_quote")
        if exact is None:
            exact = value.get("quote")
        page_raw = value.get("page_number")
    text = exact.strip() if isinstance(exact, str) else ""
    if not text:
        text = backup.strip() if isinstance(backup, str) else ""
    if not text:
        return None
    return {"page_number": _page_number(page_raw), "exact_quote": text[:2000], "verified": True}


def _page_number(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 1
    if isinstance(value, int):
        return value if value >= 1 else 1
    if isinstance(value, float):
        number = int(value)
        return number if number >= 1 else 1
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            number = int(match.group(0))
            return number if number >= 1 else 1
    return 1


def _clean_hearing(value: object) -> Optional[dict[str, object]]:
    if not isinstance(value, dict) or not value:
        return None
    date_value = _iso_or_none(value.get("date"))
    time_value = _optional_str(value.get("time"))
    location = _optional_str(value.get("location"))
    purpose = _optional_str(value.get("purpose"))
    quote = _clean_quote(value.get("source_quote"), None)
    if date_value is None and time_value is None and location is None and purpose is None and quote is None:
        return None
    hearing = NextHearingInfo(
        date=date_value,
        time=time_value,
        location=location,
        purpose=purpose,
        source_quote=QuoteReference.model_validate(quote) if quote else None,
    )
    return hearing.model_dump()


def _keep_model(
    data: dict[str, object],
    model: type[PartyInfo] | type[QuoteReference] | type[DeadlineItem] | type[CourtOrderItem],
    sink: list[dict[str, object]],
    warnings: list[str],
    label: str,
) -> None:
    try:
        sink.append(model.model_validate(data).model_dump())
    except ValidationError as exc:
        warnings.append(f"Отброшен {label}: {_validation_field_list(exc)}")


def _match_stage(value: object) -> Optional[StageCode]:
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip()
    if token in _STAGE_CODES:
        return token  # type: ignore[return-value]
    lowered = token.lower().replace(" ", "_").replace("-", "_")
    if lowered in _STAGE_CODES:
        return lowered  # type: ignore[return-value]
    for code, title in STAGE_TITLES.items():
        if title.lower() in token.lower() or code in lowered:
            return code
    markers = (
        "субсидиар",
        "оспариван",
        "реестр",
        "реструктуризац",
        "реализац",
        "наблюден",
        "конкурсн",
        "банкрот",
        "61.2",
        "61.3",
    )
    if any(marker in token.lower() for marker in markers):
        return _classify_stage(token)[0]
    return None


def _coerce_document_type_soft(value: object, fallback: DocumentType) -> DocumentType:
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        return _coerce_document_type(value)
    except ValueError:
        return fallback


def _parse_days_limit(value: object) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            return int(match.group(0))
        lowered = value.lower().replace("ё", "е")
        for word, number in _WORD_DAYS.items():
            if re.search(rf"\b{word}\b", lowered):
                return number
    return None


def _coerce_days_type(value: object) -> str:
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"calendar", "business", "unknown"}:
            return lowered
        if "рабоч" in lowered:
            return "business"
        if "календар" in lowered:
            return "calendar"
        if "неизв" in lowered or "unknown" in lowered:
            return "unknown"
    return "calendar"


def _coerce_anchor(value: object) -> str:
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"act_date", "receipt_date", "hearing_date", "unknown"}:
            return lowered
        if "получен" in lowered or "receipt" in lowered:
            return "receipt_date"
        if "заседан" in lowered or "hearing" in lowered:
            return "hearing_date"
        if "fixed" in lowered or "прям" in lowered or "конечн" in lowered:
            return "fixed_date"
        if "акт" in lowered or "вынес" in lowered:
            return "act_date"
        if lowered.strip():
            return "unknown"
    return "act_date"


def _coerce_urgency(value: object, days: int, months: Optional[int] = None) -> str:
    if isinstance(months, int) and months >= 1:
        return "normal"
    if days > 10:
        return "normal"
    if 1 <= days <= 10:
        return "critical"
    if isinstance(value, str) and value.strip().lower() == "critical" and days == 0:
        return "critical"
    return "normal"


def _public_llm_text(text: str) -> str:
    redacted = re.sub(r"sk-[A-Za-z0-9_\-]+", "[key]", text)
    redacted = re.sub(r"AQVN[A-Za-z0-9_\-]+", "[key]", redacted)
    return redacted[:1500]


def _validation_field_list(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(item) for item in err['loc'])}: {err['type']}" for err in exc.errors()]
    return "; ".join(parts)[:1500]


def _coerce_role(value: str) -> PartyRole:
    if value in _ROLES:
        return value  # type: ignore[return-value]
    lowered = value.lower().replace("ё", "е")
    if "саморегулир" in lowered or re.search(r"(^|[^а-яёa-z])сро([^а-яёa-z]|$)", lowered):
        return "СРО АУ"
    if lowered in {"ау", "а/у", "фу"} or "управля" in lowered:
        return "Финансовый управляющий"
    if "кред" in lowered:
        return "Кредитор"
    if "ответ" in lowered:
        return "Ответчик"
    if "ист" in lowered:
        return "Истец"
    if "заявит" in lowered or "долж" in lowered or "граждан" in lowered:
        return "Должник"
    return "Третье лицо"


def _classify_stage(text: str) -> tuple[StageCode, str]:
    lowered = re.sub(r"\s+", " ", text.lower().replace("ё", "е"))
    if "субсидиар" in lowered:
        code: StageCode = "subsidiary_liability"
    elif ("оспариван" in lowered and "сдел" in lowered) or "61.2" in lowered or "61.3" in lowered:
        code = "transaction_challenge"
    elif "реестр требований" in lowered or "включить требование" in lowered:
        code = "creditor_registry_claim"
    elif "реструктуризац" in lowered and "долг" in lowered:
        code = "bankruptcy_individual_restructuring"
    elif ("реализац" in lowered and "имуществ" in lowered) or "признать банкротом" in lowered or "признан банкротом" in lowered:
        code = "bankruptcy_individual_realization"
    elif any(token in lowered for token in ("наблюден", "конкурсн", "внешн управлен")):
        code = "bankruptcy_legal_entity"
    elif "банкрот" in lowered and any(token in lowered for token in ("гражданин", "физическ", "должник")):
        if any(token in lowered for token in ("ооо", "ао ", "пао", "юридическ")):
            code = "bankruptcy_legal_entity"
        elif "принять заявление" in lowered or "принят" in lowered or "без движения" in lowered:
            code = "bankruptcy_individual_accept"
        else:
            code = "bankruptcy_individual_realization"
    else:
        code = "general_arbitration_dispute"
    return code, STAGE_TITLES[code]


def _ensure_stage(analysis: DocumentAnalysisResult, text: str) -> None:
    if analysis.stage_code == "bankruptcy_individual_intro" or analysis.stage_code not in _STAGE_CODES:
        analysis.stage_code, _title = _classify_stage(text)
        if analysis.stage_code == "general_arbitration_dispute" and "банкрот" in text.lower():
            analysis.stage_code = "bankruptcy_individual_realization"
    elif analysis.stage_code == "general_arbitration_dispute":
        code, _title = _classify_stage(text)
        if code != "general_arbitration_dispute":
            analysis.stage_code = code
    analysis.case_stage = STAGE_TITLES[analysis.stage_code]


def _merge_regex_facts(parsed: PdfParseResult, analysis: DocumentAnalysisResult) -> None:
    metadata = parsed.metadata
    full_text = "\n".join(page.text for page in parsed.pages)
    normalized = _norm(full_text)
    if metadata.case_number and (
        analysis.case_number in {"", "НЕ ИЗВЛЕЧЁН"}
        or "<" in analysis.case_number
        or _norm(analysis.case_number) not in normalized
    ):
        analysis.case_number = metadata.case_number
    if analysis.case_number and "<" not in analysis.case_number and analysis.case_number != "НЕ ИЗВЛЕЧЁН":
        analysis.case_number = canonical_case_number(analysis.case_number)
    if metadata.act_date and not analysis.act_date:
        analysis.act_date = metadata.act_date
    if metadata.court_name and (
        analysis.court_name in {"", "Суд не указан в текстовом слое"}
        or "<" in analysis.court_name
        or _norm(analysis.court_name) not in normalized
    ):
        analysis.court_name = metadata.court_name
    if metadata.judge_name and not analysis.judge_name:
        analysis.judge_name = metadata.judge_name
    for party in metadata.parties:
        if any(_same_party(party.name, existing.name) for existing in analysis.parties):
            continue
        if _party_seen(party.name, normalized):
            analysis.parties.append(party)
    _attach_nearby_ids(full_text, analysis, metadata.inns, metadata.snils_list)
    for name in metadata.sro_names:
        if any(name in party.name for party in analysis.parties):
            continue
        analysis.parties.append(PartyInfo(role="СРО АУ", name=name))


def _drop_unseen_parties(parsed: PdfParseResult, analysis: DocumentAnalysisResult) -> list[str]:
    """Убирает стороны, которых нет в тексте: модель иногда копирует их из примера JSON."""

    normalized = _norm("\n".join(page.text for page in parsed.pages))
    kept: list[PartyInfo] = []
    warnings: list[str] = []
    for party in analysis.parties:
        if _party_seen(party.name, normalized):
            kept.append(party)
            continue
        warnings.append(f"Участник отброшен: имя не найдено в тексте PDF: {party.name}")
    analysis.parties = kept
    return warnings


def _same_party(left: str, right: str) -> bool:
    stems_left = _name_stems(left)
    stems_right = _name_stems(right)
    if not stems_left or not stems_right:
        return _norm(left) == _norm(right) and bool(_norm(left))
    return any(stem_matches(left_stem, right_stem) for left_stem in stems_left for right_stem in stems_right)


def _name_stems(name: str) -> list[str]:
    stems: list[str] = []
    for token in _norm(name).split():
        if token in {"ооо", "ао", "пао", "зао", "ип"}:
            continue
        if len(token) >= 5:
            stems.append(token[:-2])
        elif len(token) >= 4:
            stems.append(token)
    return stems


def stem_matches(left: str, right: str) -> bool:
    if len(left) < 4 or len(right) < 4:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


_ROLE_RANK = {
    "Финансовый управляющий": 4,
    "СРО АУ": 3,
    "Должник": 2,
    "Кредитор": 2,
    "Истец": 2,
    "Ответчик": 2,
    "Третье лицо": 1,
}
_NOISE_MARKERS = (
    "бумажном носителе",
    "пятидневный срок со дня поступления ходатайства",
    "до даты подачи",
    "до подачи заявления",
)


def _is_organization(party: PartyInfo) -> bool:
    if party.role == "СРО АУ":
        return True
    lowered = f" {_norm(party.name)} "
    return any(mark in lowered for mark in (" ооо ", " ао ", " пао ", " зао ", " ассоциац", " саморегулир"))


def _id_near_name(lowered: str, name: str, identifier: str) -> bool:
    needles = _name_stems(name) or ([_norm(name)] if _norm(name) else [])
    for needle in needles:
        if len(needle) < 4:
            continue
        start = 0
        while True:
            index = lowered.find(needle, start)
            if index < 0:
                break
            window = lowered[max(0, index - 200): index + len(needle) + 200]
            if identifier in window.replace(" ", ""):
                return True
            start = index + len(needle)
    return False


def _attach_nearby_ids(
    text: str,
    analysis: DocumentAnalysisResult,
    inns: list[str],
    snils_list: list[str],
) -> None:
    lowered = ocr_search_copy(text).lower().replace("ё", "е")
    for party in analysis.parties:
        for inn in inns:
            if party.inn:
                break
            if len(inn) == 12 and (party.role == "СРО АУ" or _is_organization(party)):
                continue
            if _id_near_name(lowered, party.name, inn):
                party.inn = inn
        for snils in snils_list:
            if party.snils or party.role == "СРО АУ" or _is_organization(party):
                break
            if _id_near_name(lowered, party.name, snils):
                party.snils = snils
    individuals = [
        party for party in analysis.parties
        if party.role == "Должник" and not _is_organization(party) and not party.inn
    ]
    personal = [inn for inn in inns if len(inn) == 12]
    if len(individuals) == 1 and len(personal) == 1:
        individuals[0].inn = personal[0]


def _dedupe_parties(analysis: DocumentAnalysisResult) -> None:
    kept: list[PartyInfo] = []
    for party in analysis.parties:
        match = next((item for item in kept if _same_party(item.name, party.name)), None)
        if match is None:
            kept.append(party)
            continue
        winner = party if _ROLE_RANK.get(party.role, 0) > _ROLE_RANK.get(match.role, 0) else match
        donor = match if winner is party else party
        winner.inn = winner.inn or donor.inn
        winner.snils = winner.snils or donor.snils
        winner.ogrn = winner.ogrn or donor.ogrn
        winner.address = winner.address or donor.address
        if winner is party:
            kept[kept.index(match)] = party
    analysis.parties = kept


def _drop_duplicate_deadlines(analysis: DocumentAnalysisResult) -> None:
    """Срок, который уже есть в поручении с исполнителем, в deadlines не повторяем."""

    analysis.deadlines = [
        item for item in analysis.deadlines
        if not any(_same_order_and_deadline(item, order) for order in analysis.court_orders)
    ]


def _same_order_and_deadline(deadline: DeadlineItem, order: CourtOrderItem) -> bool:
    quote = _norm(deadline.source_quote.exact_quote)
    order_quote = _norm(order.source_quote.exact_quote)
    if quote and quote == order_quote:
        return True
    if deadline.anchor_event != order.anchor_event:
        return False
    if (deadline.days_limit or 0) != (order.days_limit or 0):
        return False
    if (deadline.months_limit or 0) != (order.months_limit or 0):
        return False
    if not (deadline.days_limit or deadline.months_limit or deadline.anchor_event == "fixed_date"):
        return False
    return _task_substance_matches(deadline.description, order.action)


def _task_substance_matches(description: str, action: str) -> bool:
    left = _norm(description)
    right = _norm(action)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    stems = [token[:-2] for token in left.split() if len(token) >= 6]
    return any(len(stem) >= 5 and stem in right for stem in stems)


def _drop_noise(analysis: DocumentAnalysisResult) -> None:
    def noisy(text: str) -> bool:
        lowered = text.lower().replace("ё", "е")
        return any(marker in lowered for marker in _NOISE_MARKERS)

    analysis.deadlines = [item for item in analysis.deadlines if not noisy(item.description)]
    analysis.court_orders = [
        item for item in analysis.court_orders
        if not noisy(f"{item.action} {item.deadline_text or ''}")
    ]
    points = []
    for point in analysis.key_points:
        quote = point.exact_quote.lower()
        if "kad.arbitr" in quote:
            continue
        if "электронн" in quote and "подпис" in quote:
            continue
        points.append(point)
    analysis.key_points = points[:3]


def _dedupe_legal_basis(analysis: DocumentAnalysisResult) -> None:
    seen: set[str] = set()
    cleaned: list[str] = []
    for note in analysis.legal_basis:
        fixed = note.replace("(банкротстве»)", "(банкротстве)»").replace(
            "«О несостоятельности (банкротстве»)",
            "«О несостоятельности (банкротстве)»",
        )
        key = _norm(fixed)
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(fixed)
    analysis.legal_basis = cleaned


def _party_seen(name: str, normalized_text: str) -> bool:
    normalized = _norm(name)
    if len(normalized) >= 4 and normalized in normalized_text:
        return True
    stop = {
        "ооо",
        "оао",
        "пао",
        "ао",
        "ип",
        "зао",
        "общество",
        "ограниченной",
        "ответственностью",
        "акционерное",
    }
    tokens = [token for token in normalized.split() if len(token) >= 4 and token not in stop]
    for token in tokens:
        if token in normalized_text:
            return True
        # «Иванова» → «иванов» находится и в «Ивановой», и в «Иванова».
        if len(token) >= 5 and token[:-2] in normalized_text:
            return True
    return False


def _recalculate_due_dates(analysis: DocumentAnalysisResult) -> None:
    anchor = analysis.act_date or _to_iso_date(analysis.document_date)
    for item in analysis.deadlines:
        _align_days_type(item)
        _apply_due_date(item, anchor)
    for item in analysis.court_orders:
        _align_days_type(item)
        _apply_due_date(item, anchor)


def _align_days_type(item: DeadlineItem | CourtOrderItem) -> None:
    quote = item.source_quote.exact_quote.lower().replace("ё", "е")
    if "рабоч" in quote:
        item.days_type = "business"
    elif "календар" in quote:
        item.days_type = "calendar"


def calculate_due_date(
    start_iso: str,
    days_limit: int,
    days_type: str,
    months_limit: int = 0,
) -> Optional[str]:
    """Считает срок от даты. Конец в выходной переносится на понедельник, ч. 4 ст. 114 АПК РФ."""

    start = _parse_date(start_iso)
    if start is None:
        return None
    if months_limit > 0:
        result = _add_months(start, months_limit)
        return _shift_weekend(result).isoformat()
    if days_limit < 0 or days_type == "unknown":
        return None
    if days_type == "business":
        return _add_business_days(start, days_limit).isoformat()
    return _shift_weekend(start + timedelta(days=days_limit)).isoformat()


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, monthrange(year, month)[1])
    return date(year, month, day)


def _shift_weekend(value: date) -> date:
    if value.weekday() == 5:
        return value + timedelta(days=2)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _apply_due_date(item: DeadlineItem | CourtOrderItem, anchor: Optional[str]) -> None:
    if item.anchor_event == "fixed_date" and item.due_date:
        item.due_date = _to_iso_date(item.due_date) or item.due_date
        return
    if item.anchor_event == "receipt_date":
        item.due_date = None
        return
    if item.anchor_event != "act_date" or not anchor:
        if item.anchor_event != "fixed_date":
            return
        item.due_date = None
        return
    months = item.months_limit or 0
    if months > 0:
        item.due_date = calculate_due_date(anchor, 0, "calendar", months)
        return
    if item.days_limit is None or item.days_type == "unknown":
        item.due_date = None
        return
    item.due_date = calculate_due_date(anchor, item.days_limit, item.days_type)


def _add_business_days(start: date, days: int) -> date:
    current = start
    left = days
    while left > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            left -= 1
    return current


def _next_hearing(pages: list[PageText]) -> Optional[NextHearingInfo]:
    for page in pages:
        for sentence in _sentences(page.text):
            if not re.search(r"заседани", sentence, re.IGNORECASE):
                continue
            date_match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", sentence)
            textual = re.search(
                r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})",
                sentence,
                re.IGNORECASE,
            )
            if date_match is None and textual is None:
                continue
            quote = _exact_substring(page.text, sentence)
            if quote is None:
                continue
            time_match = re.search(r"\b(\d{1,2}:\d{2})\b", sentence)
            room_match = re.search(r"((?:зал|кабинет)\w*\s*№?\s*\d+)", sentence, re.IGNORECASE)
            if date_match:
                hearing_date = _to_iso_date(date_match.group(1))
            else:
                assert textual is not None
                hearing_date = _to_iso_date(
                    f"{int(textual.group(1)):02d}.{_month_number(textual.group(2))}.{textual.group(3)}"
                )
            return NextHearingInfo(
                date=hearing_date,
                time=time_match.group(1) if time_match else None,
                location=room_match.group(1) if room_match else None,
                purpose="Судебное заседание",
                source_quote=QuoteReference(page_number=page.page, exact_quote=quote[:700]),
            )
    return None


def _court_orders(pages: list[PageText]) -> list[CourtOrderItem]:
    orders: list[CourtOrderItem] = []
    for page in pages:
        section = _between(page.text, ("определил", "решил", "постановил"), ("судья",))
        parts = re.split(r"(?:^|\n)\s*\d+[\.\)]\s+", section)
        if len(parts) <= 1:
            continue
        for part in parts[1:]:
            sentences = _sentences(part)
            sentence = sentences[0] if sentences else " ".join(part.split())
            if len(sentence) < 20:
                continue
            quote = _exact_substring(page.text, sentence[:350])
            if quote is None:
                continue
            has_days = _has_day_phrase(sentence)
            receipt = re.search(r"получен", sentence, re.IGNORECASE) is not None
            orders.append(
                CourtOrderItem(
                    actor=_actor(sentence),
                    action=sentence[:500],
                    deadline_text=sentence[:300] if has_days else None,
                    days_limit=_days_near(sentence, 0) if has_days else None,
                    days_type="business" if "рабоч" in sentence.lower() else "calendar",
                    anchor_event="receipt_date" if receipt else "act_date",
                    source_quote=QuoteReference(page_number=page.page, exact_quote=quote[:700]),
                )
            )
            if len(orders) >= 8:
                return orders
    return orders


def _actor(text: str) -> str:
    lowered = text.lower()
    if re.search(r"финансовому управляющему|финансовый управляющий|арбитражному управляющему", lowered):
        return "Финансовый управляющий"
    if re.search(r"\bсро\b|саморегулир|союз", lowered):
        return "СРО"
    if "должник" in lowered:
        return "Должник"
    if "кредитор" in lowered:
        return "Кредитор"
    return "Лица участвующие в деле"


def _has_day_phrase(text: str) -> bool:
    if re.search(r"\d+\s+(?:рабочих\s+|календарных\s+)?дн", text, re.IGNORECASE):
        return True
    lowered = text.lower().replace("ё", "е")
    return any(re.search(rf"\b{word.replace('ё', 'е')}\b", lowered) for word in _WORD_DAYS)


_MONTH_INDEX = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}


def _month_number(name: str) -> str:
    return f"{_MONTH_INDEX[name.lower().replace('ё', 'е')]:02d}"


def _iso_to_ru(value: Optional[str]) -> Optional[str]:
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return parsed.strftime("%d.%m.%Y")


def _iso_or_none(value: object) -> Optional[str]:
    return _to_iso_date(_optional_str(value))


def _to_iso_date(value: Optional[str]) -> Optional[str]:
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return parsed.isoformat()


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    text = " ".join(value.replace("ё", "е").split())
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    dotted = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", text)
    if dotted:
        return _safe_date(int(dotted.group(3)), int(dotted.group(2)), int(dotted.group(1)))
    textual = re.search(r"\b(\d{1,2})\s+([а-яА-ЯеЁ]+)\s+(\d{4})(?:\s+года)?\b", text)
    if textual is None:
        return None
    month = _MONTH_INDEX.get(textual.group(2).lower().replace("ё", "е"))
    if month is None:
        return None
    return _safe_date(int(textual.group(3)), month, int(textual.group(1)))


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "sk-" in text or "api key" in lowered or "api_key" in lowered:
        return exc.__class__.__name__
    if "permissiondenied" in lowered or "permission denied" in lowered or " 403" in lowered:
        return (
            "PermissionDeniedError: у ключа нет права вызывать модель в каталоге Yandex Cloud. "
            "Нужна роль ai.languageModels.user на каталог из OPENAI_PROJECT"
        )
    compact = " ".join(text.split())
    if not compact:
        return exc.__class__.__name__
    return f"{exc.__class__.__name__}: {compact[:240]}"
