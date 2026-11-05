"""Build a filtered games-direct list and optionally its Mihomo MRS.

The script does not access the network. A caller must fetch every allowlisted
MetaCubeX source at one immutable commit and pass that commit as --source-ref.
All validation happens before existing outputs are replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any


class BuildError(RuntimeError):
    """A fail-closed policy or validation error."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise BuildError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise BuildError(f"expected JSON object: {path}")
    return value


def validate_source_ref(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
        raise BuildError("--source-ref must be a full 40-character Git commit SHA")
    return normalized


def validate_rule(rule: str, path: Path, line_number: int) -> None:
    host = rule[2:] if rule.startswith("+.") else rule
    if not host:
        raise BuildError(f"empty domain rule in {path}:{line_number}")
    if any(char.isspace() for char in rule):
        raise BuildError(f"whitespace inside domain rule in {path}:{line_number}: {rule!r}")
    if any(char in rule for char in ("/", ":", "@", ",", "*")):
        raise BuildError(f"unsupported domain rule in {path}:{line_number}: {rule!r}")
    if host.startswith(".") or host.endswith(".") or ".." in host:
        raise BuildError(f"malformed domain rule in {path}:{line_number}: {rule!r}")


def load_rules_with_hash(path: Path) -> tuple[list[str], str]:
    try:
        raw_bytes = path.read_bytes()
        raw_lines = raw_bytes.decode("utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise BuildError(f"cannot read source {path}: {error}") from error

    rules: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(raw_lines, start=1):
        rule = raw_line.split("#", 1)[0].strip()
        if not rule:
            continue
        validate_rule(rule, path, line_number)
        if rule in seen:
            raise BuildError(f"duplicate rule in {path}:{line_number}: {rule}")
        seen.add(rule)
        rules.append(rule)
    if not rules:
        raise BuildError(f"source contains no rules: {path}")
    return rules, sha256(raw_bytes)


def load_rules(path: Path) -> list[str]:
    rules, _ = load_rules_with_hash(path)
    return rules


def require_integer(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BuildError(f"policy guard {key!r} must be a non-negative integer")
    return value


def check_range(name: str, count: int, minimum: int, maximum: int) -> None:
    if minimum > maximum:
        raise BuildError(f"invalid {name} guard: minimum {minimum} exceeds maximum {maximum}")
    if not minimum <= count <= maximum:
        raise BuildError(f"{name} count {count} is outside [{minimum}, {maximum}]")


def run_mihomo(mihomo: Path, source: Path, output_mrs: Path, roundtrip: Path) -> None:
    commands = (
        [str(mihomo), "convert-ruleset", "domain", "text", str(source), str(output_mrs)],
        [str(mihomo), "convert-ruleset", "domain", "mrs", str(output_mrs), str(roundtrip)],
    )
    for command in commands:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise BuildError(f"mihomo conversion failed ({completed.returncode}): {detail}")


def atomic_replace(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)


def temporary_path(parent: Path, suffix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="games-direct-build-",
        suffix=suffix,
        dir=parent,
        delete=False,
    ) as stream:
        return Path(stream.name)


def build(
    *,
    policy_path: Path,
    source_dir: Path,
    source_ref: str,
    output_list: Path,
    report_path: Path,
    previous_list: Path | None = None,
    mihomo: Path | None = None,
    output_mrs: Path | None = None,
) -> dict[str, Any]:
    source_ref = validate_source_ref(source_ref)
    policy = read_json(policy_path)
    if policy.get("schema_version") != 1:
        raise BuildError("unsupported policy schema_version")

    base_file = policy.get("base_file")
    exclude_files = policy.get("exclude_files")
    guards = policy.get("guards")
    if not isinstance(base_file, str) or not base_file or Path(base_file).name != base_file:
        raise BuildError("policy base_file must be one plain file name")
    if (
        not isinstance(exclude_files, list)
        or not exclude_files
        or any(not isinstance(item, str) or not item or Path(item).name != item for item in exclude_files)
        or len(exclude_files) != len(set(exclude_files))
    ):
        raise BuildError("policy exclude_files must be unique plain file names")
    if not isinstance(guards, dict):
        raise BuildError("policy guards must be an object")

    base_rules, base_hash = load_rules_with_hash(source_dir / base_file)
    base_set = set(base_rules)
    exclusion_sets: dict[str, set[str]] = {}
    source_hashes: dict[str, str] = {base_file: base_hash}
    excluded_union: set[str] = set()
    for file_name in exclude_files:
        file_rules, file_hash = load_rules_with_hash(source_dir / file_name)
        source_hashes[file_name] = file_hash
        rules = set(file_rules)
        missing = rules - base_set
        if missing:
            sample = ", ".join(sorted(missing)[:5])
            raise BuildError(f"{file_name} is not a subset of {base_file}; missing: {sample}")
        overlap = rules & excluded_union
        if overlap:
            sample = ", ".join(sorted(overlap)[:5])
            raise BuildError(f"exclusion files overlap; {file_name}: {sample}")
        exclusion_sets[file_name] = rules
        excluded_union.update(rules)

    output_rules = [rule for rule in base_rules if rule not in excluded_union]
    check_range(
        "base",
        len(base_rules),
        require_integer(guards, "base_min"),
        require_integer(guards, "base_max"),
    )
    check_range(
        "output",
        len(output_rules),
        require_integer(guards, "output_min"),
        require_integer(guards, "output_max"),
    )
    if len(output_rules) != len(base_rules) - len(excluded_union):
        raise BuildError("subtraction invariant failed")

    added: set[str] = set()
    removed: set[str] = set()
    if previous_list is not None:
        previous = set(load_rules(previous_list))
        current = set(output_rules)
        added = current - previous
        removed = previous - current
        if len(added) > require_integer(guards, "max_added"):
            raise BuildError(f"added delta {len(added)} exceeds policy maximum")
        if len(removed) > require_integer(guards, "max_removed"):
            raise BuildError(f"removed delta {len(removed)} exceeds policy maximum")

    list_bytes = ("\n".join(output_rules) + "\n").encode("utf-8")
    output_list.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    if (mihomo is None) != (output_mrs is None):
        raise BuildError("--mihomo and --output-mrs must be supplied together")
    if mihomo is not None and not mihomo.is_file():
        raise BuildError(f"mihomo executable not found: {mihomo}")

    temporary_files: list[Path] = []
    try:
        temp_list = temporary_path(output_list.parent, ".list")
        temporary_files.append(temp_list)
        temp_list.write_bytes(list_bytes)
        mrs_hash: str | None = None
        roundtrip_order_preserved: bool | None = None
        temp_mrs: Path | None = None
        if mihomo is not None and output_mrs is not None:
            temp_mrs = temporary_path(output_mrs.parent, ".mrs")
            roundtrip = temporary_path(output_list.parent, ".roundtrip.list")
            temporary_files.extend((temp_mrs, roundtrip))
            run_mihomo(mihomo, temp_list, temp_mrs, roundtrip)
            roundtrip_rules = load_rules(roundtrip)
            if len(roundtrip_rules) != len(output_rules) or set(roundtrip_rules) != set(output_rules):
                raise BuildError("MRS round-trip changes the canonical rule set")
            roundtrip_order_preserved = roundtrip_rules == output_rules
            mrs_hash = sha256(temp_mrs.read_bytes())

        report: dict[str, Any] = {
            "schema_version": 1,
            "source_repository": policy.get("source_repository"),
            "source_ref": source_ref,
            "source_sha256": source_hashes,
            "base_file": base_file,
            "base_count": len(base_rules),
            "exclude_files": {name: len(values) for name, values in exclusion_sets.items()},
            "excluded_count": len(excluded_union),
            "output_count": len(output_rules),
            "added_count": len(added),
            "removed_count": len(removed),
            "output_sha256": sha256(list_bytes),
            "mrs_sha256": mrs_hash,
            "mrs_roundtrip_order_preserved": roundtrip_order_preserved,
        }
        temp_report = temporary_path(report_path.parent, ".report.json")
        temporary_files.append(temp_report)
        temp_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        atomic_replace(temp_list, output_list)
        if temp_mrs is not None and output_mrs is not None:
            atomic_replace(temp_mrs, output_mrs)
        atomic_replace(temp_report, report_path)
    finally:
        for path in temporary_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output-list", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--previous-list", type=Path)
    parser.add_argument("--mihomo", type=Path)
    parser.add_argument("--output-mrs", type=Path)
    args = parser.parse_args()
    try:
        report = build(
            policy_path=args.policy,
            source_dir=args.source_dir,
            source_ref=args.source_ref,
            output_list=args.output_list,
            report_path=args.report,
            previous_list=args.previous_list,
            mihomo=args.mihomo,
            output_mrs=args.output_mrs,
        )
    except BuildError as error:
        parser.exit(2, f"error: {error}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
