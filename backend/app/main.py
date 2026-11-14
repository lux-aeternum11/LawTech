"""FastAPI: загрузка судебных PDF и карточка дела по номеру."""

from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.models.schemas import (
    CaseCard,
    DocumentUploadItem,
    DocumentUploadResponse,
    FetchCaseRequest,
    HealthResponse,
)
from app.services.ai_extractor import AILegalExtractor
from app.services.court_scraper import CourtScraperError, CourtScraperService
from app.services.pdf_parser import PdfParseError, SmartPDFParser

load_dotenv()
logging.basicConfig(level=logging.INFO)

MAX_FILES = 10
MAX_BYTES = 30 * 1024 * 1024

app = FastAPI(
    title="AMD LegalTech",
    summary="Умный загрузчик документов и парсер карточек судебных дел",
    version="0.1.0",
    description=(
        "Нулевой ввод: карточка дела собирается из PDF судебного акта "
        "или по номеру дела. Каждый тезис fallback-разбора опирается на цитату из текста."
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


@app.get("/api/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    """Статус сервиса, режим парсера судов и наличие ключа LLM."""

    return HealthResponse(
        status="ok",
        mock_scraper=scraper.use_mock,
        llm_configured=extractor.is_configured,
        version=app.version,
    )


@app.post("/api/documents/upload", response_model=DocumentUploadResponse, tags=["documents"])
async def upload_documents(
    files: list[UploadFile] = File(..., description="Один или несколько PDF судебных актов"),
) -> DocumentUploadResponse:
    """Разобрать PDF и вернуть карточку акта вместе с постраничным текстом."""

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
        documents.append(
            DocumentUploadItem(
                filename=filename,
                page_count=len(parsed.pages),
                pages=parsed.pages,
                regex_metadata=parsed.metadata,
                analysis=outcome.analysis,
                extraction_mode=outcome.extraction_mode,
                warnings=outcome.warnings,
            )
        )
    return DocumentUploadResponse(documents=documents)


@app.post("/api/cases/fetch-by-number", response_model=CaseCard, tags=["cases"])
async def fetch_case_by_number(payload: FetchCaseRequest) -> CaseCard:
    """Подтянуть карточку и хронологию по номеру дела КАД или СОЮ."""

    try:
        return await scraper.fetch_case_by_number(payload.case_number)
    except CourtScraperError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
