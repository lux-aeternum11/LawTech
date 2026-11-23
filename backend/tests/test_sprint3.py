"""Объединение дел, сроки от даты получения и ссылки на КонсультантПлюс."""

import tempfile
from pathlib import Path
from urllib.parse import unquote_plus

from app.models.schemas import (
    CourtOrderItem,
    DeadlineItem,
    DocumentUploadItem,
    PageText,
    PartyInfo,
    PdfParseResult,
    QuoteReference,
    RegexMetadata,
)
from app.services.ai_extractor import _apply_due_date, _dedupe_parties, _merge_regex_facts, calculate_due_date
from app.services.legal_knowledge import LegalKnowledgeService
from app.services.pdf_parser import SmartPDFParser, _INN_RE, canonical_case_number
from app.services.storage import CaseStore
from tests.test_ai_sanitizer import _fallback


def _quote(text: str) -> QuoteReference:
    return QuoteReference(page_number=1, exact_quote=text)


def _item(filename: str, case_number: str) -> DocumentUploadItem:
    analysis = _fallback()
    analysis.case_number = case_number
    analysis.act_date = "2026-11-01"
    analysis.next_hearing = None
    analysis.deadlines = [
        DeadlineItem(
            description="Публикация в ЕФРСБ",
            days_limit=3,
            days_type="business",
            anchor_event="receipt_date",
            urgency="critical",
            due_date=None,
            source_quote=_quote("в течение трёх рабочих дней"),
        )
    ]
    analysis.court_orders = [
        CourtOrderItem(
            actor="СРО",
            action="представить кандидатуру",
            days_limit=10,
            days_type="calendar",
            anchor_event="receipt_date",
            due_date=None,
            source_quote=_quote("в течение десяти дней"),
        )
    ]
    return DocumentUploadItem(
        filename=filename,
        page_count=1,
        pages=[PageText(page=1, text="текст акта")],
        regex_metadata=RegexMetadata(case_number=case_number),
        analysis=analysis,
        extraction_mode="fallback",
    )


def test_latin_and_cyrillic_case_numbers_share_one_case() -> None:
    assert canonical_case_number("a21–11942/2026") == "А21-11942/2026"
    assert canonical_case_number("А 21 - 11942 / 2026") == "А21-11942/2026"
    with tempfile.TemporaryDirectory() as folder:
        store = CaseStore(Path(folder) / "lawtech.db")
        first = _item("one.pdf", "A21-11942/2026")
        second = _item("two.pdf", "а21-11942/2026")
        case_id = store.save_document(first, b"%PDF-1.4 one")
        other_id = store.save_document(second, b"%PDF-1.4 two")
        assert case_id == other_id
        detail = store.get_case(case_id)
        assert detail is not None
        assert detail.case_number == "А21-11942/2026"
        assert [item.filename for item in detail.documents] == ["two.pdf", "one.pdf"]
        assert detail.documents[0].doc_id is not None
        assert detail.documents[0].pdf_url == f"/api/documents/{detail.documents[0].doc_id}/pdf"
        assert store.pdf_path(detail.documents[0].doc_id).is_file()
        listed = store.list_cases()
        assert len(listed) == 1
        assert listed[0].document_count == 2
        assert listed[0].urgency == "critical"
        assert listed[0].open_tasks_count == 4
        store.save_document(first, b"%PDF-1.4 replaced")
        again = store.get_case(case_id)
        assert again is not None
        assert len(again.documents) == 2


