"""LLM-экстрактор реквизитов, резолюции и сроков. Без ключа работает fallback."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI

from app.models.schemas import (
    DeadlineItem,
    DocumentAnalysisResult,
    DocumentType,
    ExtractionOutcome,
    PageText,
    PdfParseResult,
    QuoteReference,
)

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
    "трёх": 3,
    "десять": 10,
    "десяти": 10,
    "тридцать": 30,
    "тридцати": 30,
}
_SYSTEM_PROMPT = """Ты — профессиональный юрист-арбитражник и помощник арбитражного управляющего.
Извлеки из текста судебного акта только проверяемые факты и верни один JSON-объект.

Правила:
1. Никакого текста вне JSON. Не пересказывай цитаты.
2. exact_quote — дословный фрагмент из блока [PAGE N], без многоточий и без правки слов.
3. page_number — номер N из метки [PAGE N], где лежит эта цитата.
4. Не выдумывай номер дела, суд, судью, стороны, суммы и статьи. Нет данных — null или пустой список.
5. verdict_summary — 1–2 предложения строго по резолютивной части (после ОПРЕДЕЛИЛ / РЕШИЛ / ПОСТАНОВИЛ).
6. document_type — одно из значений: Определение, Решение, Постановление, Требование кредитора, Выписка.
7. role участника — одно из: Истец, Ответчик, Должник, Кредитор, Третье лицо.
8. financial_claims — строки вида «Кто → Кому: сумма».
9. legal_basis — только нормы, прямо названные в тексте (ГК РФ, АПК РФ, ФЗ-127 «О банкротстве»).
10. deadlines:
   - публикация на ЕФРСБ / Федресурсе: days_limit обычно 3, urgency = critical;
   - апелляция на определение: обычно 10 дней, urgency = critical;
   - апелляция на решение: обычно 30 дней, urgency = normal.
   due_date — YYYY-MM-DD от даты акта, если дату удалось прочитать, иначе null.
11. key_points — тезисы мотивировки, не резолюция. Каждый тезис обязан иметь exact_quote.
12. Подсказки regex_hints вспомогательные: при расхождении с текстом страницы верь тексту страницы.
"""


class AILegalExtractor:
    """Структурирует судебный акт через OpenAI-совместимый API или локальные regex."""

    def __init__(self) -> None:
        load_dotenv()
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def extract(self, parsed: PdfParseResult) -> ExtractionOutcome:
        fallback = build_fallback_analysis(parsed)
        if not self.is_configured:
            warnings = ["OPENAI_API_KEY не задан: карточка собрана регулярными выражениями, без LLM."]
            warnings.extend(_fallback_warnings(parsed, fallback))
            return ExtractionOutcome(analysis=fallback, extraction_mode="fallback", warnings=warnings)

        try:
            analysis = self._call_llm(parsed)
        except Exception as exc:
            logger.exception("LLM extraction failed")
            warnings = [
                f"LLM недоступен ({_safe_error(exc)}). Карточка собрана регулярными выражениями.",
                *_fallback_warnings(parsed, fallback),
            ]
            return ExtractionOutcome(analysis=fallback, extraction_mode="fallback", warnings=warnings)

        warnings = _verify_quotes(parsed.pages, analysis)
        return ExtractionOutcome(analysis=analysis, extraction_mode="llm", warnings=warnings)

    def _call_llm(self, parsed: PdfParseResult) -> DocumentAnalysisResult:
        client = OpenAI(**self._client_kwargs())
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Верни JSON по схеме DocumentAnalysisResult.\n"
                    f"Схема:\n{json.dumps(DocumentAnalysisResult.model_json_schema(), ensure_ascii=False)}\n\n"
                    f"regex_hints:\n{parsed.metadata.model_dump_json()}\n\n"
                    f"Текст документа:\n{_prompt_text(parsed.pages)}"
                ),
            },
        ]
        completion = self._complete(client, messages)
        content = completion.choices[0].message.content or ""
        payload = _unwrap_payload(_load_json(content))
        if "document_type" in payload:
            payload["document_type"] = _coerce_document_type(str(payload["document_type"]))
        return DocumentAnalysisResult.model_validate(payload)

    def _client_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {"api_key": self.api_key, "timeout": 90.0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
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
            document_date=metadata.dates[0] if metadata.dates else None,
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
    document_date = metadata.dates[0] if metadata.dates else None
    verdict = _verdict_summary(pages, document_type)
    return DocumentAnalysisResult(
        case_number=metadata.case_number or "НЕ ИЗВЛЕЧЁН",
        court_name=metadata.court_name or "Суд не указан в текстовом слое",
        judge_name=metadata.judge_name,
        document_type=document_type,
        document_date=document_date,
        parties=metadata.parties,
        verdict_summary=verdict,
        financial_claims=_financial_claims(metadata.parties, pages),
        deadlines=_deadlines(pages, document_type, document_date),
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
        items.append(
            DeadlineItem(
                description="Публикация на ЕФРСБ"
                if "рабоч" not in publication.exact_quote.lower()
                else "Публикация на ЕФРСБ (срок в рабочих днях; дата посчитана календарно)",
                due_date=_shift_date(document_date, days),
                days_limit=days,
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
                due_date=_shift_date(document_date, days),
                days_limit=days,
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
    chunks: list[str] = []
    total = 0
    for page in pages[:40]:
        body = page.text.strip() or "[пустая страница или скан без текстового слоя]"
        if len(body) > 8000:
            body = body[:8000] + "\n…[страница обрезана]"
        piece = f"[PAGE {page.page}]\n{body}"
        if total + len(piece) > 100_000:
            chunks.append("…[дальнейшие страницы опущены из-за лимита контекста]")
            break
        chunks.append(piece)
        total += len(piece)
    return "\n\n".join(chunks)


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
    for reference in references:
        if _quote_matches(pages, reference.page_number, reference.exact_quote):
            continue
        relocated = _find_page(pages, reference.exact_quote)
        if relocated is not None:
            reference.page_number = relocated
            continue
        warnings.append(
            "Цитата не найдена дословно в тексте PDF: "
            f"«{reference.exact_quote[:120]}»"
        )
    return warnings


def _quote_matches(pages: list[PageText], page_number: int, quote: str) -> bool:
    page = next((item for item in pages if item.page == page_number), None)
    if page is None:
        return False
    return _norm(quote) in _norm(page.text)


def _find_page(pages: list[PageText], quote: str) -> Optional[int]:
    normalized = _norm(quote)
    if len(normalized) < 12:
        return None
    for page in pages:
        if normalized in _norm(page.text):
            return page.page
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
    compact = re.sub(r"\s+", " ", value).replace("ё", "е").replace("Ё", "Е")
    return compact.strip().lower()


def _response_format_rejected(exc: Exception) -> bool:
    text = str(exc).lower()
    return "response_format" in text or "json_object" in text


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    if "sk-" in text or "api key" in text.lower() or "api_key" in text.lower():
        return exc.__class__.__name__
    compact = " ".join(text.split())
    if not compact:
        return exc.__class__.__name__
    return f"{exc.__class__.__name__}: {compact[:240]}"
