"""Строгие модели карточки дела, цитат, сроков и правовой справки."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

PartyRole = Literal[
    "Истец",
    "Ответчик",
    "Должник",
    "Кредитор",
    "Третье лицо",
    "СРО АУ",
    "Финансовый управляющий",
]
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
DaysType = Literal["calendar", "business", "unknown"]
AnchorEvent = Literal["act_date", "receipt_date", "hearing_date", "fixed_date", "unknown"]
StageCode = Literal[
    "bankruptcy_individual_accept",
    "bankruptcy_individual_restructuring",
    "bankruptcy_individual_realization",
    "bankruptcy_individual_intro",
    "bankruptcy_legal_entity",
    "creditor_registry_claim",
    "transaction_challenge",
    "subsidiary_liability",
    "general_arbitration_dispute",
]


class PartyInfo(BaseModel):
    """Участник спора."""

    model_config = ConfigDict(extra="ignore")

    role: PartyRole = Field(description="Процессуальная роль участника")
    name: str = Field(description="Наименование или ФИО")
    inn: Optional[str] = Field(default=None, description="ИНН, если указан в источнике")
    ogrn: Optional[str] = Field(default=None, description="ОГРН или ОГРНИП, если указан в источнике")
    snils: Optional[str] = Field(default=None, description="СНИЛС, если указан в источнике")
    address: Optional[str] = Field(default=None, description="Адрес, если указан в источнике")


class QuoteReference(BaseModel):
    """Дословная цитата для подсветки фрагмента в PDF."""

    model_config = ConfigDict(extra="ignore")

    page_number: int = Field(ge=1, description="Номер страницы PDF, начиная с 1")
    exact_quote: str = Field(min_length=1, description="Дословный фрагмент текста страницы")
    verified: bool = Field(
        default=True,
        description="True — цитата дословно найдена в PDF на слое 3, False — не подтверждена",
    )


class DeadlineItem(BaseModel):
    """Процессуальный срок для радара дедлайнов."""

    model_config = ConfigDict(extra="ignore")

    description: str = Field(
        description='Задача, например «Публикация на ЕФРСБ» или «Подача апелляционной жалобы»',
    )
    due_date: Optional[str] = Field(
        default=None,
        description="Расчётная дата YYYY-MM-DD. Считает код слоя 3, не модель",
    )
    days_limit: int = Field(ge=0, description="Длительность срока в днях")
    months_limit: Optional[int] = Field(default=None, ge=0, description="Срок в календарных месяцах, например 1 по ст. 259 АПК РФ")
    days_type: DaysType = "calendar"
    anchor_event: AnchorEvent = "act_date"
    urgency: Urgency = Field(description="critical — до 10 дней, normal — более длинный срок")
    source_quote: QuoteReference


class NextHearingInfo(BaseModel):
    """Ближайшее судебное заседание, извлечённое из акта."""

    model_config = ConfigDict(extra="ignore")

    date: Optional[str] = Field(default=None, description="Дата заседания YYYY-MM-DD")
    time: Optional[str] = Field(default=None, description="Время, например 09:38")
    location: Optional[str] = Field(default=None, description="Адрес или зал")
    purpose: Optional[str] = None
    source_quote: Optional[QuoteReference] = None


class CourtOrderItem(BaseModel):
    """Действие, которое суд прямо требует в резолютивной части."""

    model_config = ConfigDict(extra="ignore")

    actor: str = Field(description="Кому адресовано: СРО, Должник, Кредитор, управляющий")
    action: str = Field(description="Что конкретно сделать")
    deadline_text: Optional[str] = Field(
        default=None,
        description="Срок как он написан в акте",
    )
    days_limit: Optional[int] = Field(default=None, ge=0)
    months_limit: Optional[int] = Field(default=None, ge=0)
    days_type: DaysType = "calendar"
    anchor_event: AnchorEvent = "act_date"
    due_date: Optional[str] = Field(default=None, description="YYYY-MM-DD, считает код слоя 3")
    source_quote: QuoteReference


class StatutoryReference(BaseModel):
    """Норма из проверенной базы, а не из свободного ответа модели."""

    model_config = ConfigDict(extra="ignore")

    article: str
    summary: str
    url: Optional[str] = None


class LegalKnowledgeBlock(BaseModel):
    """Справка по законодательству РФ. Заполняется только кодом."""

    model_config = ConfigDict(extra="ignore")

    stage_title: str
    source_disclaimer: str
    statutory_articles: list[StatutoryReference] = Field(default_factory=list)
    legal_consequences: list[str] = Field(default_factory=list)
    mandatory_obligations: list[str] = Field(default_factory=list)
    rights_and_restrictions: list[str] = Field(default_factory=list)


class DocumentAnalysisResult(BaseModel):
    """Карточка судебного акта: факты из PDF и отдельная правовая справка."""

    model_config = ConfigDict(extra="ignore")

    case_number: str = Field(description='Номер дела, например «А40-12345/2026»')
    court_name: str
    judge_name: Optional[str] = None
    document_type: DocumentType
    document_date: Optional[str] = Field(default=None, description="Дата акта как в документе")
    stage_code: StageCode
    case_stage: str
    act_date: Optional[str] = Field(default=None, description="Дата вынесения акта YYYY-MM-DD")
    parties: list[PartyInfo] = Field(default_factory=list)
    verdict_summary: str = Field(description="Краткая суть резолютивной части, 1–2 предложения")
    financial_claims: list[str] = Field(
        default_factory=list,
        description="Кто, кому и сколько должен или требует",
    )
    next_hearing: Optional[NextHearingInfo] = None
    court_orders: list[CourtOrderItem] = Field(default_factory=list)
    deadlines: list[DeadlineItem] = Field(default_factory=list)
    legal_basis: list[str] = Field(
        default_factory=list,
        description="Статьи, прямо названные в тексте акта",
    )
    key_points: list[QuoteReference] = Field(
        default_factory=list,
        description="Ключевые тезисы мотивировки с привязкой к странице",
    )
    legal_knowledge: Optional[LegalKnowledgeBlock] = None


class PageText(BaseModel):
    """Текст одной страницы для Split-Screen просмотра. Не нормализуется."""

    page: int = Field(ge=1)
    text: str


class RegexMetadata(BaseModel):
    """Быстрые реквизиты, найденные регулярными выражениями до вызова LLM."""

    case_number: Optional[str] = None
    dates: list[str] = Field(default_factory=list)
    act_date: Optional[str] = None
    inns: list[str] = Field(default_factory=list)
    ogrns: list[str] = Field(default_factory=list)
    snils_list: list[str] = Field(default_factory=list)
    sro_names: list[str] = Field(default_factory=list)
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
    case_id: Optional[int] = None
    doc_id: Optional[int] = None
    pdf_url: Optional[str] = None
    token_usage: Optional[dict[str, int]] = None


class DocumentUploadResponse(BaseModel):
    documents: list[DocumentUploadItem]
    case_id: Optional[int] = None


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
    case_id: Optional[int] = None


class TaskStateItem(BaseModel):
    """Отметка юриста по поручению суда или процессуальному сроку."""

    task_key: str
    is_done: bool = False
    receipt_date: Optional[str] = None
    calculated_due_date: Optional[str] = None
    updated_at: Optional[str] = None


class TaskStateUpdateRequest(BaseModel):
    """Частичное обновление задачи. Пустая дата получения очищает расчёт."""

    is_done: Optional[bool] = None
    receipt_date: Optional[str] = None


class TaskStateUpdateResponse(BaseModel):
    task: TaskStateItem
    case_urgency: Urgency
    case_nearest_deadline: Optional[str] = None


class CaseListItem(BaseModel):
    """Строка радара дел."""

    id: int
    case_number: str
    court_name: str
    judge_name: Optional[str] = None
    stage_code: Optional[str] = None
    case_stage: Optional[str] = None
    case_status: Optional[str] = None
    urgency: Urgency = "normal"
    next_hearing_date: Optional[str] = None
    nearest_deadline: Optional[str] = None
    verdict_summary: Optional[str] = None
    extraction_mode: Optional[str] = None
    updated_at: str
    document_count: int = 0
    open_tasks_count: int = 0
    done_tasks_count: int = 0


class CaseDetailResponse(BaseModel):
    """Дело и все сохранённые PDF с постраничным текстом."""

    id: int
    case_number: str
    court_name: str
    judge_name: Optional[str] = None
    stage_code: Optional[str] = None
    case_stage: Optional[str] = None
    case_status: Optional[str] = None
    urgency: Urgency = "normal"
    next_hearing_date: Optional[str] = None
    nearest_deadline: Optional[str] = None
    verdict_summary: Optional[str] = None
    extraction_mode: Optional[str] = None
    updated_at: str
    card: dict
    documents: list[DocumentUploadItem] = Field(default_factory=list)
    task_states: dict[str, TaskStateItem] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    mock_scraper: bool
    llm_configured: bool
    llm_model: Optional[str] = None
    llm_provider: str
    version: str


class ExtractionOutcome(BaseModel):
    """Внутренний результат AILegalExtractor."""

    analysis: DocumentAnalysisResult
    extraction_mode: ExtractionMode
    warnings: list[str] = Field(default_factory=list)
    token_usage: Optional[dict[str, int]] = None
