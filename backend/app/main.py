"""FastAPI: разбор PDF, карточка дела и радар в одном окне."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.models.schemas import (
    CaseCard,
    CaseDetailResponse,
    CaseListItem,
    DocumentUploadItem,
    DocumentUploadResponse,
    FetchCaseRequest,
    HealthResponse,
    TaskStateUpdateRequest,
    TaskStateUpdateResponse,
)
from app.services.ai_extractor import AILegalExtractor
from app.services.court_scraper import CourtScraperError, CourtScraperService
from app.services.pdf_parser import PdfParseError, SmartPDFParser
from app.services.storage import CaseStore

load_dotenv()
logging.basicConfig(level=logging.INFO)

MAX_FILES = 10
MAX_BYTES = 30 * 1024 * 1024
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="AMD LegalTech",
    summary="Умный загрузчик документов и парсер карточек судебных дел",
    version="0.2.0",
    description=(
        "Конвейер «код → модель → код»: факты берутся из PDF, "
        "даты пересчитываются программой, правовая справка берётся из базы норм."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

parser = SmartPDFParser()
extractor = AILegalExtractor()
scraper = CourtScraperService()
store = CaseStore()


@app.get("/api/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    """Статус сервиса, режим судов и какой LLM подключён."""

    return HealthResponse(
        status="ok",
        mock_scraper=scraper.use_mock,
        llm_configured=extractor.is_configured,
        llm_model=extractor.model if extractor.is_configured else None,
        llm_provider=extractor.provider_name,
        version=app.version,
    )


@app.post("/api/documents/upload", response_model=DocumentUploadResponse, tags=["documents"])
async def upload_documents(
    files: list[UploadFile] = File(..., description="Один или несколько PDF судебных актов"),
) -> DocumentUploadResponse:
    """Разобрать PDF, проверить цитаты и сохранить дело в SQLite."""

    if not files:
        raise HTTPException(status_code=400, detail="Прикрепите хотя бы один PDF")
    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"За один запрос можно загрузить не больше {MAX_FILES} файлов",
        )

    documents: list[DocumentUploadItem] = []
    for upload in files:
        content = await upload.read()
        filename = upload.filename or "document.pdf"
        if len(content) > MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"{filename}: файл больше 30 МБ")
        if not content.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail=f"{filename}: ожидается PDF")
        try:
            parsed = await asyncio.to_thread(parser.parse, content)
            outcome = await asyncio.to_thread(extractor.extract, parsed)
        except PdfParseError as exc:
            raise HTTPException(status_code=400, detail=f"{filename}: {exc.message}") from exc
        item = DocumentUploadItem(
            filename=filename,
            page_count=len(parsed.pages),
            pages=parsed.pages,
            regex_metadata=parsed.metadata,
            analysis=outcome.analysis,
            extraction_mode=outcome.extraction_mode,
            warnings=outcome.warnings,
            token_usage=outcome.token_usage,
        )
        store.save_document(item, content)
        documents.append(item)
    return DocumentUploadResponse(
        documents=documents,
        case_id=documents[0].case_id if documents else None,
    )


@app.post("/api/cases/fetch-by-number", response_model=CaseCard, tags=["cases"])
async def fetch_case_by_number(payload: FetchCaseRequest) -> CaseCard:
    """Подтянуть карточку по номеру дела и сохранить её в радаре."""

    try:
        card = await scraper.fetch_case_by_number(payload.case_number)
    except CourtScraperError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    card.case_id = store.save_court_card(card)
    return card


@app.get("/api/cases", response_model=list[CaseListItem], tags=["cases"])
async def list_cases() -> list[CaseListItem]:
    """Список дел: сначала срочные, затем ближайшие даты."""

    return store.list_cases()


@app.get("/api/cases/{case_id}", response_model=CaseDetailResponse, tags=["cases"])
async def get_case(case_id: int) -> CaseDetailResponse:
    """Полная карточка и все PDF этого дела."""

    found = store.get_case(case_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    return found


@app.patch(
    "/api/cases/{case_id}/tasks/{task_key}",
    response_model=TaskStateUpdateResponse,
    tags=["cases"],
)
async def update_task(case_id: int, task_key: str, payload: TaskStateUpdateRequest) -> TaskStateUpdateResponse:
    """Отметить задачу выполненной или посчитать срок от даты получения."""

    try:
        return store.update_task_state(case_id, task_key, payload.is_done, payload.receipt_date)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/documents/{doc_id}/pdf", tags=["documents"])
async def download_pdf(doc_id: int) -> FileResponse:
    """Оригинал загруженного PDF."""

    path = store.pdf_path(doc_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="PDF не найден")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.delete("/api/cases/{case_id}", tags=["cases"])
async def delete_case(case_id: int) -> dict[str, bool]:
    """Удалить дело и привязанные документы."""

    if not store.delete_case(case_id):
        raise HTTPException(status_code=404, detail="Дело не найдено")
    return {"deleted": True}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
