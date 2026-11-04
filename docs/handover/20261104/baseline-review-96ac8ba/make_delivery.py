"""Package explicit read-only audit artifacts; never include runtime databases."""
import collections
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

OUT = Path(__file__).resolve().parent
ROOT = OUT.parent.parent
A = ROOT / "Disaster_Rescue_DRRM"
B = ROOT / "Disaster_Rescue_DRRM_B"


def write(name, text):
    target = OUT / name
    if target.exists():
        raise RuntimeError(f"Refuse overwrite: {name}")
    target.write_text(text, encoding="utf-8")


report = OUT / "阶段核查报告.md"
inventory = json.loads((OUT / "A-file-manifest.json").read_text(encoding="utf-8"))
text = report.read_text(encoding="utf-8")
assert text.count("<!-- FILE_INVENTORY -->") == 1
original = OUT / "报告初稿备份.md"
assert not original.exists()
original.write_bytes(report.read_bytes())
text = text.replace("<!-- FILE_INVENTORY -->", "```text\n" + "\n".join(row["path"] for row in inventory) + "\n```")
report.write_text(text, encoding="utf-8")

summaries = {}
for name in ("A-tests.xml", "B-s4-auth-media-tests.xml", "B-c4-s4-tests.xml"):
    root = ET.parse(OUT / name).getroot()
    cases = list(root.iter("testcase"))
    failures = [c.get("name") for c in cases if c.find("failure") is not None]
    errors = [c.get("name") for c in cases if c.find("error") is not None]
    skipped = [c.get("name") for c in cases if c.find("skipped") is not None]
    summaries[name] = {"tests": len(cases), "failures": failures, "errors": errors, "skipped": skipped,
                       "passed": len(cases) - len(failures) - len(errors) - len(skipped),
                       "by_class": dict(collections.Counter(c.get("classname", "") for c in cases))}
assert summaries["A-tests.xml"]["tests"] == 99
assert summaries["B-s4-auth-media-tests.xml"]["passed"] == 79
assert len(summaries["B-c4-s4-tests.xml"]["failures"]) == 2
write("test-summary.json", json.dumps(summaries, ensure_ascii=False, indent=2))

selections = {
    "A": {"miniprogram/app.json": [(1, 25)], "miniprogram/config/env.js": [(1, 18)],
          "miniprogram/services/index.js": [(1, 32)], "miniprogram/utils/request.js": [(21, 82)],
          "miniprogram/utils/dto-schema.js": [(1, 71)]},
    "B": {"server/s4.py": [(81, 98), (220, 246), (278, 309), (320, 345)],
          "server/auth.py": [(95, 126), (131, 165), (173, 220)],
          "server/wechat.py": [(60, 116)], "server/media.py": [(52, 62), (100, 108), (141, 145)],
          "server/api.py": [(576, 604)], "src/sync/worker.py": [(18, 20), (49, 78), (134, 166)],
          "server/c4.py": [(34, 43), (139, 168), (172, 218)],
          "src/sensor/alarm.py": [(73, 95), (149, 169)],
          "src/database/db.py": [(299, 312), (1246, 1292), (2171, 2183), (2214, 2234)],
          "src/database/canonical.py": [(38, 52)], "src/tools/database_tool.py": [(90, 115)],
          "tests/test_c4_s4_flow.py": [(34, 66), (178, 201)]},
}
chunks = ["Line-numbered source evidence. A=ef59ee4; B=96ac8ba. No repository files changed.\n"]
for label, files in selections.items():
    repo = A if label == "A" else B
    for relative, ranges in files.items():
        lines = (repo / relative).read_text(encoding="utf-8").splitlines()
        for start, end in ranges:
            chunks.append(f"\n[{label}] {relative}:{start}-{end}\n")
            chunks.extend(f"{i}: {lines[i-1]}\n" for i in range(start, min(end, len(lines)) + 1))
write("source-excerpts.txt", "".join(chunks))

# Identify exactly this run by expected cases plus recent creation, relative to its JUnit timestamp.
suite = next(iter(ET.parse(OUT / "B-c4-s4-tests.xml").getroot().iter("testsuite")))
from datetime import datetime
started = datetime.fromisoformat(suite.get("timestamp")).timestamp()
records = []
for folder in OUT.parent.glob("c4_s4_ASGI_TEST_*"):
    path = folder / "evidence.json"
    if path.is_file() and started - 2 <= path.stat().st_mtime <= started + 45:
        value = json.loads(path.read_text(encoding="utf-8"))
        records.append({"source_directory": folder.name, "evidence": value})
