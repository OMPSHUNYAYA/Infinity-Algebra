#!/usr/bin/env python3
# Phase 7B-A — External Audit Bundle + Offline Verifier SmokeTest (SIA)
# Standard library only. Deterministic. No new algebra.

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

CANONICAL_ADVISE = (
    "use classical analysis (limits/asymptotics/numerical methods) with explicit acknowledgement of approximation"
)

DEFAULT_RULESET_ID = "SIA_CORE_RULESET_v1"
GENESIS_FALLBACK = "GENESIS"


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_jsonl(path: str):
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                raise ValueError(f"JSONL parse error at line {ln}: {e}")
            records.append(rec)
    if not records:
        raise ValueError("JSONL is empty.")
    return records


def write_text(path: str, s: str):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_manifest(bundle_dir: str, rel_files):
    """
    Build MANIFEST.sha256 as:
      <sha256>  <relpath>
    Deterministic ordering.
    IMPORTANT: self-excluding (do not include MANIFEST.sha256).
    """
    rel_files = sorted(rel_files)
    lines = []
    for rel in rel_files:
        abspath = os.path.join(bundle_dir, rel)
        digest = file_sha256(abspath)
        lines.append(f"{digest}  {rel}")
    manifest_path = os.path.join(bundle_dir, "MANIFEST.sha256")
    write_text(manifest_path, "\n".join(lines) + "\n")
    return manifest_path


