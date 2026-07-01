"""API route definitions for docmind."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")


@router.get("/documents")
async def list_documents():
    return {"documents": []}


@router.get("/documents/{document_id}")
async def get_document(document_id: str):
    return {"document_id": document_id}