assert len(records) == 8, f"Expected eight current-run case artifacts; found {len(records)}"
assert len({r["evidence"]["case"] for r in records}) == 8
write("c4-s4-request-evidence.json", json.dumps(records, ensure_ascii=False, indent=2))

allow = [
    "阶段核查报告.md", "test-commands.md", "test-summary.json", "source-excerpts.txt",
    "A-file-manifest.json", "A-file-list.txt", "A-s4_contract.json", "B-s4_contract.json", "A-to-B-contract.diff",
    "A-status.txt", "A-working-diff.txt", "A-staged-diff.txt", "A-working-stat.txt", "A-staged-stat.txt",
    "B-status.txt", "B-working-diff.txt", "B-staged-diff.txt", "B-working-stat.txt", "B-staged-stat.txt",
    "A-node-version.txt", "A-tests.tap", "A-tests.xml", "A-tests.exit-code.txt",
    "B-s4-auth-media-tests.log", "B-s4-auth-media-tests.xml", "B-s4-auth-media-tests.exit-code.txt",
    "B-c4-s4-tests.log", "B-c4-s4-tests.xml", "B-c4-s4-tests.exit-code.txt", "c4-s4-request-evidence.json",
    "auth-boundary-result.json", "auth-boundary-probe.log", "auth-boundary.exit-code.txt",
    "github-remote-heads.txt", "B-reference-files.json", "B-existing-96ac8ba-files.txt", "B-existing-96ac8ba-stat.txt",
    "repositories-before.json", "repositories-after.json", "repository-preservation.json",
    "collect_evidence.py", "probe_auth_boundary.py", "make_delivery.py",
]
preserved = json.loads((OUT / "repository-preservation.json").read_text(encoding="utf-8"))
assert preserved == {"A": True, "B": True}
for name in allow:
    assert (OUT / name).is_file(), name
    assert (OUT / name).suffix.lower() not in {".db", ".sqlite", ".mp4", ".jpg", ".pem", ".key"}
    assert name != ".env" and "/" not in name

# Bound the security check honestly: artifact texts, not a whole repository credential audit.
patterns = {"private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            "github_pat": r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
            "aws_access_key": r"\bAKIA[A-Z0-9]{16}\b"}
hits = []
for name in allow:
    # Exclude this scanner's own pattern literals from value matching.
    if name == "make_delivery.py":
        continue
    content = (OUT / name).read_text(encoding="utf-8-sig", errors="replace")
    for kind, pattern in patterns.items():
        if re.search(pattern, content):
            hits.append({"file": name, "kind": kind})
assert not hits, hits
write("delivery-scope.json", json.dumps({
    "included_files": allow, "known_secret_pattern_hits": hits,
    "scan_scope": "limited artifact pattern checks; not exhaustive source/credential audit",
    "excluded": ["temporary/", "raw SQLite databases", "previous runtime folders", ".env", "real credentials", "models", "photos/videos", "unrelated files", "local miniprogram/.gitignore"],
    "token_evidence": "random auth probe tokens only in memory; no headers/token values included in result",
}, ensure_ascii=False, indent=2))
allow.append("delivery-scope.json")
checksums = {name: hashlib.sha256((OUT / name).read_bytes()).hexdigest() for name in allow}
write("SHA256SUMS.json", json.dumps(checksums, ensure_ascii=False, indent=2))
allow.append("SHA256SUMS.json")
package = OUT.parent / ("AB阶段核查证据_20261103_" + OUT.name.rsplit("_", 1)[1] + ".zip")
assert not package.exists()
with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name in allow:
        archive.write(OUT / name, arcname=name)
with zipfile.ZipFile(package) as archive:
    assert archive.testzip() is None
    assert set(archive.namelist()) == set(allow)
    for name, expected in checksums.items():
        assert hashlib.sha256(archive.read(name)).hexdigest() == expected
print(json.dumps({"report": str(report), "package": str(package), "files": len(allow),
                  "bytes": package.stat().st_size, "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                  "repo_preserved": preserved}, ensure_ascii=False))
