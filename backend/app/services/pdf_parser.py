"""Локальный разбор судебных PDF: текст по страницам и быстрые реквизиты."""

from __future__ import annotations

import re
from typing import Optional

import pymupdf as fitz

from app.models.schemas import (
    DocumentType,
    PageText,
    PartyInfo,
    PartyRole,
    PdfParseResult,
    RegexMetadata,
)

class PdfParseError(Exception):
    """PDF не удалось прочитать или он не похож на документ суда."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# Дефис в PDF бывает обычным, неразрывным или мягким (U+00AD).
_DASH = r"[\-\u00ad\u2010\u2011\u2012\u2013\u2014\u2212]"
# А40-12345/2026 и редкая форма А-40-12345/2026, кириллическая или латинская «А».
_CASE_NUMBER_RE = re.compile(
    rf"(?<![0-9A-Za-zА-Яа-яЁё])"
    rf"([АA]\d{{2}}{_DASH}\d+/\d{{4}}|[АA]{_DASH}\d{{2}}{_DASH}\d+/\d{{4}})",
    re.IGNORECASE,
)
_ANY_DATE_RE = re.compile(
    r"\b(\d{2}\.\d{2}\.\d{4})\b"
    r"|\b(\d{1,2})\s+"
    r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"\s+(\d{4})\b",
    re.IGNORECASE,
)
_INN_RE = re.compile(r"(?:ИНН|инн)\s*[:№]?\s*(\d{10}|\d{12})")
_OGRN_RE = re.compile(r"(?:ОГРН(?:ИП)?|огрн(?:ип)?)\s*[:№]?\s*(\d{13}|\d{15})")
_JUDGE_RE = re.compile(
    r"(?:судья|судьи)\s+"
    r"([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2}|[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)",
    re.IGNORECASE,
)
_PARTY_RE = re.compile(
    r"(?i:(?<![а-яёa-z])"
    r"(истца|истец|истцу|истцом|"
    r"ответчика|ответчик|ответчику|ответчиком|"
    r"должника|должник|должнику|должником|"
    r"кредитора|кредитор|кредитору|кредитором|"
    r"третьего лица|третье лицо|третьему лицу|третьим лицом)"
    r"(?![а-яёa-z]))"
    r"\s*[:\-–]?\s*"
    r"("
    r"(?:ООО|АО|ПАО|ЗАО|ОАО|ИП|ФГУП|ГУП)\s+[«\"“][^»\"”\n]{2,160}[»\"”]"
    r"|[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,3}"
    r")"
)
_MONTHS = {
    "января": "01",
    "февраля": "02",
    "марта": "03",
    "апреля": "04",
    "мая": "05",
    "июня": "06",
    "июля": "07",
    "августа": "08",
    "сентября": "09",
    "октября": "10",
    "ноября": "11",
    "декабря": "12",
}
_DASH_TO_HYPHEN = str.maketrans(
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


def _tidy(value: str) -> str:
    return " ".join(value.translate(_DASH_TO_HYPHEN).split())


def _optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = _tidy(value)
    return cleaned or None


def _canonical_case_number(value: str) -> str:
    cleaned = _tidy(value)
    first = cleaned[0]
    if first in {"A", "a", "А", "а"}:
        first = "А"
    return first + cleaned[1:]


class SmartPDFParser:
    """Извлекает постраничный текст и реквизиты первой страницы без LLM."""

    def parse(self, file_bytes: bytes) -> PdfParseResult:
        if not file_bytes:
            raise PdfParseError("Пустой файл")
        if not file_bytes.startswith(b"%PDF"):
            raise PdfParseError("Файл не является PDF")

        try:
            document = fitz.open(stream=file_bytes, filetype="pdf")
        except Exception as exc:
            raise PdfParseError(f"Не удалось открыть PDF: {exc}") from exc

        try:
            if document.needs_pass:
                raise PdfParseError("PDF защищён паролем и не может быть прочитан")
            if document.page_count == 0:
                raise PdfParseError("PDF не содержит страниц")

            pages = [
                PageText(page=index, text=page.get_text("text") or "")
                for index, page in enumerate(document, start=1)
            ]
        finally:
            document.close()

        return PdfParseResult(pages=pages, metadata=self._extract_metadata(pages))

    def _extract_metadata(self, pages: list[PageText]) -> RegexMetadata:
        first_page = pages[0].text if pages else ""
        full_text = "\n".join(page.text for page in pages)

        case_number = self._first_group(_CASE_NUMBER_RE, first_page)
        if case_number is None:
            case_number = self._first_group(_CASE_NUMBER_RE, full_text)
        if case_number is not None:
            case_number = _canonical_case_number(case_number)

        return RegexMetadata(
            case_number=case_number,
            dates=self._dates(first_page) or self._dates(full_text),
            inns=self._unique(_INN_RE.findall(first_page) or _INN_RE.findall(full_text)),
            ogrns=self._unique(_OGRN_RE.findall(first_page) or _OGRN_RE.findall(full_text)),
            court_name=_optional_text(self._court_name(first_page) or self._court_name(full_text)),
            judge_name=_optional_text(
                self._first_group(_JUDGE_RE, first_page) or self._first_group(_JUDGE_RE, full_text)
            ),
            document_type=self._document_type(first_page) or self._document_type(full_text),
            parties=self._parties(full_text),
        )

    @staticmethod
    def _first_group(pattern: re.Pattern[str], text: str) -> Optional[str]:
        match = pattern.search(text)
        if match is None:
            return None
        return match.group(1).strip()

    @classmethod
    def _dates(cls, text: str) -> list[str]:
        found: list[str] = []
        for match in _ANY_DATE_RE.finditer(text):
            if match.group(1):
                found.append(match.group(1))
                continue
            day, month, year = match.group(2), match.group(3), match.group(4)
            found.append(f"{int(day):02d}.{_MONTHS[month.lower()]}.{year}")
        return cls._unique(found)

    @staticmethod
    def _court_name(text: str) -> Optional[str]:
        for line in text.splitlines():
            cleaned = " ".join(line.split())
            if "АРБИТРАЖНЫЙ СУД" in cleaned.upper() and 10 < len(cleaned) < 180:
                return cleaned
            if re.search(r"районный суд|городской суд|областной суд|верховный суд", cleaned, re.IGNORECASE):
                return cleaned
        return None

    @staticmethod
    def _document_type(text: str) -> Optional[DocumentType]:
        head = text[:2500].lower()
        found: list[tuple[int, DocumentType]] = []
        for needle, label in (
            ("постановление", "Постановление"),
            ("требование кредитора", "Требование кредитора"),
            ("выписка", "Выписка"),
            ("определение", "Определение"),
            ("решение", "Решение"),
        ):
            index = head.find(needle)
            if index >= 0:
                found.append((index, label))
        if not found:
            return None
        return min(found)[1]

    @classmethod
    def _parties(cls, text: str) -> list[PartyInfo]:
        parties: list[PartyInfo] = []
        seen: set[str] = set()
        for match in _PARTY_RE.finditer(text):
            name = _tidy(match.group(2)).strip(" ,.;")
            key = name.lower().replace("ё", "е")
            if len(name) < 3 or key in seen:
                continue
            seen.add(key)
            window = text[match.end(): match.end() + 120]
            inn_match = _INN_RE.search(window)
            ogrn_match = _OGRN_RE.search(window)
            parties.append(
                PartyInfo(
                    role=cls._role(match.group(1)),
                    name=name,
                    inn=inn_match.group(1) if inn_match else None,
                    ogrn=ogrn_match.group(1) if ogrn_match else None,
                )
            )
        return parties

    @staticmethod
    def _role(token: str) -> PartyRole:
        lowered = token.lower()
        if lowered.startswith("ист"):
            return "Истец"
        if lowered.startswith("ответ"):
            return "Ответчик"
        if lowered.startswith("долж"):
            return "Должник"
        if lowered.startswith("кред"):
            return "Кредитор"
        return "Третье лицо"

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            if value not in result:
                result.append(value)
        return result
