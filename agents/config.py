import os

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
DEFAULT_MODEL = os.getenv("LOCAL_AGENT_MODEL", "qwen2.5-coder:14b")
MAX_FEEDBACK_RETRIES = int(os.getenv("MAX_FEEDBACK_RETRIES", "3"))
