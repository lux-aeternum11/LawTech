"""Локальное хранилище дел и разобранных PDF."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional

from app.models.schemas import (
    CaseCard,
    CaseDetailResponse,
    CaseListItem,
    DocumentAnalysisResult,
    DocumentUploadItem,
    TaskStateItem,
    TaskStateUpdateResponse,
    Urgency,
)
from app.services.ai_extractor import calculate_due_date
from app.services.legal_knowledge import STAGE_TITLES, LegalKnowledgeService
from app.services.pdf_parser import canonical_case_number

DB_PATH = Path(__file__).resolve().parents[2] / "lawtech.db"


class CaseStore:
    """SQLite-файл backend/lawtech.db создаётся при первом запуске."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self.uploads = path.parent / "uploads"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.uploads.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_number TEXT NOT NULL UNIQUE,
                    court_name TEXT,
                    judge_name TEXT,
                    stage_code TEXT,
                    case_stage TEXT,
                    case_status TEXT,
                    urgency TEXT NOT NULL DEFAULT 'normal',
                    next_hearing_date TEXT,
                    nearest_deadline TEXT,
                    verdict_summary TEXT,
                    extraction_mode TEXT,
                    card_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL,
                    case_number TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    extraction_mode TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (case_id) REFERENCES cases(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS task_states (
                    case_id INTEGER NOT NULL,
                    task_key TEXT NOT NULL,
                    is_done INTEGER NOT NULL DEFAULT 0,
                    receipt_date TEXT,
                    calculated_due_date TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (case_id, task_key),
                    FOREIGN KEY (case_id) REFERENCES cases(id) ON DELETE CASCADE
                );
                """
            )

    def save_document(self, item: DocumentUploadItem, pdf_bytes: Optional[bytes] = None) -> int:
        analysis = item.analysis
        analysis.case_number = _stored_case_number(analysis.case_number)
        case_id = self._upsert_case(
            case_number=analysis.case_number,
            court_name=analysis.court_name,
            judge_name=analysis.judge_name,
            stage_code=analysis.stage_code,
            case_stage=analysis.case_stage,
            case_status=analysis.document_type,
            urgency=_urgency_from_analysis(analysis),
            next_hearing_date=analysis.next_hearing.date if analysis.next_hearing else None,
            nearest_deadline=_nearest_deadline(analysis),
            verdict_summary=analysis.verdict_summary,
            card=analysis.model_dump(mode="json"),
            extraction_mode=item.extraction_mode,
        )
        item.case_id = case_id
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM documents WHERE case_id = ? AND filename = ?",
                (case_id, item.filename),
            ).fetchone()
            if existing is not None:
                doc_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE documents
                    SET case_number = ?, page_count = ?, extraction_mode = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (analysis.case_number, item.page_count, item.extraction_mode, _now(), doc_id),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO documents (
                        case_id, case_number, filename, page_count, extraction_mode, document_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (case_id, analysis.case_number, item.filename, item.page_count, item.extraction_mode, "{}", _now()),
                )
                doc_id = int(cursor.lastrowid)
            item.doc_id = doc_id
            if pdf_bytes:
                self._write_pdf(doc_id, pdf_bytes)
            item.pdf_url = f"/api/documents/{doc_id}/pdf" if self.pdf_path(doc_id).is_file() else None
            connection.execute(
                "UPDATE documents SET document_json = ? WHERE id = ?",
                (item.model_dump_json(), doc_id),
            )
            documents = self._load_documents(connection, case_id)
            urgency, nearest, hearing = _metrics(documents, self._load_task_states(connection, case_id))
            newest = documents[0].analysis if documents else None
            connection.execute(
                """
                UPDATE cases
                SET stage_code = ?, case_stage = ?, verdict_summary = ?, judge_name = ?,
                    urgency = ?, nearest_deadline = ?, next_hearing_date = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    newest.stage_code if newest else None,
                    newest.case_stage if newest else None,
                    newest.verdict_summary if newest else None,
                    newest.judge_name if newest else None,
                    urgency,
                    nearest,
                    hearing,
                    _now(),
                    case_id,
                ),
            )
        return case_id

    def save_court_card(self, card: CaseCard) -> int:
        card.case_number = _stored_case_number(card.case_number)
        stage_code, case_stage = _stage_from_card(card)
        hearing = _next_future_hearing(card)
        urgency = "critical" if _within_days(hearing, 3) else "normal"
        return self._upsert_case(
            case_number=card.case_number,
            court_name=card.court_name,
            judge_name=card.judge_name,
            stage_code=stage_code,
            case_stage=case_stage,
            case_status=card.case_status,
            urgency=urgency,
            next_hearing_date=hearing,
            nearest_deadline=None,
            verdict_summary=card.note or card.case_status,
            card=card.model_dump(mode="json"),
            extraction_mode=card.mode,
        )

    def list_cases(self) -> list[CaseListItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, case_number, court_name, judge_name, stage_code, case_stage, case_status,
                       urgency, next_hearing_date, nearest_deadline, verdict_summary, extraction_mode, updated_at
                FROM cases
                ORDER BY CASE urgency WHEN 'critical' THEN 0 ELSE 1 END,
                         COALESCE(next_hearing_date, nearest_deadline, '9999-12-31'),
                         updated_at DESC
                """
            ).fetchall()
            items: list[CaseListItem] = []
            for row in rows:
                data = dict(row)
                case_id = int(data["id"])
                documents = self._load_documents(connection, case_id)
                states = self._load_task_states(connection, case_id)
                done, open_count = _task_counts(documents, states)
                data.update(_header_from_documents(documents, states))
                data["document_count"] = len(documents)
                data["done_tasks_count"] = done
                data["open_tasks_count"] = open_count
                items.append(CaseListItem(**data))
        return items

    def get_case(self, case_id: int) -> Optional[CaseDetailResponse]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, case_number, court_name, judge_name, stage_code, case_stage, case_status,
                       urgency, next_hearing_date, nearest_deadline, verdict_summary, extraction_mode,
                       updated_at, card_json
                FROM cases WHERE id = ?
                """,
                (case_id,),
            ).fetchone()
            if row is None:
                return None
            documents = self._load_documents(connection, case_id)
            states = self._load_task_states(connection, case_id)
        data = dict(row)
        data.update(_header_from_documents(documents, states))
        card = json.loads(data.pop("card_json"))
        return CaseDetailResponse(**data, card=card, documents=documents, task_states=states)

    def delete_case(self, case_id: int) -> bool:
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            ids = connection.execute("SELECT id FROM documents WHERE case_id = ?", (case_id,)).fetchall()
            deleted = connection.execute("DELETE FROM cases WHERE id = ?", (case_id,)).rowcount
        if deleted:
            for row in ids:
                path = self.pdf_path(int(row["id"]))
                if path.is_file():
                    path.unlink()
        return deleted > 0

    def pdf_path(self, doc_id: int) -> Path:
        return self.uploads / f"{doc_id}.pdf"

    def update_task_state(
        self,
        case_id: int,
        task_key: str,
        is_done: Optional[bool],
        receipt_date: Optional[str],
    ) -> TaskStateUpdateResponse:
        with self._connect() as connection:
            case = connection.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
            if case is None:
                raise LookupError("Дело не найдено")
            located = _locate_task(self._load_documents(connection, case_id), task_key)
            if located is None:
                raise LookupError("Задача не найдена")
            _days_limit, days_type, months_limit = located
            current = connection.execute(
                "SELECT * FROM task_states WHERE case_id = ? AND task_key = ?",
                (case_id, task_key),
            ).fetchone()
            done = bool(current["is_done"]) if current is not None and is_done is None else bool(is_done)
            stored_receipt = current["receipt_date"] if current is not None else None
            calculated = current["calculated_due_date"] if current is not None else None
            if receipt_date is not None:
                if receipt_date == "":
                    stored_receipt = None
                    calculated = None
                else:
                    if _parse_iso(receipt_date) is None:
                        raise ValueError("Дата получения должна быть в формате YYYY-MM-DD")
                    stored_receipt = receipt_date
                    if months_limit:
                        calculated = calculate_due_date(receipt_date, _days_limit or 0, days_type, months_limit)
                    elif _days_limit is not None:
                        calculated = calculate_due_date(receipt_date, _days_limit, days_type)
                    else:
                        calculated = None
            now = _now()
            connection.execute(
                """
                INSERT INTO task_states (
                    case_id, task_key, is_done, receipt_date, calculated_due_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id, task_key) DO UPDATE SET
                    is_done = excluded.is_done,
                    receipt_date = excluded.receipt_date,
                    calculated_due_date = excluded.calculated_due_date,
                    updated_at = excluded.updated_at
                """,
                (case_id, task_key, int(done), stored_receipt, calculated, now),
            )
            urgency, nearest, hearing = self._refresh_case_metrics(connection, case_id)
            connection.execute(
                """
                UPDATE cases
                SET urgency = ?, nearest_deadline = ?, next_hearing_date = ?, updated_at = ?
                WHERE id = ?
                """,
                (urgency, nearest, hearing, now, case_id),
            )
        task = TaskStateItem(
            task_key=task_key,
            is_done=done,
            receipt_date=stored_receipt,
            calculated_due_date=calculated,
            updated_at=now,
        )
        return TaskStateUpdateResponse(task=task, case_urgency=urgency, case_nearest_deadline=nearest)

    def _upsert_case(
        self,
        *,
        case_number: str,
        court_name: str,
        judge_name: Optional[str],
        stage_code: str,
        case_stage: str,
        case_status: str,
        urgency: str,
        next_hearing_date: Optional[str],
        nearest_deadline: Optional[str],
        verdict_summary: str,
        card: dict,
        extraction_mode: str,
    ) -> int:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cases (
                    case_number, court_name, judge_name, stage_code, case_stage, case_status,
                    urgency, next_hearing_date, nearest_deadline, verdict_summary, card_json,
                    extraction_mode, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_number) DO UPDATE SET
                    court_name = excluded.court_name,
                    judge_name = excluded.judge_name,
                    stage_code = excluded.stage_code,
                    case_stage = excluded.case_stage,
                    case_status = excluded.case_status,
                    urgency = excluded.urgency,
                    next_hearing_date = excluded.next_hearing_date,
                    nearest_deadline = excluded.nearest_deadline,
                    verdict_summary = excluded.verdict_summary,
                    card_json = excluded.card_json,
                    extraction_mode = excluded.extraction_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    case_number,
                    court_name,
                    judge_name,
                    stage_code,
                    case_stage,
                    case_status,
                    urgency,
                    next_hearing_date,
                    nearest_deadline,
                    verdict_summary,
                    json.dumps(card, ensure_ascii=False),
                    extraction_mode,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM cases WHERE case_number = ?",
                (case_number,),
            ).fetchone()
        return int(row["id"])

    def _write_pdf(self, doc_id: int, data: bytes) -> None:
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.pdf_path(doc_id).write_bytes(data)

    def _load_documents(self, connection: sqlite3.Connection, case_id: int) -> list[DocumentUploadItem]:
        rows = connection.execute(
            "SELECT id, document_json FROM documents WHERE case_id = ? ORDER BY id DESC",
            (case_id,),
        ).fetchall()
        documents: list[DocumentUploadItem] = []
        for row in rows:
            item = DocumentUploadItem.model_validate_json(row["document_json"])
            item.doc_id = int(row["id"])
            item.case_id = case_id
            item.pdf_url = f"/api/documents/{item.doc_id}/pdf" if self.pdf_path(item.doc_id).is_file() else None
            _refresh_saved_analysis(item.analysis)
            documents.append(item)
        documents.sort(key=lambda item: (item.analysis.act_date or "", item.doc_id or 0), reverse=True)
        return documents

    def _load_task_states(self, connection: sqlite3.Connection, case_id: int) -> dict[str, TaskStateItem]:
        rows = connection.execute(
            "SELECT task_key, is_done, receipt_date, calculated_due_date, updated_at FROM task_states WHERE case_id = ?",
            (case_id,),
        ).fetchall()
        return {
            row["task_key"]: TaskStateItem(
                task_key=row["task_key"],
                is_done=bool(row["is_done"]),
                receipt_date=row["receipt_date"],
                calculated_due_date=row["calculated_due_date"],
                updated_at=row["updated_at"],
            )
            for row in rows
        }

    def _refresh_case_metrics(
        self,
        connection: sqlite3.Connection,
        case_id: int,
    ) -> tuple[Urgency, Optional[str], Optional[str]]:
        return _metrics(self._load_documents(connection, case_id), self._load_task_states(connection, case_id))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


