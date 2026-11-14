"""Строгие модели карточки дела, цитат и процессуальных сроков."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

PartyRole = Literal["Истец", "Ответчик", "Должник", "Кредитор", "Третье лицо"]
DocumentType = Literal[
    "Определение",
    "Решение",
    "Постановление",
    "Требование кредитора",
    "Выписка",
]
Urgency = Literal["critical", "normal"]
CourtSystem = Literal["arbitration", "general_jurisdiction"]
CourtSource = Literal["kad.arbitr.ru", "sudrf.ru"]
ScraperMode = Literal["mock", "live", "document"]
ExtractionMode = Literal["llm", "fallback"]


class PartyInfo(BaseModel):
    """Участник спора."""

    model_config = ConfigDict(extra="ignore")

    role: PartyRole = Field(description="Процессуальная роль участника")
    name: str = Field(description="Наименование или ФИО")
    inn: Optional[str] = Field(default=None, description="ИНН, если указан в источнике")
    ogrn: Optional[str] = Field(default=None, description="ОГРН или ОГРНИП, если указан в источнике")


class QuoteReference(BaseModel):
    """Дословная цитата для подсветки фрагмента в PDF."""

    model_config = ConfigDict(extra="ignore")

    page_number: int = Field(ge=1, description="Номер страницы PDF, начиная с 1")
    exact_quote: str = Field(min_length=1, description="Дословный фрагмент текста страницы")


class DeadlineItem(BaseModel):
    """Процессуальный срок для радара дедлайнов."""

    model_config = ConfigDict(extra="ignore")

    description: str = Field(
        description='Задача, например «Публикация на ЕФРСБ» или «Подача апелляционной жалобы»',
    )
    due_date: Optional[str] = Field(
        default=None,
        description="Расчётная дата в формате YYYY-MM-DD, если известна дата отсчёта",
    )
    days_limit: int = Field(ge=0, description="Длительность срока в днях")
    urgency: Urgency = Field(description="critical — до 10 дней, normal — более длинный срок")
    source_quote: QuoteReference


class DocumentAnalysisResult(BaseModel):
    """Карточка судебного акта: реквизиты, суть, суммы, сроки и цитаты."""

    model_config = ConfigDict(extra="ignore")

    case_number: str = Field(description='Номер дела, например «А40-12345/2026»')
    court_name: str
    judge_name: Optional[str] = None
    document_type: DocumentType
    document_date: Optional[str] = Field(default=None, description="Дата акта как в документе")
    parties: list[PartyInfo] = Field(default_factory=list)
    verdict_summary: str = Field(description="Краткая суть резолютивной части, 1–2 предложения")
    financial_claims: list[str] = Field(
        default_factory=list,
        description="Кто, кому и сколько должен или требует",
    )
    deadlines: list[DeadlineItem] = Field(default_factory=list)
    legal_basis: list[str] = Field(
        default_factory=list,
        description="Статьи ГК РФ, АПК РФ, ФЗ-127 «О банкротстве»",
    )
    key_points: list[QuoteReference] = Field(
        default_factory=list,
        description="Ключевые тезисы мотивировки с привязкой к странице",
    )


class PageText(BaseModel):
    """Текст одной страницы для Split-Screen просмотра."""

    page: int = Field(ge=1)
    text: str


class RegexMetadata(BaseModel):
    """Быстрые реквизиты, найденные регулярными выражениями до вызова LLM."""

    case_number: Optional[str] = None
    dates: list[str] = Field(default_factory=list)
    inns: list[str] = Field(default_factory=list)
    ogrns: list[str] = Field(default_factory=list)
    court_name: Optional[str] = None
    judge_name: Optional[str] = None
    document_type: Optional[DocumentType] = None
    parties: list[PartyInfo] = Field(default_factory=list)


class PdfParseResult(BaseModel):
    """Результат локального разбора PDF."""

    pages: list[PageText]
    metadata: RegexMetadata


class DocumentUploadItem(BaseModel):
    """Разобранный файл: карточка, цитаты и постраничный текст."""

    filename: str
    page_count: int
    pages: list[PageText]
    regex_metadata: RegexMetadata
    analysis: DocumentAnalysisResult
    extraction_mode: ExtractionMode
    warnings: list[str] = Field(default_factory=list)


class DocumentUploadResponse(BaseModel):
    documents: list[DocumentUploadItem]


class FetchCaseRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"case_number": "А40-154321/2026"}]},
    )

    case_number: str = Field(min_length=3, description="Номер дела, например А40-154321/2026")


class HearingEvent(BaseModel):
    """Заседание в хронологии дела."""

    date: str
    time: Optional[str] = None
    instance: str
    courtroom: Optional[str] = None
    result: str
    description: str


class CourtAct(BaseModel):
    """Судебный акт в карточке дела."""

    date: str
    document_type: str
    instance: str
    judge_name: Optional[str] = None
    title: str


class CaseCard(BaseModel):
    """Карточка дела с сайта суда или из демонстрационного провайдера."""

    case_number: str
    court_system: CourtSystem
    source: CourtSource
    mode: ScraperMode
    court_name: str
    instance: str
    judge_name: Optional[str] = None
    case_status: str
    category: Optional[str] = None
    registered_at: Optional[str] = None
    parties: list[PartyInfo] = Field(default_factory=list)
    hearings: list[HearingEvent] = Field(default_factory=list)
    acts: list[CourtAct] = Field(default_factory=list)
    uid: Optional[str] = None
    note: Optional[str] = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    mock_scraper: bool
    llm_configured: bool
    version: str


class ExtractionOutcome(BaseModel):
    """Внутренний результат AILegalExtractor."""

    analysis: DocumentAnalysisResult
    extraction_mode: ExtractionMode
    warnings: list[str] = Field(default_factory=list)
