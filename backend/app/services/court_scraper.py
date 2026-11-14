"""Провайдеры карточки дела: mock, kad.arbitr.ru и sudrf.ru."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any, AsyncIterator, Optional

from dotenv import load_dotenv

from app.models.schemas import (
    CaseCard,
    CourtAct,
    CourtSource,
    CourtSystem,
    DocumentAnalysisResult,
    HearingEvent,
    PartyInfo,
)

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_ARB_RE = re.compile(r"^[АAаa](?:\d{2}-\d+/\d{2,4}|-\d{2}-\d+/\d{4})$", re.IGNORECASE)
_UID_RE = re.compile(r"\d{2}[A-Za-zА-Яа-я]{2}\d{0,6}-\d{2}-\d{4}-\d{3,8}-\d{2}")
_SOJ_RE = re.compile(r"^\d{1,3}-\d+/\d{2,4}(?:~.+)?" )
_COURTS: dict[str, str] = {
    "А40": "Арбитражный суд города Москвы",
    "А41": "Арбитражный суд Московской области",
    "А56": "Арбитражный суд города Санкт-Петербурга и Ленинградской области",
    "А32": "Арбитражный суд Краснодарского края",
    "А45": "Арбитражный суд Новосибирской области",
    "А65": "Арбитражный суд Республики Татарстан",
    "А07": "Арбитражный суд Республики Башкортостан",
    "А60": "Арбитражный суд Свердловской области",
    "А53": "Арбитражный суд Ростовской области",
    "А27": "Арбитражный суд Кемеровской области",
}
_COMPANIES = (
    "ООО «Северная логистика»",
    "ООО «ПромСнаб»",
    "АО «Волга-Инвест»",
    "ООО «СтройАльянс»",
    "ПАО «НефтехимСервис»",
    "ООО «Городские сети»",
    "АО «Балтийский терминал»",
    "ООО «РегионПродукт»",
)
_PERSONS = (
    "Иванов Сергей Петрович",
    "Соколова Анна Викторовна",
    "Кузнецов Дмитрий Олегович",
    "Морозова Елена Игоревна",
    "Павлов Артём Николаевич",
    "Орлова Мария Андреевна",
)
_JUDGES = (
    "Иванова М. С.",
    "Петров А. В.",
    "Смирнова Е. Н.",
    "Кузнецов Д. А.",
    "Соколова О. И.",
    "Морозов П. С.",
)
_SOJ_COURTS = (
    "Пресненский районный суд города Москвы",
    "Тверской районный суд города Москвы",
    "Санкт-Петербургский городской суд",
    "Центральный районный суд города Новосибирска",
)
_HEARING_RESULTS = (
    ("Исковое заявление (заявление) принято к производству", "Возбуждено производство по делу"),
    ("Предварительное судебное заседание", "Рассмотрены ходатайства, назначено заседание"),
    ("Судебное заседание", "Заседание отложено"),
    ("Судебное заседание", "Рассмотрение по существу"),
)


class CourtScraperError(Exception):
    """Сайт суда не ответил или карточка не распознана."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def detect_court_system(case_number: str) -> CourtSystem:
    """А40-… — арбитраж (КАД), УИД или «~» — суд общей юрисдикции."""

    raw = case_number.strip()
    if "~" in raw or _UID_RE.search(raw):
        return "general_jurisdiction"
    if _ARB_RE.match(raw):
        return "arbitration"
    if _SOJ_RE.match(raw):
        return "general_jurisdiction"
    return "arbitration"


def _source_for(system: CourtSystem) -> CourtSource:
    if system == "arbitration":
        return "kad.arbitr.ru"
    return "sudrf.ru"


def _recognized(case_number: str) -> bool:
    raw = case_number.strip()
    return bool(_ARB_RE.match(raw) or "~" in raw or _UID_RE.search(raw) or _SOJ_RE.match(raw))


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class BaseCourtProvider(ABC):
    """Общий контракт получения карточки по номеру дела."""

    name: str

    @abstractmethod
    async def fetch_case(self, case_number: str) -> CaseCard:
        """Вернуть карточку дела."""


