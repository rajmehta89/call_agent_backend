import os
from pathlib import Path

from dotenv import load_dotenv


def load_project_env() -> str | None:
    backend_dir = Path(__file__).resolve().parent
    local_env = backend_dir / ".env"
    loaded_path: str | None = None

    if local_env.exists():
        load_dotenv(local_env, override=False)
        loaded_path = str(local_env)

    return loaded_path
