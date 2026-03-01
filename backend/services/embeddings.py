import httpx

from backend.config import OLLAMA_BASE_URL, OLLAMA_EMBED_MODEL


async def embed_text(text: str) -> list[float]:
    """Get embedding vector from Ollama for a single text string."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": OLLAMA_EMBED_MODEL, "input": text},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"][0]


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Get embedding vectors for multiple texts in one call."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": OLLAMA_EMBED_MODEL, "input": texts},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]