class MockCourtProvider(BaseCourtProvider):
    """Стабильная демо-карточка для любого номера, без запроса к сайту суда."""

    name = "mock"

    async def fetch_case(self, case_number: str) -> CaseCard:
        return self.build(case_number, detect_court_system(case_number))

    def build(self, case_number: str, system: CourtSystem) -> CaseCard:
        seed = _seed(case_number)
        year = _case_year(case_number)
        dates = _timeline(year, seed)
        judge = _JUDGES[seed % len(_JUDGES)]
        bankruptcy = system == "arbitration" and seed % 3 == 0
        parties = _mock_parties(system, bankruptcy, seed)
        status = "Рассмотрено" if dates[-1] < date.today() and seed % 2 == 0 else "Рассматривается"
        hearings = [
            HearingEvent(
                date=current.isoformat(),
                time=("10:00", "11:30", "14:00", "15:40")[index % 4],
                instance=_instance_name(case_number, system, seed),
                courtroom=f"зал № {(seed % 18) + 1}",
                result=result if not (index == 3 and status == "Рассмотрено") else "Решение (определение) оглашено",
                description=description,
            )
            for index, (current, (description, result)) in enumerate(zip(dates, _HEARING_RESULTS))
        ]
        acts = _mock_acts(dates, judge, system, bankruptcy, status, _instance_name(case_number, system, seed))
        note = "Демонстрационная карточка: USE_MOCK_SCRAPER=true, сайт суда не открывался."
        if not _recognized(case_number):
            note += " Формат номера не распознан, показан демо-шаблон арбитражного дела."
        uid = case_number.strip() if _UID_RE.search(case_number) or "~" in case_number else None
        return CaseCard(
            case_number=case_number.strip(),
            court_system=system,
            source=_source_for(system),
            mode="mock",
            court_name=_court_name(case_number, system, seed),
            instance=_instance_name(case_number, system, seed),
            judge_name=judge,
            case_status=status,
            category=_category(system, bankruptcy),
            registered_at=dates[0].isoformat(),
            parties=parties,
            hearings=hearings,
            acts=acts,
            uid=uid,
            note=note,
        )


class DocumentMetadataProvider(BaseCourtProvider):
    """Карточка из уже разобранного PDF, без хождения на сайт суда."""

    name = "document"

    async def fetch_case(self, case_number: str) -> CaseCard:
        raise CourtScraperError(
            "DocumentMetadataProvider строит карточку из загруженного акта. Используйте to_card()."
        )

    def to_card(self, analysis: DocumentAnalysisResult) -> CaseCard:
        system = detect_court_system(analysis.case_number)
        act_date = analysis.document_date or date.today().isoformat()
        return CaseCard(
            case_number=analysis.case_number,
            court_system=system,
            source=_source_for(system),
            mode="document",
            court_name=analysis.court_name,
            instance="По тексту судебного акта",
            judge_name=analysis.judge_name,
            case_status="Сведения взяты из загруженного PDF",
            category=analysis.document_type,
            registered_at=act_date,
            parties=analysis.parties,
            hearings=[],
            acts=[
                CourtAct(
                    date=act_date,
                    document_type=analysis.document_type,
                    instance=analysis.court_name,
                    judge_name=analysis.judge_name,
                    title=analysis.verdict_summary[:300],
                )
            ],
            note="Карточка собрана из загруженного судебного акта, сайт суда не открывался.",
        )


class PlaywrightKadProvider(BaseCourtProvider):
    """Базовый разбор публичной карточки kad.arbitr.ru."""

    name = "kad.arbitr.ru"
    base_url = "https://kad.arbitr.ru/"

    async def fetch_case(self, case_number: str) -> CaseCard:
        async with _chromium_page() as page:
            try:
                await page.goto(self.base_url, wait_until="domcontentloaded", timeout=45000)
                await _dismiss_overlays(page)
                await _fill_first(page, _KAD_INPUTS, case_number)
                await _click_first(page, _KAD_SUBMIT)
                await page.wait_for_timeout(1500)
                link = page.locator("a.num_case")
                if await link.count():
                    await link.first.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=20000)
                text = await page.inner_text("body")
            except CourtScraperError:
                raise
            except Exception as exc:
                logger.exception("kad.arbitr.ru fetch failed")
                raise CourtScraperError(
                    "Не удалось открыть карточку на kad.arbitr.ru. "
                    "Сайт мог показать проверку или изменить вёрстку. "
                    f"Для локальной отладки включите USE_MOCK_SCRAPER=true. ({exc.__class__.__name__})"
                ) from exc
        _raise_if_blocked(text, "kad.arbitr.ru")
        if _compact(case_number) not in _compact(text):
            raise CourtScraperError(
                f"Дело {case_number} не найдено на kad.arbitr.ru или страница поиска не распознана."
            )
        return _card_from_visible_text(case_number, text, source="kad.arbitr.ru", system="arbitration")


