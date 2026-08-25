import os
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_REFERENCE_ENV_PATHS = [
    Path(r"D:\whatsapp_ai_agent\.env"),
    Path(r"D:\whatsapp-ai-chatbot\.env"),
]


def load_project_env() -> str | None:
    backend_dir = Path(__file__).resolve().parent
    local_env = backend_dir / ".env"
    loaded_path: str | None = None

    if local_env.exists():
        load_dotenv(local_env, override=False)
        loaded_path = str(local_env)

    configured_reference = os.getenv("REFERENCE_ENV_FILE")
    candidate_paths = [Path(configured_reference)] if configured_reference else []
    candidate_paths.extend(DEFAULT_REFERENCE_ENV_PATHS)

    for candidate in candidate_paths:
        if candidate and candidate.exists():
            load_dotenv(candidate, override=False)
            loaded_path = loaded_path or str(candidate)
            break

    return loaded_path
