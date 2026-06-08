"""Canon domain initializer — scaffolds new domains from template."""

from pathlib import Path
import shutil


def init_domain(domain_slug: str, repo_root: Path | None = None) -> Path:
    """Copy _template to a new domain folder and replace placeholders."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    template_path = repo_root / "domains" / "_template"
    target_path = repo_root / "domains" / domain_slug

    if target_path.exists():
        raise FileExistsError(f"Domain '{domain_slug}' already exists at {target_path}")

    if not template_path.exists():
        raise FileNotFoundError(f"Template not found at {template_path}")

    shutil.copytree(template_path, target_path)

    # Replace placeholder in all files
    for filepath in target_path.rglob("*"):
        if filepath.is_file():
            content = filepath.read_text(encoding="utf-8")
            if "__DOMAIN_SLUG__" in content:
                filepath.write_text(
                    content.replace("__DOMAIN_SLUG__", domain_slug),
                    encoding="utf-8",
                )

    # Create bootstrap-docs folder
    bootstrap_path = repo_root / "bootstrap-docs" / domain_slug
    bootstrap_path.mkdir(parents=True, exist_ok=True)

    # Create evals folder with starter questions file
    evals_path = repo_root / "evals" / domain_slug
    evals_path.mkdir(parents=True, exist_ok=True)
    questions_file = evals_path / "questions.yaml"
    if not questions_file.exists():
        questions_file.write_text(
            f"# yaml-language-server: $schema=../../schemas/questions.schema.json\n"
            f'schema_version: "1.0"\n'
            f"domain: {domain_slug}\n"
            f"questions: []\n",
            encoding="utf-8",
        )

    return target_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python canon_init.py <domain-slug>")
        sys.exit(1)

    path = init_domain(sys.argv[1])
    print(f"Domain scaffolded at: {path}")