class PlaywrightSudrfProvider(BaseCourtProvider):
    """Базовый разбор поиска по делам судов общей юрисдикции."""

    name = "sudrf.ru"
    base_url = "https://sudrf.ru/"

    async def fetch_case(self, case_number: str) -> CaseCard:
        async with _chromium_page() as page:
            try:
                await page.goto(self.base_url, wait_until="domcontentloaded", timeout=45000)
                await _dismiss_overlays(page)
                filled = await _try_fill(page, _SUDRF_INPUTS, case_number)
                if filled:
                    await _click_first(page, _SUDRF_SUBMIT)
                    await page.wait_for_timeout(1500)
                text = await page.inner_text("body")
            except CourtScraperError:
                raise
            except Exception as exc:
                logger.exception("sudrf.ru fetch failed")
                raise CourtScraperError(
                    "Не удалось открыть sudrf.ru. Порталы судов общей юрисдикции "
                    "различаются по регионам. Для отладки включите USE_MOCK_SCRAPER=true. "
                    f"({exc.__class__.__name__})"
                ) from exc
        _raise_if_blocked(text, "sudrf.ru")
        if _compact(case_number) not in _compact(text):
            raise CourtScraperError(
                f"Дело {case_number} не найдено в открытой форме sudrf.ru. "
                "Для карточки СОЮ обычно нужен сайт конкретного суда."
            )
        return _card_from_visible_text(
            case_number,
            text,
            source="sudrf.ru",
            system="general_jurisdiction",
        )


class CourtScraperService:
    """Выбирает провайдер по формату номера и флагу USE_MOCK_SCRAPER."""

    def __init__(self) -> None:
        load_dotenv()
        self.use_mock = _env_flag("USE_MOCK_SCRAPER", True)
        self.providers: dict[str, BaseCourtProvider] = {
            "mock": MockCourtProvider(),
            "kad": PlaywrightKadProvider(),
            "sudrf": PlaywrightSudrfProvider(),
            "document": DocumentMetadataProvider(),
        }

    async def fetch_case_by_number(self, case_number: str) -> CaseCard:
        normalized = " ".join(case_number.split())
        if len(normalized) < 3:
            raise CourtScraperError("Укажите номер дела")
        system = detect_court_system(normalized)
        if self.use_mock:
            provider = self.providers["mock"]
            assert isinstance(provider, MockCourtProvider)
            return provider.build(normalized, system)
        provider = self.providers["kad" if system == "arbitration" else "sudrf"]
        return await provider.fetch_case(normalized)

    def card_from_document(self, analysis: DocumentAnalysisResult) -> CaseCard:
        provider = self.providers["document"]
        assert isinstance(provider, DocumentMetadataProvider)
        return provider.to_card(analysis)


_KAD_INPUTS = (
    "textarea.g-ph",
    "input.g-ph",
    "#sug-cases input",
    "input[placeholder*='А50']",
    "input[placeholder*='номер']",
)
_KAD_SUBMIT = ("#b-form-submit", "button:has-text('Найти')", "input[type='submit']")
_SUDRF_INPUTS = (
    "input[name*='case']",
    "input[placeholder*='номер']",
    "input[type='search']",
)
_SUDRF_SUBMIT = ("button:has-text('Найти')", "input[type='submit']", "button[type='submit']")


@asynccontextmanager
async def _chromium_page() -> AsyncIterator[object]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise CourtScraperError(
            "Пакет playwright не установлен. Выполните: pip install playwright && playwright install chromium"
        ) from exc

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            raise CourtScraperError(
                "Не удалось запустить Chromium. Выполните: playwright install chromium"
            ) from exc
        try:
            page = await browser.new_page(locale="ru-RU", user_agent=_USER_AGENT)
            yield page
        finally:
            await browser.close()


