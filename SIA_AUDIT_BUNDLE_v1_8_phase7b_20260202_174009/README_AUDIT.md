# SIA Audit Bundle (Phase 7B)

This bundle is an offline-verifiable audit artifact.

Contents:
- CERTS.jsonl: sealed certificate chain (Phase 6C + Phase 7A)
- VERIFY.py: offline verifier (recomputes certificate_id and verifies chain + finality)
- MANIFEST.sha256: file hashes for integrity (self-excluding)
- RULESET.txt: pinned ruleset identifier

How to verify:
  python VERIFY.py --bundle_dir .

Expected:
  VERIFY: PASS