def test_receipt_date_calculates_due_and_done_clears_urgency() -> None:
    with tempfile.TemporaryDirectory() as folder:
        store = CaseStore(Path(folder) / "lawtech.db")
        item = _item("act.pdf", "А40-1/2026")
        case_id = store.save_document(item)
        assert item.doc_id is not None
        deadline_key = f"doc:{item.doc_id}:deadline:0"
        order_key = f"doc:{item.doc_id}:order:0"
        business = store.update_task_state(case_id, deadline_key, False, "2026-11-14")
        assert business.task.calculated_due_date == "2026-11-18"
        calendar = store.update_task_state(case_id, order_key, False, "2026-11-01")
        assert calendar.task.calculated_due_date == "2026-11-11"
        assert calendar.case_urgency == "critical"
        store.update_task_state(case_id, deadline_key, True, None)
        closed = store.update_task_state(case_id, order_key, True, None)
        assert closed.case_urgency == "normal"
        cleared = store.update_task_state(case_id, order_key, True, "")
        assert cleared.task.receipt_date is None
        assert cleared.task.calculated_due_date is None
        detail = store.get_case(case_id)
        assert detail is not None
        assert detail.task_states[deadline_key].is_done is True


def test_knowledge_links_point_to_consultant_search() -> None:
    block = LegalKnowledgeService().build("bankruptcy_individual_accept", ["ст. 36 Федерального закона № 127-ФЗ"])
    assert block.statutory_articles
    for article in block.statutory_articles:
        assert article.url is not None
        assert article.url.startswith("https://www.consultant.ru/search/?q=")
        query = unquote_plus(article.url.split("q=", 1)[1])
        assert "http" not in query
        assert len(query) < 180
    joined = " ".join(item.url or "" for item in block.statutory_articles)
    assert "213.4" in unquote_plus(joined)
    assert any("Пленум ВАС РФ от 22.06.2012 № 35" in unquote_plus(item.url or "") for item in block.statutory_articles)


def test_inn_is_twelve_digits_and_not_attached_to_sro() -> None:
    text = "Должник Гапанович Ольга Афанасьевна, ИНН 390400422108. СРО АУ Ассоциация арбитражных управляющих."
    assert _INN_RE.findall(text) == ["390400422108"]
    analysis = _fallback()
    analysis.parties = [
        PartyInfo(role="Должник", name="Гапанович Ольга Афанасьевна"),
        PartyInfo(role="СРО АУ", name="Ассоциация арбитражных управляющих"),
    ]
    parsed = PdfParseResult(
        pages=[PageText(page=1, text=text)],
        metadata=RegexMetadata(inns=["390400422108"]),
    )
    _merge_regex_facts(parsed, analysis)
    debtor = next(party for party in analysis.parties if party.role == "Должник")
    sro = next(party for party in analysis.parties if party.role == "СРО АУ")
    assert debtor.inn == "390400422108"
    assert sro.inn is None


def test_manager_phrase_does_not_create_debtor() -> None:
    parties = SmartPDFParser._parties("утвердить финансовым управляющим должника Собитнюка Андрея Алексеевича")
    assert parties and parties[0].role == "Финансовый управляющий"
    assert all(party.role != "Должник" for party in parties)
    analysis = _fallback()
    analysis.parties = [
        PartyInfo(role="Должник", name="Собитнюка Андрея Алексеевича"),
        PartyInfo(role="Финансовый управляющий", name="Собитнюк Андрей Алексеевич"),
    ]
    _dedupe_parties(analysis)
    assert len(analysis.parties) == 1
    assert analysis.parties[0].role == "Финансовый управляющий"


def test_month_deadline_moves_off_sunday_and_fixed_date_stays() -> None:
    assert calculate_due_date("2026-11-13", 0, "calendar", 1) == "2026-12-14"
    item = DeadlineItem(
        description="Реализация имущества до 11 мая 2027 года",
        days_limit=180,
        anchor_event="fixed_date",
        due_date="2027-05-11",
        urgency="normal",
        source_quote=_quote("до 11 мая 2027 года"),
    )
    _apply_due_date(item, "2026-11-13")
    assert item.due_date == "2027-05-11"


