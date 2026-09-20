import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


def test_readme_local_links_and_required_setup_files_exist():
    root = Path(__file__).resolve().parents[2]
    required = {".env.example", ".dockerignore", "docs/billing.md", "docs/architecture.md"}
    for target in re.findall(r"\]\(([^\s)]+)\)", (root / "README.md").read_text()):
        url = urlsplit(target)
        if not url.scheme and not url.netloc and url.path:
            required.add(unquote(url.path))
    for path in required:
        assert (root / path).is_file(), f"Missing repository file: {path}"