def verifier_py_source():
    return r'''#!/usr/bin/env python3
# SIA Offline Verifier — Phase 7B (Standard library only)

import argparse
import hashlib
import json
import os
import sys

CANONICAL_ADVISE = "use classical analysis (limits/asymptotics/numerical methods) with explicit acknowledgement of approximation"
GENESIS_FALLBACK = "GENESIS"

def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def load_manifest(manifest_path: str):
    items = []
    with open(manifest_path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if "  " not in line:
                raise ValueError(f"Manifest line {ln} malformed (missing double-space separator).")
            digest, rel = line.split("  ", 1)
            digest = digest.strip()
            rel = rel.strip()
            if len(digest) != 64:
                raise ValueError(f"Manifest line {ln} has invalid sha256 length.")
            items.append((digest, rel))
    if not items:
        raise ValueError("Manifest is empty.")

    # Guard: manifest must never include itself
    for _, rel in items:
        if rel.replace("\\", "/") == "MANIFEST.sha256":
            raise ValueError("Manifest must not include itself: MANIFEST.sha256")

    return items

def verify_manifest(bundle_dir: str, manifest_path: str):
    items = load_manifest(manifest_path)
    for digest, rel in items:
        abspath = os.path.join(bundle_dir, rel)
        if not os.path.isfile(abspath):
            raise ValueError(f"Missing file listed in manifest: {rel}")
        got = file_sha256(abspath)
        if got != digest:
            raise ValueError(f"Hash mismatch for {rel}: expected {digest}, got {got}")
    return True

def read_jsonl(path: str):
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                raise ValueError(f"JSONL parse error at line {ln}: {e}")
            records.append(rec)
    if not records:
        raise ValueError("JSONL is empty.")
    return records

def canonical_json(obj: dict) -> str:
    # Match Phase 7A: ensure_ascii=True, separators=(",",":")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

def canonical_body_for_cert(rec: dict) -> str:
    """
    Phase 7A hashing surface (required by observed 7A artifacts):

    - Always exclude derived fields:
        certificate_id, chain_hash

    - Seal record (op == "seal"):
        certificate_id computed before 'seal_id' and 'label' are attached.
        => exclude 'seal_id' and 'label' ONLY for op=="seal"

    - Non-seal records:
        label is present at hashing time.
        => KEEP 'label'
    """
    body = dict(rec)
    body.pop("certificate_id", None)
    body.pop("chain_hash", None)

    if body.get("op") == "seal":
        body.pop("seal_id", None)
        body.pop("label", None)

    return canonical_json(body)

def recompute_certificate_id(rec: dict) -> str:
    return sha256_hex(canonical_body_for_cert(rec).encode("utf-8"))

def recompute_chain_hash(prev_chain_hash: str, certificate_id: str) -> str:
    return sha256_hex((prev_chain_hash + "|" + certificate_id).encode("utf-8"))

def require_keys(rec: dict, keys):
    for k in keys:
        if k not in rec:
            raise ValueError(f"Missing required key: {k}")

def reason_text(rec: dict) -> str:
    r = rec.get("reason", "")
    if r is None:
        return ""
    return str(r)

def is_reseal_refusal(rec: dict) -> bool:
    # Post-seal reseal attempts are expected to ABSTAIN.
    if rec.get("decision") != "ABSTAIN":
        return False
    # Accept several reason styles seen in practice:
    # - FINALITY_VIOLATION...
    # - already sealed / ALREADY_SEALED / SEALED / FINALITY / etc.
    rt = reason_text(rec).strip().upper()
    if rt.startswith("FINALITY_VIOLATION"):
        return True
    if "ALREADY" in rt and "SEAL" in rt:
        return True
    if rt.startswith("ALREADY_SEALED") or rt.startswith("SEALED") or "SEALED" in rt:
        return True
    if "FINALITY" in rt and "VIOL" in rt:
        return True
    # Also accept explicit finality flag
    fin = rec.get("finality")
    if isinstance(fin, dict) and fin.get("sealed") is True:
        return True
    return False

def finality_seal_id(rec: dict):
    fin = rec.get("finality")
    if isinstance(fin, dict) and fin.get("seal_id"):
        return fin.get("seal_id")
    if rec.get("seal_id"):
        return rec.get("seal_id")
    return None

def verify_records(records):
    seal_index = None
    seal_id = None
    sealed_chain_hash = None

    prev_chain_hash = None

    for i, rec in enumerate(records):
        require_keys(rec, ["mode", "phase", "label", "op", "decision", "reason", "certificate_id", "chain_hash", "a_decimals"])

        # Advise discipline
        advise = rec.get("advise", None)
        decision = rec.get("decision")
        if decision == "ABSTAIN":
            if advise is not None and advise != CANONICAL_ADVISE:
                raise ValueError(f"advise must be canonical or null (label={rec.get('label')})")
        else:
            if advise is not None:
                raise ValueError(f"advise must be null when decision != ABSTAIN (label={rec.get('label')})")

        # certificate_id must always recompute correctly
        cid_expected = recompute_certificate_id(rec)
        if cid_expected != rec["certificate_id"]:
            raise ValueError(f"certificate_id mismatch at record {i+1} (label={rec.get('label')})")

        # Determine prev_chain_hash for the FIRST record only
        if i == 0:
            prev_chain_hash = rec.get("prev_chain_hash") or rec.get("inputs", {}).get("seal_prev_chain_hash") or GENESIS_FALLBACK

        # chain_hash rule:
        # - before seal: advances as sha256(prev || "|" || certificate_id)
        # - at seal (first sealed=true record): advances normally
        # - after seal: frozen to sealed_chain_hash
        if sealed_chain_hash is None:
            ch_expected = recompute_chain_hash(prev_chain_hash, rec["certificate_id"])
            if ch_expected != rec["chain_hash"]:
                raise ValueError(f"chain_hash mismatch at record {i+1} (label={rec.get('label')})")
            prev_chain_hash = rec["chain_hash"]
        else:
            if rec.get("chain_hash") != sealed_chain_hash:
                raise ValueError(f"Post-seal chain_hash changed at record {i+1} (must remain stable after seal).")

        # Seal assertions handling
        if rec.get("op") == "seal" and rec.get("sealed") is True:
            if seal_index is None:
                # First seal assertion is the canonical seal event
                seal_index = i
                seal_id = rec.get("seal_id") or rec.get("certificate_id")
                sealed_chain_hash = rec.get("chain_hash")
            else:
                # Later seal assertions allowed ONLY as reseal refusals
                if not is_reseal_refusal(rec):
                    raise ValueError("Multiple seal assertions found (later sealed=true seal records must be reseal refusals).")
                sid = finality_seal_id(rec) or seal_id
                if sid != seal_id:
                    raise ValueError("Post-seal seal assertion does not bind to original seal_id.")
                if rec.get("chain_hash") != sealed_chain_hash:
                    raise ValueError("Post-seal seal assertion must preserve sealed chain_hash.")

    if seal_index is None:
        raise ValueError("No finality seal record found (op='seal' and sealed=true).")

    # Post-seal discipline checks (issue/ops are frozen, must ABSTAIN for proof_assistant_cert)
    for j in range(seal_index + 1, len(records)):
        rec = records[j]

        if rec.get("mode") == "proof_assistant_cert":
            if rec.get("decision") != "ABSTAIN":
                raise ValueError(f"Post-seal issuance must ABSTAIN (label={rec.get('label')})")
            # Be permissive here: some post-seal refusals may use "ALREADY_SEALED" style.
            rt = reason_text(rec).strip().upper()
            if not (rt.startswith("FINALITY_VIOLATION") or ("SEAL" in rt) or ("FINALITY" in rt)):
                raise ValueError(f"Post-seal issuance reason must indicate finality/seal (label={rec.get('label')})")
            fin = rec.get("finality")
            if isinstance(fin, dict):
                if fin.get("sealed") is not True:
                    raise ValueError(f"finality.sealed must be true post-seal (label={rec.get('label')})")
                # If finality.seal_id exists, it must match canonical seal id
                if fin.get("seal_id") and fin.get("seal_id") != seal_id:
                    raise ValueError(f"finality.seal_id mismatch (label={rec.get('label')})")

    return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle_dir", required=True, help="Bundle directory containing CERTS.jsonl and MANIFEST.sha256")
    ap.add_argument("--certs", default="CERTS.jsonl")
    ap.add_argument("--manifest", default="MANIFEST.sha256")
    args = ap.parse_args()

    bundle_dir = args.bundle_dir
    certs_path = os.path.join(bundle_dir, args.certs)
    manifest_path = os.path.join(bundle_dir, args.manifest)

    if not os.path.isfile(certs_path):
        print("VERIFY: FAIL (missing CERTS.jsonl)")
        return 2
    if not os.path.isfile(manifest_path):
        print("VERIFY: FAIL (missing MANIFEST.sha256)")
        return 2

    try:
        verify_manifest(bundle_dir, manifest_path)
        records = read_jsonl(certs_path)
        verify_records(records)
    except Exception as e:
        print(f"VERIFY: FAIL ({e})")
        return 2

    print("VERIFY: PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="7", help="Phase label (default: 7)")
    ap.add_argument("--verify", action="store_true", help="Run VERIFY.py after building bundle")
    ap.add_argument("--in_jsonl", default="sia_smoketest_out_v1_7_phase7a.jsonl", help="Input sealed JSONL (from Phase 7A)")
    ap.add_argument("--ruleset_id", default=DEFAULT_RULESET_ID, help="Ruleset id string to pin in bundle")
    ap.add_argument("--bundle_dir", default=None, help="Output bundle directory (default auto)")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite bundle_dir if exists")
    args = ap.parse_args()

    in_jsonl = args.in_jsonl
    if not os.path.isfile(in_jsonl):
        print(f"ERROR: input JSONL not found: {in_jsonl}")
        return 2

    _ = read_jsonl(in_jsonl)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_dir = args.bundle_dir or f"SIA_AUDIT_BUNDLE_v1_8_phase7b_{stamp}"

    if os.path.exists(bundle_dir):
        if not args.overwrite:
            print(f"ERROR: bundle_dir exists: {bundle_dir} (use --overwrite)")
            return 2
        shutil.rmtree(bundle_dir)

    os.makedirs(bundle_dir, exist_ok=True)

    certs_path = os.path.join(bundle_dir, "CERTS.jsonl")
    shutil.copyfile(in_jsonl, certs_path)

    ruleset_path = os.path.join(bundle_dir, "RULESET.txt")
    write_text(ruleset_path, args.ruleset_id.strip() + "\n")

    readme_path = os.path.join(bundle_dir, "README_AUDIT.md")
    readme = (
        "# SIA Audit Bundle (Phase 7B)\n\n"
        "This bundle is an offline-verifiable audit artifact.\n\n"
        "Contents:\n"
        "- CERTS.jsonl: sealed certificate chain (Phase 6C + Phase 7A)\n"
        "- VERIFY.py: offline verifier (recomputes certificate_id and verifies chain + finality)\n"
        "- MANIFEST.sha256: file hashes for integrity (self-excluding)\n"
        "- RULESET.txt: pinned ruleset identifier\n\n"
        "How to verify:\n"
        "  python VERIFY.py --bundle_dir .\n\n"
        "Expected:\n"
        "  VERIFY: PASS\n"
    )
    write_text(readme_path, readme)

    verify_py_path = os.path.join(bundle_dir, "VERIFY.py")
    write_text(verify_py_path, verifier_py_source())
    try:
        os.chmod(verify_py_path, 0o755)
    except Exception:
        pass

    rel_files = ["CERTS.jsonl", "VERIFY.py", "RULESET.txt", "README_AUDIT.md"]
    _ = build_manifest(bundle_dir, rel_files)

    print(f"P7B_A_1_BUNDLE_WRITTEN: {bundle_dir}")
    print("P7B_A_2_MANIFEST_WRITTEN: MANIFEST.sha256 (self-excluded by design)")
    print("P7B_A_3_VERIFIER_WRITTEN: VERIFY.py")

    if args.verify:
        cmd = [sys.executable, "VERIFY.py", "--bundle_dir", "."]
        p = subprocess.run(cmd, cwd=bundle_dir, capture_output=True, text=True)

        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()

        verify_out_path = os.path.join(bundle_dir, "VERIFY_OUT.txt")
        write_text(verify_out_path, (out + ("\n" + err if err else "")).strip() + "\n")

        if out:
            print(out)
        if err:
            print(err)

        if p.returncode != 0:
            print("VERIFY: FAIL")
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
