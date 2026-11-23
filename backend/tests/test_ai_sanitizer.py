"""Проверка мягкой очистки JSON, который YandexGPT возвращает с null внутри списков."""

from app.models.schemas import (
    DocumentAnalysisResult,
    DocumentUploadItem,
    PageText,
    PdfParseResult,
    QuoteReference,
    RegexMetadata,
)
from app.services.ai_extractor import _keep_model, _norm, _party_seen, _sanitize_llm_payload, _to_iso_date


def _fallback() -> DocumentAnalysisResult:
    return DocumentAnalysisResult(
        case_number="А40-1/2026",
        court_name="Арбитражный суд города Москвы",
        judge_name="Иванова М.С.",
        document_type="Определение",
        stage_code="general_arbitration_dispute",
        case_stage="Общий арбитражный исковой спор",
        verdict_summary="Суть из регулярного разбора",
    )


def _parsed() -> PdfParseResult:
    return PdfParseResult(pages=[PageText(page=1, text="Заявитель Иванов")], metadata=RegexMetadata())


def test_dirty_llm_payload_becomes_valid_card() -> None:
    dirty = {
        "parties": [None, {"role": "Заявитель", "name": "Иванов"}, {"role": "СРО", "name": ""}, "не объект"],
        "deadlines": None,
        "key_points": ["текст цитаты", {"page_number": None, "exact_quote": ""}, None],
        "court_orders": [
            {
                "actor": "СРО",
                "action": "представить кандидатуру",
                "days_limit": "14 дней",
                "days_type": "рабочих",
                "anchor_event": "с даты получения",
                "source_quote": {"page_number": "0", "exact_quote": None},
            },
            None,
        ],
        "financial_claims": [None, "100 руб.", ""],
        "legal_basis": None,
        "next_hearing": {
            "date": None,
            "time": None,
            "location": None,
            "purpose": None,
            "source_quote": {"page_number": 1, "exact_quote": ""},
        },
        "stage_code": None,
        "document_type": "Что-то",
        "case_number": None,
        "court_name": "",
        "verdict_summary": None,
        "case_stage": "",
    }

    cleaned = _sanitize_llm_payload(dirty, _parsed(), _fallback())
    warnings = cleaned.pop("_sanitize_warnings")
    analysis = DocumentAnalysisResult.model_validate(cleaned)

    assert analysis.parties[0].role == "Должник"
    assert analysis.parties[0].name == "Иванов"
    assert len(analysis.parties) == 1
    assert analysis.deadlines == []
    assert analysis.key_points[0].exact_quote == "текст цитаты"
    assert analysis.key_points[0].page_number == 1
    assert len(analysis.key_points) == 1
    order = analysis.court_orders[0]
    assert order.days_limit == 14
    assert order.days_type == "business"
    assert order.anchor_event == "receipt_date"
    assert order.source_quote.page_number == 1
    assert order.source_quote.exact_quote == "представить кандидатуру"
    assert analysis.next_hearing is None
    assert analysis.case_number == "А40-1/2026"
    assert analysis.court_name == "Арбитражный суд города Москвы"
    assert analysis.document_type == "Определение"
    assert analysis.stage_code == "general_arbitration_dispute"
    assert analysis.verdict_summary == "Суть из регулярного разбора"
    assert analysis.financial_claims == ["100 руб."]
    assert analysis.legal_basis == []
    assert warnings


def test_one_broken_deadline_does_not_drop_the_rest() -> None:
    dirty = {
        "deadlines": [
            {
                "description": "Подача апелляционной жалобы",
                "days_limit": "14 дней",
                "days_type": "календарных",
                "urgency": "неизвестно",
                "source_quote": {"page_number": "1", "exact_quote": "в течение 14 дней"},
            },
            "битая запись",
            {"description": "", "days_limit": None, "source_quote": None},
        ],
        "parties": [
            {"role": "Гражданин", "name": "Петров"},
            {"role": "АУ", "name": "Сидоров"},
            {"role": "Саморегулируемая организация", "name": "Союз АУ"},
        ],
    }

    cleaned = _sanitize_llm_payload(dirty, _parsed(), _fallback())
    warnings = cleaned.pop("_sanitize_warnings")
    analysis = DocumentAnalysisResult.model_validate(cleaned)

    assert len(analysis.deadlines) == 1
    deadline = analysis.deadlines[0]
    assert deadline.days_limit == 14
    assert deadline.days_type == "calendar"
    assert deadline.urgency == "normal"
    assert [party.role for party in analysis.parties] == [
        "Должник",
        "Финансовый управляющий",
        "СРО АУ",
    ]
    assert any("срок" in item.lower() or "битая" in item.lower() or "объект" in item.lower() for item in warnings)