async def _dismiss_overlays(page: Any) -> None:
    for label in ("Закрыть", "Понятно", "Нет, спасибо"):
        locator = page.get_by_text(label, exact=False)
        try:
            if await locator.count():
                await locator.first.click(timeout=1200)
        except Exception:
            continue


async def _fill_first(page: Any, selectors: tuple[str, ...], value: str) -> None:
    if not await _try_fill(page, selectors, value):
        raise CourtScraperError("На странице суда не найдено поле номера дела.")


async def _try_fill(page: Any, selectors: tuple[str, ...], value: str) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if await locator.count():
                await locator.first.fill(value, timeout=4000)
                return True
        except Exception:
            continue
    return False


async def _click_first(page: Any, selectors: tuple[str, ...]) -> None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if await locator.count():
                await locator.first.click(timeout=4000)
                return
        except Exception:
            continue
    await page.keyboard.press("Enter")


def _raise_if_blocked(text: str, site: str) -> None:
    lowered = text.lower()
    markers = ("капча", "captcha", "не робот", "доступ запрещ", "access denied")
    if any(marker in lowered for marker in markers):
        raise CourtScraperError(
            f"{site} запросил проверку или ограничил доступ. Включите USE_MOCK_SCRAPER=true для локальной работы."
        )


def _card_from_visible_text(
    case_number: str,
    text: str,
    *,
    source: CourtSource,
    system: CourtSystem,
) -> CaseCard:
    judge_match = re.search(
        r"(?:судья|судьи)\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.|(?:\s+[А-ЯЁ][а-яё]+){1,2}))",
        text,
        re.IGNORECASE,
    )
    court_name = _visible_court_name(text) or (
        "Арбитражный суд" if system == "arbitration" else "Суд общей юрисдикции"
    )
    hearings: list[HearingEvent] = []
    acts: list[CourtAct] = []
    for line in text.splitlines():
        cleaned = " ".join(line.split())
        date_match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", cleaned)
        if date_match is None or len(cleaned) < 12:
            continue
        lowered = cleaned.lower()
        if "заседан" in lowered:
            hearings.append(
                HearingEvent(
                    date=_iso_date(date_match.group(1)),
                    instance=court_name,
                    result=cleaned[:240],
                    description=cleaned[:240],
                )
            )
        if any(word in lowered for word in ("определен", "решен", "постановлен")):
            acts.append(
                CourtAct(
                    date=_iso_date(date_match.group(1)),
                    document_type=_act_type(lowered),
                    instance=court_name,
                    title=cleaned[:300],
                )
            )
    return CaseCard(
        case_number=case_number.strip(),
        court_system=system,
        source=source,
        mode="live",
        court_name=court_name,
        instance=court_name,
        judge_name=judge_match.group(1).strip() if judge_match else None,
        case_status="Сведения со страницы суда",
        registered_at=hearings[0].date if hearings else None,
        hearings=hearings[:20],
        acts=acts[:20],
        note="Карточка собрана из видимого текста страницы. Вёрстка суда могла измениться, часть полей может быть пустой.",
    )


def _visible_court_name(text: str) -> Optional[str]:
    for line in text.splitlines():
        cleaned = " ".join(line.split())
        if "арбитражный суд" in cleaned.lower() and len(cleaned) < 180:
            return cleaned
        if re.search(r"районный суд|городской суд|областной суд", cleaned, re.IGNORECASE):
            return cleaned
    return None


def _act_type(lowered: str) -> str:
    if "постановлен" in lowered:
        return "Постановление"
    if "решен" in lowered:
        return "Решение"
    return "Определение"


def _iso_date(value: str) -> str:
    day, month, year = value.split(".")
    return f"{year}-{month}-{day}"


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).upper().replace("A", "А")