def test_documents_follow_act_date_and_old_stage_is_not_critical() -> None:
    with tempfile.TemporaryDirectory() as folder:
        store = CaseStore(Path(folder) / "lawtech.db")
        early = _item("leave.pdf", "А21-11942/2026")
        early.analysis.act_date = "2026-10-06"
        early.analysis.stage_code = "bankruptcy_individual_accept"
        early.analysis.case_stage = "Оставление без движения"
        early.analysis.verdict_summary = "Заявление оставить без движения"
        later = _item("decision.pdf", "А21-11942/2026")
        later.analysis.act_date = "2026-11-13"
        later.analysis.stage_code = "bankruptcy_individual_realization"
        later.analysis.case_stage = "Реализация имущества гражданина"
        later.analysis.verdict_summary = "Признать банкротом и ввести реализацию имущества"
        later.analysis.deadlines = [
            DeadlineItem(
                description="Завершить реализацию",
                days_limit=180,
                anchor_event="fixed_date",
                due_date="2027-05-11",
                urgency="normal",
                source_quote=_quote("до 11 мая 2027 года"),
            )
        ]
        later.analysis.court_orders = []
        case_id = store.save_document(early)
        store.save_document(later)
        detail = store.get_case(case_id)
        assert detail is not None
        assert [item.filename for item in detail.documents] == ["decision.pdf", "leave.pdf"]
        assert detail.stage_code == "bankruptcy_individual_realization"
        listed = store.list_cases()
        assert listed[0].open_tasks_count == 1
        assert detail.urgency == "normal"


def test_realization_card_states_enforcement_end_and_manager_only_deals() -> None:
    service = LegalKnowledgeService()
    block = service.build("bankruptcy_individual_realization", [])
    text = " ".join(block.legal_consequences + block.mandatory_obligations + block.rights_and_restrictions)
    articles = " ".join(item.article for item in block.statutory_articles)
    assert "оканчиваются" in text
    assert "только финансовый управляющий" in text
    assert "213.30" in articles
    assert "3 рабочих дней" in text
    legacy = service.build("bankruptcy_individual_intro", [])
    assert legacy.stage_title == "Реализация имущества гражданина"


def test_quote_split_across_pages_stays_verified() -> None:
    from app.services.ai_extractor import _verify_quotes

    analysis = _fallback()
    analysis.key_points = [
        QuoteReference(
            page_number=1,
            exact_quote="реализации имущества гражданина до одиннадцатого мая",
        )
    ]
    pages = [
        PageText(page=1, text="Суд решил ввести процедуру реализации"),
        PageText(page=2, text="имущества гражданина до одиннадцатого мая две тысячи двадцать седьмого года"),
    ]
    assert _verify_quotes(pages, analysis) == []
    assert analysis.key_points[0].verified is True


def test_deadline_with_same_quote_as_order_is_not_duplicated() -> None:
    from app.services.ai_extractor import _drop_duplicate_deadlines

    shared = _quote("представить кандидатуру в течение девяти дней")
    analysis = _fallback()
    analysis.court_orders = [
        CourtOrderItem(
            actor="СРО",
            action="представить кандидатуру",
            days_limit=9,
            anchor_event="receipt_date",
            source_quote=shared,
        )
    ]
    analysis.deadlines = [
        DeadlineItem(
            description="представить кандидатуру",
            days_limit=9,
            anchor_event="receipt_date",
            urgency="critical",
            source_quote=_quote("представить кандидатуру в течение девяти дней"),
        ),
        DeadlineItem(
            description="подать апелляционную жалобу",
            days_limit=9,
            anchor_event="receipt_date",
            urgency="critical",
            source_quote=_quote("апелляционная жалоба подается в течение девяти дней"),
        ),
    ]
    _drop_duplicate_deadlines(analysis)
    assert [item.description for item in analysis.deadlines] == ["подать апелляционную жалобу"]
    assert analysis.court_orders[0].actor == "СРО"


def test_token_usage_reads_prompt_and_completion() -> None:
    from types import SimpleNamespace

    from app.services.ai_extractor import _read_token_usage

    usage = _read_token_usage(
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens=3200, completion_tokens=640, total_tokens=3840))
    )
    assert usage == {"prompt_tokens": 3200, "completion_tokens": 640, "total_tokens": 3840}
