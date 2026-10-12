"""Python dependency validation used by application entry points."""
from typing import List
from pathlib import Path
from typing import Tuple
import importlib.metadata as importlib_metadata
import re
import sys

REQUIREMENTS_FILE = Path(__file__).resolve().parent.parent / "requirements.txt"

def _requirement_version_tuple(
    value: str,
) -> Tuple[int, ...]:
    """
    Convert a normal package version to a comparable numeric tuple.

    Harmonia's requirements currently use simple >= minimum versions,
    so a lightweight stdlib-only comparison is sufficient during bootstrap.
    """
    numbers = re.findall(
        r"\d+",
        str(value or ""),
    )

    if not numbers:
        return ()

    return tuple(
        int(number)
        for number in numbers[:6]
    )


def _version_meets_minimum(
    installed: str,
    minimum: str,
) -> bool:
    """Return whether an installed version satisfies a >= minimum."""
    installed_parts = list(
        _requirement_version_tuple(
            installed
        )
    )
    minimum_parts = list(
        _requirement_version_tuple(
            minimum
        )
    )

    if (
        not installed_parts
        or not minimum_parts
    ):
        return True

    width = max(
        len(installed_parts),
        len(minimum_parts),
    )

    installed_parts.extend(
        [0]
        * (
            width
            - len(installed_parts)
        )
    )
    minimum_parts.extend(
        [0]
        * (
            width
            - len(minimum_parts)
        )
    )

    return tuple(
        installed_parts
    ) >= tuple(
        minimum_parts
    )


def _parse_requirements_file(
    requirements_path: Path,
    _seen=None,
) -> List[dict]:
    """Parse Harmonia requirements.txt entries."""
    requirements = []
    seen=set() if _seen is None else _seen
    resolved=requirements_path.resolve()
    if resolved in seen:raise ValueError("Recursive requirements include")
    seen.add(resolved)

    for raw_line in requirements_path.read_text(
        encoding="utf-8"
    ).splitlines():
        line = raw_line.split(
            "#",
            1,
        )[0].strip()

        if not line:
            continue

        if line.startswith("-r "):
            included=(requirements_path.parent/line[3:].strip()).resolve()
            if included.parent!=requirements_path.resolve().parent:raise ValueError("Requirements include must stay in the application directory")
            requirements.extend(_parse_requirements_file(included,seen))
            continue
        if line.startswith("-"):
            continue

        line = line.split(
            ";",
            1,
        )[0].strip()

        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)"
            r"(?:\[[^\]]+\])?"
            r"\s*"
            r"(?:(>=|==|<=|>|<|~=)\s*([^\s]+))?",
            line,
        )

        if not match:
            requirements.append(
                {
                    "name": line,
                    "operator": "",
                    "version": "",
                    "parse_error": True,
                }
            )
            continue

        requirements.append(
            {
                "name": match.group(1),
                "operator": (
                    match.group(2)
                    or ""
                ),
                "version": (
                    match.group(3)
                    or ""
                ),
                "parse_error": False,
            }
        )

    return requirements


def _check_requirements(
    requirements_path: Path = None,
    version_lookup=None,
) -> dict:
    """
    Check requirements.txt before importing third-party packages.

    This uses importlib.metadata from the Python standard library, allowing
    Harmonia to report all missing/outdated dependencies at once.
    """
    path = (
        Path(requirements_path)
        if requirements_path is not None
        else REQUIREMENTS_FILE
    )

    if version_lookup is None:
        version_lookup = (
            importlib_metadata.version
        )

    result = {
        "ok": False,
        "requirements_file": str(
            path
        ),
        "file_missing": False,
        "missing": [],
        "outdated": [],
        "unparsed": [],
        "checked": [],
    }

    if not path.is_file():
        result[
            "file_missing"
        ] = True
        return result

    try:
        requirements = (
            _parse_requirements_file(
                path
            )
        )
    except (
        OSError,
        UnicodeError,
    ):
        result[
            "file_missing"
        ] = True
        return result

    for requirement in requirements:
        if requirement[
            "parse_error"
        ]:
            result[
                "unparsed"
            ].append(
                requirement["name"]
            )
            continue

        name = requirement[
            "name"
        ]

        try:
            installed = str(
                version_lookup(
                    name
                )
            )
        except (
            importlib_metadata.PackageNotFoundError,
            KeyError,
        ):
            result[
                "missing"
            ].append(
                {
                    "name": name,
                    "required": (
                        f"{requirement['operator']}"
                        f"{requirement['version']}"
                    ),
                }
            )
            continue
        except Exception:
            result[
                "missing"
            ].append(
                {
                    "name": name,
                    "required": (
                        f"{requirement['operator']}"
                        f"{requirement['version']}"
                    ),
                }
            )
            continue

        result[
            "checked"
        ].append(
            {
                "name": name,
                "installed": installed,
                "required": (
                    f"{requirement['operator']}"
                    f"{requirement['version']}"
                ),
            }
        )

        operator = requirement[
            "operator"
        ]
        required_version = requirement[
            "version"
        ]

        if (
            operator == ">="
            and required_version
            and not _version_meets_minimum(
                installed,
                required_version,
            )
        ):
            result[
                "outdated"
            ].append(
                {
                    "name": name,
                    "installed": installed,
                    "required": (
                        f">={required_version}"
                    ),
                }
            )

    result["ok"] = not (
        result[
            "file_missing"
        ]
        or result[
            "missing"
        ]
        or result[
            "outdated"
        ]
        or result[
            "unparsed"
        ]
    )

    return result


def _ensure_requirements():
    """Stop startup with install instructions when requirements fail."""
    result = (
        _check_requirements()
    )

    if result["ok"]:
        return

    print(
        "\n✗ Harmonia cannot start because "
        "its Python requirements are not satisfied."
    )

    if result[
        "file_missing"
    ]:
        print(
            f"\n  requirements.txt was not found at:\n"
            f"  {result['requirements_file']}"
        )
        print(
            "\n  Restore requirements.txt in the application root "
            "and try again."
        )
        raise SystemExit(
            1
        )

    if result[
        "missing"
    ]:
        print(
            "\nMissing packages:"
        )

        for item in result[
            "missing"
        ]:
            print(
                f"  - "
                f"{item['name']}"
                f"{item['required']}"
            )

    if result[
        "outdated"
    ]:
        print(
            "\nPackages that need to be upgraded:"
        )

        for item in result[
            "outdated"
        ]:
            print(
                f"  - "
                f"{item['name']} "
                f"{item['installed']} "
                f"(requires "
                f"{item['required']})"
            )

    if result[
        "unparsed"
    ]:
        print(
            "\nRequirements that could not be checked:"
        )

        for item in result[
            "unparsed"
        ]:
            print(
                f"  - {item}"
            )

    print(
        "\nInstall/update dependencies with:\n"
    )
    print(
        f'  "{sys.executable}" -m pip install '
        f'-r "{REQUIREMENTS_FILE}"'
    )
    print()

    raise SystemExit(
        1
    )