_TASK_KEY = re.compile(r"^doc:(\d+):(order|deadline):(\d+)$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stored_case_number(value: str) -> str:
    if not value or value == "НЕ ИЗВЛЕЧЁН":
        return value
    return canonical_case_number(value)


def _parse_iso(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _locate_task(
    documents: list[DocumentUploadItem],
    task_key: str,
) -> Optional[tuple[Optional[int], str]]:
    match = _TASK_KEY.match(task_key)
    if match is None:
        return None
    doc_id = int(match.group(1))
    index = int(match.group(3))
    document = next((item for item in documents if item.doc_id == doc_id), None)
    if document is None:
        return None
    items = document.analysis.court_orders if match.group(2) == "order" else document.analysis.deadlines
    if index < 0 or index >= len(items):
        return None
    found = items[index]
    return found.days_limit, found.days_type, found.months_limit or 0


def _task_counts(documents: list[DocumentUploadItem], states: dict[str, TaskStateItem]) -> tuple[int, int]:
    total = 0
    done = 0
    for document in documents:
        if _is_archived(document, documents):
            continue
        for kind, items in (
            ("order", document.analysis.court_orders),
            ("deadline", document.analysis.deadlines),
        ):
            for index, _item in enumerate(items):
                total += 1
                state = states.get(f"doc:{document.doc_id}:{kind}:{index}")
                if state is not None and state.is_done:
                    done += 1
    return done, total - done


def _metrics(
    documents: list[DocumentUploadItem],
    states: dict[str, TaskStateItem],
) -> tuple[Urgency, Optional[str], Optional[str]]:
    dues: list[str] = []
    hearings: list[str] = []
    urgent = False
    for document in documents:
        if _is_archived(document, documents):
            continue
        analysis = document.analysis
        if analysis.next_hearing and analysis.next_hearing.date:
            hearings.append(analysis.next_hearing.date)
        groups = (
            ("order", analysis.court_orders),
            ("deadline", analysis.deadlines),
        )
        for kind, items in groups:
            for index, item in enumerate(items):
                state = states.get(f"doc:{document.doc_id}:{kind}:{index}")
                if state is not None and state.is_done:
                    continue
                due = state.calculated_due_date if state is not None and state.calculated_due_date else item.due_date
                if due:
                    dues.append(due)
                if _open_task_is_urgent(item, due):
                    urgent = True
    nearest = min(dues) if dues else None
    hearing = min(hearings) if hearings else None
    if _within_days(nearest, 10) or _within_days(hearing, 3):
        urgent = True
    urgency: Urgency = "critical" if urgent else "normal"
    return urgency, nearest, hearing


def _header_from_documents(
    documents: list[DocumentUploadItem],
    states: dict[str, TaskStateItem],
) -> dict[str, object]:
    urgency, nearest, hearing = _metrics(documents, states)
    newest = documents[0].analysis if documents else None
    header: dict[str, object] = {
        "urgency": urgency,
        "nearest_deadline": nearest,
        "next_hearing_date": hearing,
    }
    if newest is not None:
        header["stage_code"] = newest.stage_code
        header["case_stage"] = newest.case_stage
        header["verdict_summary"] = newest.verdict_summary
        header["judge_name"] = newest.judge_name
    return header


def _refresh_saved_analysis(analysis: DocumentAnalysisResult) -> None:
    """Старые карточки intro при показе становятся реализацией или реструктуризацией, справка берётся из кодекса."""

    summary = (analysis.verdict_summary or "").lower().replace("ё", "е")
    if analysis.stage_code == "bankruptcy_individual_intro":
        analysis.stage_code = (
            "bankruptcy_individual_restructuring"
            if "реструктуризац" in summary and "реализац" not in summary
            else "bankruptcy_individual_realization"
        )
        analysis.case_stage = STAGE_TITLES[analysis.stage_code]
    analysis.legal_basis = [note.replace("(банкротстве»)", "(банкротстве)»") for note in analysis.legal_basis]
    analysis.legal_knowledge = LegalKnowledgeService().build(analysis.stage_code, analysis.legal_basis)


def _procedure_rank(analysis: DocumentAnalysisResult) -> int:
    blob = f"{analysis.stage_code} {analysis.case_stage} {analysis.verdict_summary}".lower().replace("ё", "е")
    if analysis.stage_code in {
        "bankruptcy_individual_restructuring",
        "bankruptcy_individual_realization",
        "bankruptcy_individual_intro",
    } or "реализац" in blob or "реструктуризац" in blob or "признан банкротом" in blob or "признать банкротом" in blob:
        return 2
    if "без движения" in blob:
        return 0
    return 1


def _is_archived(document: DocumentUploadItem, documents: list[DocumentUploadItem]) -> bool:
    own_date = document.analysis.act_date or ""
    if not own_date:
        return False
    own_rank = _procedure_rank(document.analysis)
    for other in documents:
        other_date = other.analysis.act_date or ""
        if other is document or not other_date:
            continue
        if other_date > own_date and _procedure_rank(other.analysis) > own_rank:
            return True
    return False


def _open_task_is_urgent(item: object, due: Optional[str]) -> bool:
    months = getattr(item, "months_limit", None) or 0
    days_limit = getattr(item, "days_limit", None)
    if due:
        parsed = _parse_iso(due)
        if parsed is None:
            return False
        delta = (parsed - date.today()).days
        return -30 <= delta <= 10
    if isinstance(months, int) and months >= 1:
        return False
    if isinstance(days_limit, int) and days_limit > 10:
        return False
    return isinstance(days_limit, int) and 1 <= days_limit <= 10


def _urgency_from_analysis(analysis: DocumentAnalysisResult) -> str:
    if any(item.urgency == "critical" for item in analysis.deadlines):
        return "critical"
    nearest = _nearest_deadline(analysis)
    if _within_days(nearest, 10) or _within_days(
        analysis.next_hearing.date if analysis.next_hearing else None,
        3,
    ):
        return "critical"
    return "normal"


def _nearest_deadline(analysis: DocumentAnalysisResult) -> Optional[str]:
    dates = [item.due_date for item in analysis.deadlines if item.due_date]
    dates.extend(item.due_date for item in analysis.court_orders if item.due_date)
    return min(dates) if dates else None


def _within_days(value: Optional[str], limit: int) -> bool:
    if not value:
        return False
    try:
        target = date.fromisoformat(value)
    except ValueError:
        return False
    return 0 <= (target - date.today()).days <= limit


def _next_future_hearing(card: CaseCard) -> Optional[str]:
    today = date.today().isoformat()
    future = sorted(item.date for item in card.hearings if item.date >= today)
    if future:
        return future[0]
    return None


def _stage_from_card(card: CaseCard) -> tuple[str, str]:
    category = (card.category or "").lower()
    title = card.category or "Арбитражный спор"
    if "банкрот" in category:
        debtor_names = " ".join(party.name.lower() for party in card.parties if party.role == "Должник")
        if any(marker in debtor_names for marker in ("ооо", "ао ", "пао", "зао")):
            return "bankruptcy_legal_entity", title
        return "bankruptcy_individual_accept", title
    return "general_arbitration_dispute", title