def _seed(case_number: str) -> int:
    digest = hashlib.sha256(case_number.strip().upper().encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _case_year(case_number: str) -> int:
    match = re.search(r"/(20\d{2})\b", case_number)
    if match:
        return int(match.group(1))
    match = re.search(r"(20\d{2})", case_number)
    if match:
        return int(match.group(1))
    return date.today().year


def _timeline(year: int, seed: int) -> list[date]:
    start = date(year, 2, 10) + timedelta(days=seed % 80)
    points = [start + timedelta(days=step) for step in (0, 21, 45, 78)]
    today = date.today()
    adjusted: list[date] = []
    for index, current in enumerate(points):
        if current > today:
            current = today - timedelta(days=(len(points) - index) * 14)
        adjusted.append(current)
    return adjusted


def _court_code(case_number: str) -> Optional[str]:
    match = re.search(r"[АAаa](\d{2})", case_number)
    if match is None:
        return None
    return f"А{match.group(1)}"


def _court_name(case_number: str, system: CourtSystem, seed: int) -> str:
    if system == "general_jurisdiction":
        return _SOJ_COURTS[seed % len(_SOJ_COURTS)]
    code = _court_code(case_number)
    if code and code in _COURTS:
        return _COURTS[code]
    return "Арбитражный суд"


def _instance_name(case_number: str, system: CourtSystem, seed: int) -> str:
    court = _court_name(case_number, system, seed)
    return f"Первая инстанция, {court}"


def _category(system: CourtSystem, bankruptcy: bool) -> str:
    if system == "general_jurisdiction":
        return "Гражданское дело"
    if bankruptcy:
        return "О несостоятельности (банкротстве)"
    return "Экономические споры по гражданским правоотношениям"


def _mock_parties(system: CourtSystem, bankruptcy: bool, seed: int) -> list[PartyInfo]:
    if system == "general_jurisdiction":
        return [
            PartyInfo(role="Истец", name=_PERSONS[seed % len(_PERSONS)]),
            PartyInfo(role="Ответчик", name=_PERSONS[(seed + 2) % len(_PERSONS)]),
            PartyInfo(role="Третье лицо", name=_PERSONS[(seed + 4) % len(_PERSONS)]),
        ]
    left = _COMPANIES[seed % len(_COMPANIES)]
    right = _COMPANIES[(seed + 3) % len(_COMPANIES)]
    if left == right:
        right = _COMPANIES[(seed + 4) % len(_COMPANIES)]
    if bankruptcy:
        parties = [
            PartyInfo(role="Должник", name=right, inn=_inn10(seed + 11), ogrn=_ogrn13(seed + 21)),
            PartyInfo(role="Кредитор", name=left, inn=_inn10(seed + 17), ogrn=_ogrn13(seed + 29)),
        ]
        if seed % 5 == 0:
            parties.append(PartyInfo(role="Третье лицо", name="ФНС России"))
        return parties
    return [
        PartyInfo(role="Истец", name=left, inn=_inn10(seed + 3), ogrn=_ogrn13(seed + 5)),
        PartyInfo(role="Ответчик", name=right, inn=_inn10(seed + 9), ogrn=_ogrn13(seed + 13)),
    ]


def _mock_acts(
    dates: list[date],
    judge: str,
    system: CourtSystem,
    bankruptcy: bool,
    status: str,
    instance: str,
) -> list[CourtAct]:
    first = "Определение о принятии заявления" if bankruptcy or system == "arbitration" else "Определение о принятии иска"
    acts = [
        CourtAct(
            date=dates[0].isoformat(),
            document_type="Определение",
            instance=instance,
            judge_name=judge,
            title=first,
        ),
        CourtAct(
            date=dates[2].isoformat(),
            document_type="Определение",
            instance=instance,
            judge_name=judge,
            title="Определение об отложении судебного заседания",
        ),
    ]
    if status == "Рассмотрено":
        acts.append(
            CourtAct(
                date=dates[-1].isoformat(),
                document_type="Решение" if not bankruptcy else "Определение",
                instance=instance,
                judge_name=judge,
                title="Решение по существу" if not bankruptcy else "Определение по обособленному спору",
            )
        )
    return acts


def _inn10(seed: int) -> str:
    body = f"{seed % 1_000_000_000:09d}"
    digits = [int(char) for char in body]
    checksum = sum(digit * coeff for digit, coeff in zip(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
    return body + str(checksum)


def _ogrn13(seed: int) -> str:
    body = f"{seed % 10**12:012d}"
    checksum = int(body) % 11 % 10
    return body + str(checksum)
