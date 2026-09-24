"""End-to-end check with no AWS access: build a small commercial CUR, turn it
into a fake China CUR, run the relay against a local stand-in for S3, and
confirm the USD output matches the original to the cent.

Run from the repository root:  python3 -m unittest discover tests
"""
import csv
import gzip
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import date
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parents[1]

HEADER = [
    "identity/LineItemId", "bill/InvoicingEntity", "bill/BillingEntity", "bill/PayerAccountId",
    "bill/BillingPeriodStartDate", "bill/BillingPeriodEndDate", "lineItem/UsageAccountId",
    "lineItem/LineItemType", "lineItem/UsageStartDate", "lineItem/ProductCode", "lineItem/UsageType",
    "lineItem/AvailabilityZone", "lineItem/ResourceId", "lineItem/UsageAmount", "lineItem/CurrencyCode",
    "lineItem/UnblendedRate", "lineItem/UnblendedCost", "lineItem/LineItemDescription",
    "lineItem/LegalEntity", "product/location", "product/region", "pricing/currency",
    "pricing/publicOnDemandCost", "pricing/RateCode", "resourceTags/user:team",
]
ROWS = [
    ["1", "Amazon Web Services, Inc.", "AWS", "111122223333", "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
     "444455556666", "Usage", "2026-08-01T00:00:00Z", "AmazonEC2", "BoxUsage:m5.large", "us-east-1a",
     "i-0abc", "1", "USD", "0.096", "0.096", "$0.096 per On Demand Linux m5.large Instance Hour",
     "Amazon Web Services, Inc.", "US East (N. Virginia)", "us-east-1", "USD", "0.096", "A.1", "core"],
    ["2", "Amazon Web Services, Inc.", "AWS", "111122223333", "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
     "444455556666", "Usage", "2026-08-01T00:00:00Z", "AmazonS3", "USW2-TimedStorage-ByteHrs", "",
     "arn:aws:s3:::example-bucket", "10.5", "USD", "0.023", "0.2415", "$0.023 per GB-month",
     "Amazon Web Services, Inc.", "US West (Oregon)", "us-west-2", "USD", "0.2415", "B.2", "data"],
    ["3", "Amazon Web Services, Inc.", "AWS", "111122223333", "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
     "444455556666", "Tax", "2026-08-01T00:00:00Z", "AmazonEC2", "", "", "", "1", "USD", "", "5.00",
     "Tax for product code AmazonEC2", "Amazon Web Services, Inc.", "", "", "", "", "", ""],
]
ORIGINAL_TOTAL = Decimal("5.3375")


class LocalS3:
    """Just enough of the boto3 S3 client, backed by local folders."""

    class exceptions:  # noqa: N801 - mirrors boto3's attribute name
        class NoSuchKey(Exception):
            pass

    def __init__(self, roots):
        self.roots = roots

    def _path(self, bucket, key):
        return pathlib.Path(self.roots[bucket]) / key

    def get_object(self, Bucket, Key):
        p = self._path(Bucket, Key)
        if not p.exists():
            raise self.exceptions.NoSuchKey()
        return {"Body": io.BytesIO(p.read_bytes())}

    def put_object(self, Bucket, Key, Body, **_):
        p = self._path(Bucket, Key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(Body.encode() if isinstance(Body, str) else Body)

    def upload_file(self, filename, Bucket, Key):
        self.put_object(Bucket, Key, pathlib.Path(filename).read_bytes())


class RoundTrip(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            src = tmp / "export.csv.gz"
            with gzip.open(src, "wt", newline="") as f:
                w = csv.writer(f, lineterminator="\n")
                w.writerow(HEADER)
                w.writerows(ROWS)

            china = tmp / "china"
            subprocess.run([sys.executable, str(ROOT / "tools" / "make_fake_china_cur.py"), str(src),
                            "--out", str(china), "--prefix", "cur", "--report", "china-hourly",
                            "--bucket", "source", "--remap-accounts"], check=True, capture_output=True)

            os.environ.update(CN_REGION="us-west-1", CN_CUR_BUCKET="source", CN_CUR_PREFIX="cur",
                              CUR_REPORT="china-hourly", CN_SECRET_ID="unused", DEST_BUCKET="dest",
                              DEST_PREFIX="china-cur", FX_RATES_KEY="fx/cny_usd_monthly.json")
            sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
            sys.path.insert(0, str(ROOT / "relay"))
            import lambda_function as relay

            s3 = LocalS3({"source": china, "dest": tmp / "dest"})
            rates = {"2026-08": "0.13986014"}
            relay.process_period(s3, s3, date(2026, 8, 1), rates)

            audit = json.loads(next((tmp / "dest" / "cur-relay-audit").rglob("*.json")).read_text())
            self.assertEqual(audit["rows"], len(ROWS))
            self.assertEqual(Decimal(audit["unblended_usd"]), ORIGINAL_TOTAL.quantize(Decimal("0.01")))

            out = next((tmp / "dest" / "china-cur").rglob("*.csv.gz"))
            rows = list(csv.DictReader(gzip.open(out, "rt", newline="")))
            self.assertEqual({r["lineItem/CurrencyCode"] for r in rows}, {"USD"})
            self.assertTrue(all(r["lineItem/UsageType"] == "" or r["lineItem/UsageType"][:4] in ("CNW1", "CNN1")
                                for r in rows))

            before = sorted(p.name for p in (tmp / "dest").rglob("*"))
            relay.process_period(s3, s3, date(2026, 8, 1), rates)  # unchanged: should skip
            self.assertEqual(before, sorted(p.name for p in (tmp / "dest").rglob("*")))


    def test_sample_dataset(self):
        """The committed sample China CUR converts to the totals the test guide documents."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            os.environ.update(CN_REGION="us-west-1", CN_CUR_BUCKET="source", CN_CUR_PREFIX="cur",
                              CUR_REPORT="china-hourly", CN_SECRET_ID="unused", DEST_BUCKET="dest",
                              DEST_PREFIX="china-cur", FX_RATES_KEY="fx/cny_usd_monthly.json")
            sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
            sys.path.insert(0, str(ROOT / "relay"))
            import lambda_function as relay

            s3 = LocalS3({"source": ROOT / "test-data" / "fake-china-cur", "dest": tmp / "dest"})
            relay.process_period(s3, s3, date(2026, 8, 1), {"2026-08": "0.13986014"})
            audit = json.loads(next((tmp / "dest" / "cur-relay-audit").rglob("*.json")).read_text())
            self.assertEqual(audit["rows"], 7580)
            self.assertEqual(audit["unblended_cny"], "10717.04")
            self.assertEqual(audit["unblended_usd"], "1498.89")


if __name__ == "__main__":
    unittest.main()
