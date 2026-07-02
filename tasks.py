"""
Invoke tasks for the ERA5-Slovenia / Podnebnik data pipeline.

Usage:
    uv run invoke validate
    uv run invoke create-databases
    uv run invoke datasette
    uv run invoke precompute       # run mk_precompute.py then create-databases
"""

import glob, io, json, shutil, sys
from pathlib import Path
from invoke import task
from frictionless import Package

BASE_DIR     = Path(__file__).parent
DATASETS_DIR = BASE_DIR / "data"
SQLITE_DIR   = BASE_DIR / "var" / "sqlite"
DATASETTE_PORT = 8001


class Color:
    from colorama import Fore, Style

    @staticmethod
    def success(text):
        return f"{Color.Fore.GREEN}{text}{Color.Style.RESET_ALL}"

    @staticmethod
    def failure(text):
        return f"{Color.Fore.RED}{text}{Color.Style.RESET_ALL}"


def log(msg):
    print(msg, file=sys.stderr)


def get_datapackage_paths():
    return [Path(p) for p in glob.glob(str(DATASETS_DIR / "*/datapackage.yaml"))]


def none_if_empty(value):
    return value if value else None


@task
def validate(c):
    """Validate available Frictionless data packages."""
    log("Validating data packages:\n")
    invalid = []
    for path in get_datapackage_paths():
        pkg = Package(path)
        log(f"  Name: {pkg.name}")
        log(f"  Path: {path}")
        report = pkg.validate()
        if report.valid:
            log(f"Status: {Color.success('valid')}")
        else:
            log(f"Status: {Color.failure('invalid')}")
            invalid.append(path)
        log("")
    if not invalid:
        log("All data packages are valid.")
    else:
        log("Invalid packages:")
        for p in invalid:
            log(f"  frictionless validate {p}")
        sys.exit(1)


@task(iterable=["no_validate_arch"])
def create_databases(c, no_validate=False, no_validate_arch=None):
    """Import Frictionless data packages into SQLite and generate Datasette metadata."""
    if not no_validate:
        validate(c)

    shutil.rmtree(SQLITE_DIR, ignore_errors=True)
    SQLITE_DIR.mkdir(parents=True, exist_ok=True)

    metadata = {
        "title": "Slovenia Climate Explorer",
        "description": "ERA5-Land climate data for Slovenia.",
        "databases": {},
    }
    databases = []

    for package_path in get_datapackage_paths():
        pkg = Package(package_path)
        db  = SQLITE_DIR / f"{pkg.name}.db"
        databases.append(db)
        log(f"\nImporting {pkg.name}:")

        metadata["databases"][pkg.name] = {
            "title":       pkg.title,
            "description": pkg.description,
            "tables": {},
        }

        for resource in pkg.resources:
            csv_path = DATASETS_DIR / package_path.parent.relative_to(DATASETS_DIR) / resource.path
            if resource.format != "csv":
                log(f"    Skipping {resource.name} ({resource.format})")
                continue

            log(f"    Importing {resource.name} from {csv_path.name}")
            metadata["databases"][pkg.name]["tables"][resource.name] = {
                "title":       resource.title,
                "description": resource.description,
            }
            if hasattr(resource, "schema") and resource.schema.fields:
                metadata["databases"][pkg.name]["tables"][resource.name]["columns"] = {
                    f.name: f.title for f in resource.schema.fields if hasattr(f, "title") and f.title
                }

            # Create table structure (first 10 rows for type detection)
            c.run(
                f"sqlite-utils insert {db} {resource.name} {csv_path} "
                f"--csv --detect-types --silent --stop-after 10",
                warn=True,
            )
            # Bulk-load all rows
            script = io.StringIO()
            script.write(f'delete from "{resource.name}";\n')
            script.write(f".import --csv --skip 1 {csv_path} {resource.name}")
            script.seek(0)
            c.run(f"sqlite3 {db}", in_stream=script)

    # Datasette inspect + metadata
    db_args = " ".join(str(d) for d in databases)
    c.run(f"datasette inspect {db_args} --inspect-file {SQLITE_DIR / 'inspect-data.json'}")
    with open(SQLITE_DIR / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
    log("Done.")


@task
def precompute(c):
    """Run mk_precompute.py then create-databases."""
    log("Running mk_precompute.py…")
    c.run("COUNTRY=si python3 mk_precompute.py")
    create_databases(c, no_validate=True)


@task
def datasette(c):
    """Start local Datasette server on port 8001."""
    log(f"\nStarting Datasette on http://localhost:{DATASETTE_PORT}\n")
    dbs = list(SQLITE_DIR.glob("*.db"))
    if not dbs:
        log("No databases found. Run: uv run invoke create-databases")
        sys.exit(1)
    db_args = " ".join(str(d) for d in dbs)
    c.run(
        f"datasette serve {db_args} "
        f"--inspect-file {SQLITE_DIR}/inspect-data.json "
        f"--metadata {SQLITE_DIR}/metadata.json "
        f"--port {DATASETTE_PORT} --cors "
        f"--setting max_returned_rows 500000"
    )
