"""Invariant tests for the Franklin County Recorder doc-type mapping.

The recorder is the source for LIS PENDENS (foreclosure) and LIENS only.
Probate is sourced exclusively from Probate Court NetData. These tests lock
that contract so a future edit can't silently re-route Certificate of Transfer
/ Trust (or anything else) back into the probate stream.

Run: python tests/test_recorder_mapping.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from oh_franklin_recorder import DOC_TYPE_TO_NOTICE_TYPE, DEFAULT_DOC_CODES

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  ok: {msg}")


print("Recorder never emits probate:")
# No doc type may map to probate — that's NetData's job.
probate_keys = [k for k, v in DOC_TYPE_TO_NOTICE_TYPE.items() if v == "probate"]
check(not probate_keys, f"no doc type maps to 'probate' (found {probate_keys})")

# The specific offenders must not return.
for offender in ("CERTIFICATE OF TRANSFER", "TRUST"):
    check(offender not in DOC_TYPE_TO_NOTICE_TYPE,
          f"{offender!r} not in DOC_TYPE_TO_NOTICE_TYPE")

# And we must not even request their doc codes.
for code in ("CT", "TR"):
    check(code not in DEFAULT_DOC_CODES,
          f"doc code {code!r} not requested in DEFAULT_DOC_CODES")

print("Recorder only produces foreclosure/lien notice types:")
allowed = {"foreclosure", "lien"}
bad = {k: v for k, v in DOC_TYPE_TO_NOTICE_TYPE.items() if v not in allowed}
check(not bad, f"all mappings are foreclosure/lien (offenders: {bad})")

print("Federal liens route to the Liens list (not Tax Delinquent):")
for ft in ("FEDERAL TAX LIEN", "FEDERAL LIEN"):
    check(DOC_TYPE_TO_NOTICE_TYPE.get(ft) == "lien",
          f"{ft!r} maps to 'lien'")

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nAll recorder mapping invariants hold.")
