"""The release archive: what a second machine gets, and what it must never get."""

import tarfile
from pathlib import Path

from scripts.package import EXCLUDED_NAMES, build, members, wanted


def office(tmp_path: Path) -> Path:
    """A miniature office with a secret and a cache in it."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n")
    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "app" / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "env.py").write_text("\n")
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "root.key").write_text("SECRET")
    (tmp_path / ".env").write_text("OWNER_API_TOKEN=secret-token\n")
    (tmp_path / ".env.example").write_text("OWNER_API_TOKEN=\n")
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "office.tar.gz").write_bytes(b"\x00")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "README.md").write_text("# Office\n")
    return tmp_path


def test_a_secret_is_never_packaged(tmp_path: Path) -> None:
    """The failure this guards against is handing someone the owner's keys."""
    root = office(tmp_path)
    packaged = {path.as_posix() for path in members(root)}
    assert ".env" not in packaged
    assert ".env.example" in packaged, "the template is not a secret and is needed"
    assert not any(".secrets" in name for name in packaged)
    assert not any(name.endswith(".key") for name in packaged)


def test_nothing_private_or_rebuildable_is_packaged(tmp_path: Path) -> None:
    root = office(tmp_path)
    packaged = {path.as_posix() for path in members(root)}
    assert not any("__pycache__" in name for name in packaged)
    assert not any(name.startswith("backups/") for name in packaged)
    for name in EXCLUDED_NAMES:
        assert not wanted(Path(name) / "anything"), name


def test_what_a_second_machine_needs_is_packaged(tmp_path: Path) -> None:
    root = office(tmp_path)
    packaged = {path.as_posix() for path in members(root)}
    assert {"app/main.py", "migrations/env.py", "docker-compose.yml", "README.md"} <= packaged
    # Found by unpacking a real archive: without the template, first start
    # fails with FileNotFoundError and the office never comes up at all.
    assert ".env.example" in packaged


def test_the_archive_says_it_is_not_signed(tmp_path: Path) -> None:
    """A person receiving it must not think macOS's warning is a fault."""
    root = office(tmp_path)
    archive = build(tmp_path / "dist", "9.9.9", root=root)
    assert archive.exists()
    assert (tmp_path / "dist" / f"{archive.name}.sha256").exists()

    with tarfile.open(archive) as bundle:
        names = bundle.getnames()
        release = bundle.extractfile("quarterdeck-9.9.9/RELEASE.json")
        assert release is not None
        body = release.read().decode()
        note = bundle.extractfile("quarterdeck-9.9.9/INSTALL.txt")
        assert note is not None
        install = note.read().decode()
    assert '"signed": false' in body.lower()
    assert "not signed" in install and "not notarised" in install
    assert "python3 -m scripts.launch" in install
    # And the secret is not in the file that was actually written. The
    # template is, and is not a secret: it carries empty values only.
    assert not any(name.endswith("/.env") or name.endswith("root.key") for name in names)
    assert any(name.endswith("/.env.example") for name in names)


def test_every_path_in_the_archive_sits_under_one_folder(tmp_path: Path) -> None:
    """An archive that unpacks into the current directory is a bad guest."""
    root = office(tmp_path)
    archive = build(tmp_path / "dist", "9.9.9", root=root)
    with tarfile.open(archive) as bundle:
        assert all(name.startswith("quarterdeck-9.9.9/") for name in bundle.getnames())


# The real archive, checked against what the real stack builds from. The
# miniature office above has no services, which is how both gaps were missed.

REPO = Path(__file__).resolve().parents[2]


def _copied_by(dockerfile: Path, target: str | None) -> set[str]:
    """What a Dockerfile copies from the build context, in the stages built
    for `target` (every stage up to and including it; all, without one)."""
    stages: list[tuple[str | None, list[str]]] = []
    for line in dockerfile.read_text().splitlines():
        words = line.split()
        if not words:
            continue
        keyword = words[0].upper()
        if keyword == "FROM":
            named = words[-1] if len(words) >= 4 and words[-2].lower() == "as" else None
            stages.append((named, []))
        elif keyword in ("COPY", "ADD") and stages:
            if not any(w.startswith("--from") for w in words):
                stages[-1][1].extend(w for w in words[1:-1] if not w.startswith("--"))
    built = stages
    if target is not None:
        names = [name for name, _ in stages]
        built = stages[: names.index(target) + 1]
    return {source for _, copied in built for source in copied}


def test_every_build_input_of_the_default_stack_is_packaged() -> None:
    import yaml

    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    packaged = {p.as_posix() for p in members(REPO)}
    missing: list[str] = []
    for name, service in compose["services"].items():
        build = service.get("build")
        if not build or service.get("profiles"):
            continue
        dockerfile = build.get("dockerfile", "Dockerfile")
        if dockerfile not in packaged:
            missing.append(f"{name}: {dockerfile}")
        for source in _copied_by(REPO / dockerfile, build.get("target")):
            if source not in packaged and not any(p.startswith(f"{source}/") for p in packaged):
                missing.append(f"{name}: {source}")
    assert not missing, f"a fresh install could not build: {sorted(set(missing))}"


def test_all_of_the_office_code_is_packaged() -> None:
    """`app/sources/` was left out: the rule for the owner's sources folder
    matched the office's own package of the same name."""
    packaged = set(members(REPO))
    code = [
        p.relative_to(REPO)
        for p in (REPO / "app").rglob("*")
        if p.is_file() and not ({"__pycache__", ".DS_Store"} & set(p.parts)) and p.suffix != ".pyc"
    ]
    left_out = sorted(str(p) for p in code if p not in packaged)
    assert not left_out, f"office code missing from the archive: {left_out[:10]}"


def test_the_owners_own_folders_are_still_never_packaged(tmp_path: Path) -> None:
    root = office(tmp_path)
    (root / "sources").mkdir()
    (root / "sources" / "contract.pdf").write_bytes(b"%PDF")
    (root / "workspace").mkdir()
    (root / "workspace" / "notes.md").write_text("private\n")
    names = {p.as_posix() for p in members(root)}
    assert not any(n.startswith(("sources/", "workspace/", "backups/")) for n in names)


def test_packaged_documentation_links_only_to_what_is_packaged() -> None:
    """A link to a document the archive leaves out is dead on a second machine.
    `.omx/` is the planning tool's own folder, logs included; never shipped."""
    import posixpath
    import re

    packaged = {p.as_posix() for p in members(REPO)}
    dead: list[str] = []
    for name in sorted(n for n in packaged if n.endswith(".md")):
        text = (REPO / name).read_text(errors="replace")
        for target in re.findall(r"\]\(([^)\s#]+)(?:#[^)]*)?\)", text):
            if "://" in target or target.startswith("mailto:"):
                continue
            target = target.replace("%20", " ")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if resolved.startswith(".omx/"):
                continue
            if resolved not in packaged and not any(p.startswith(f"{resolved}/") for p in packaged):
                dead.append(f"{name} -> {resolved}")
    assert not dead, dead[:10]
