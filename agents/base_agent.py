import httpx
import logging
from typing import Optional
from .config import OLLAMA_URL, DEFAULT_MODEL

logger = logging.getLogger(__name__)

class BaseLocalAgent:
    """Base class for all local agents interacting with Ollama/Local LLM API."""
    def __init__(self, name: str, role_prompt: str, model: Optional[str] = None):
        self.name = name
        self.role_prompt = role_prompt
        self.model = model or DEFAULT_MODEL

    async def execute(self, task_input: str, system_context: str = "") -> str:
        prompt = f"System Role: {self.role_prompt}\n"
        if system_context:
            prompt += f"System Context:\n{system_context}\n"
        prompt += f"\nUser Task / Input:\n{task_input}"

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                res = await client.post(
                    OLLAMA_URL,
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False
                    }
                )
                if res.status_code == 200:
                    data = res.json()
                    return data.get("response", "").strip()
                else:
                    logger.error(f"[{self.name}] Ollama API returned status {res.status_code}: {res.text}")
                    return f"[{self.name} Error] LLM Server returned status {res.status_code}"
        except Exception as e:
            logger.error(f"[{self.name}] Failed to communicate with Local LLM: {e}")
            return f"[{self.name} Fallback] Could not reach Local LLM at {OLLAMA_URL}. Error: {str(e)}"