def test_party_inflection_and_russian_dates() -> None:
    assert _party_seen("Иванова Анна Петровна", _norm("заявление Ивановой Анны Петровны"))
    assert _party_seen("Гапанович", _norm("привлечь Гапановича"))
    assert _to_iso_date("12 октября 2026 года") == "2026-10-12"
    assert _to_iso_date("11 ноября 2026") == "2026-11-11"
    assert _to_iso_date("не дата") is None

    cleaned = _sanitize_llm_payload(
        {
            "act_date": "12 октября 2026 года",
            "next_hearing": {
                "date": "11 ноября 2026",
                "time": "09:38",
                "location": "зал № 1",
                "purpose": "рассмотрение",
            },
            "deadlines": [
                {
                    "description": "Апелляция",
                    "days_limit": 10,
                    "due_date": "22 октября 2026 года",
                    "anchor_event": "act_date",
                    "source_quote": {"page_number": 1, "exact_quote": "в течение 10 дней"},
                }
            ],
            "court_orders": [
                {
                    "actor": "СРО",
                    "action": "направить ответ",
                    "days_limit": 9,
                    "anchor_event": "receipt_date",
                    "due_date": "не дата",
                    "source_quote": {"page_number": 1, "exact_quote": "в течение девяти дней"},
                }
            ],
        },
        _parsed(),
        _fallback(),
    )
    cleaned.pop("_sanitize_warnings")
    analysis = DocumentAnalysisResult.model_validate(cleaned)
    assert analysis.act_date == "2026-10-12"
    assert analysis.next_hearing is not None and analysis.next_hearing.date == "2026-11-11"
    assert analysis.deadlines[0].due_date == "2026-10-22"
    assert analysis.court_orders[0].due_date is None


def test_reupload_updates_same_filename_and_newest_document_is_first() -> None:
    import tempfile
    from pathlib import Path

    from app.services.storage import CaseStore

    with tempfile.TemporaryDirectory() as folder:
        _assert_reupload(CaseStore(Path(folder) / "lawtech.db"))


def _assert_reupload(store: object) -> None:
    first = _upload_item("act.pdf", "первая версия")
    second = _upload_item("later.pdf", "более поздний акт")
    again = _upload_item("act.pdf", "повторная загрузка")
    case_id = store.save_document(first)
    store.save_document(second)
    store.save_document(again)
    detail = store.get_case(case_id)
    assert detail is not None
    assert [item.filename for item in detail.documents] == ["later.pdf", "act.pdf"]
    assert detail.documents[1].analysis.verdict_summary == "повторная загрузка"


def _upload_item(filename: str, summary: str) -> DocumentUploadItem:
    analysis = _fallback()
    analysis.verdict_summary = summary
    return DocumentUploadItem(
        filename=filename,
        page_count=1,
        pages=[PageText(page=1, text="текст акта")],
        regex_metadata=RegexMetadata(),
        analysis=analysis,
        extraction_mode="fallback",
    )


def test_ocr_latin_and_inflected_names_match() -> None:
    from app.services.ai_extractor import _same_party, _verify_quotes

    analysis = _fallback()
    analysis.key_points = [QuoteReference(page_number=1, exact_quote="Суд Всоставе рассмотрел заявление")]
    warnings = _verify_quotes([PageText(page=1, text="Суд Bсоставе рассмотрел заявление")], analysis)
    assert warnings == []
    assert analysis.key_points[0].verified is True
    assert _same_party("Гапанович Ольга Афанасьевна", "Гапанович Ольгу Афанасьевну")
    assert _same_party("Собитнюк Андрей Алексеевич", "Собитнюка Андрея Алексеевича")


def test_invalid_quote_is_dropped_without_raising() -> None:
    sink: list[dict[str, object]] = []
    warnings: list[str] = []
    _keep_model({"page_number": 0, "exact_quote": "цитата"}, QuoteReference, sink, warnings, "key_point")
    assert sink == []
    assert warnings and "key_point" in warnings[0]
