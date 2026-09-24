"""Relay the AWS China CUR into a commercial-partition bucket, converted from CNY
to USD, keeping the Legacy CUR layout and manifests so CloudZero can process it
as native AWS billing data."""
import csv
import gzip
import io
import json
import os
import re
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import boto3

CN_REGION = os.environ["CN_REGION"].strip()          # cn-north-1 or cn-northwest-1
CN_CUR_BUCKET = os.environ["CN_CUR_BUCKET"].strip()
CN_CUR_PREFIX = os.environ["CN_CUR_PREFIX"].strip()  # report path prefix in China
CUR_REPORT = os.environ["CUR_REPORT"].strip()        # report name, kept the same
CN_SECRET_ID = os.environ["CN_SECRET_ID"].strip()
DEST_BUCKET = os.environ["DEST_BUCKET"].strip()
DEST_PREFIX = os.environ["DEST_PREFIX"].strip()      # report path prefix in the destination
FX_RATES_KEY = os.environ["FX_RATES_KEY"].strip()
AUDIT_PREFIX = os.environ.get("AUDIT_PREFIX", "cur-relay-audit").strip()
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "10").strip())

# A column is converted when its category holds money and its name ends in one
# of these words, e.g. lineItem/UnblendedCost, pricing/publicOnDemandRate,
# savingsPlan/TotalCommitmentToDate, reservation/UpfrontValue.
MONEY_CATEGORIES = {"lineItem", "pricing", "reservation", "savingsPlan", "discount"}
MONEY_SUFFIX = re.compile(r"(Cost|Rate|Fee|Commitment|Discount|UpfrontValue)$")
CURRENCY_COLUMNS = {"lineItem/CurrencyCode", "pricing/currency"}
ONE = Decimal(1)


def dec(value):
    try:
        return Decimal(value) if value else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


PLACES = Decimal("0.0000000001")  # CUR money columns carry at most 10 decimal places


def scale(value, factor):
    try:
        return format((Decimal(value) * factor).quantize(PLACES).normalize(), "f")
    except InvalidOperation:
        return value  # leave anything non-numeric untouched


def is_money(column):
    category, _, name = column.partition("/")
    return category in MONEY_CATEGORIES and bool(MONEY_SUFFIX.search(name))


def next_month(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def billing_period_id(start):
    return f"{start:%Y%m%d}-{next_month(start):%Y%m%d}"


def periods_to_process(event):
    if event and event.get("periods"):  # {"periods": ["2026-08"]} restates a month
        return [date.fromisoformat(p + "-01") for p in event["periods"]]
    today = datetime.now(timezone.utc).date()
    current = today.replace(day=1)
    periods = [current]
    if today.day <= LOOKBACK_DAYS:  # AWS finalizes last month early in this one
        periods.insert(0, (current - timedelta(days=1)).replace(day=1))
    return periods


def china_s3():
    secret = json.loads(
        boto3.client("secretsmanager").get_secret_value(SecretId=CN_SECRET_ID)["SecretString"]
    )
    # A cn-* region makes boto3 use the amazonaws.com.cn endpoints
    return boto3.client(
        "s3",
        region_name=CN_REGION,
        aws_access_key_id=secret["access_key_id"],
        aws_secret_access_key=secret["secret_access_key"],
    )


def read_json(s3, bucket, key):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def relay_file(cn, dest, src_key, dst_key, rate, totals):
    """Stream one CUR file, convert money columns to USD, upload it to the destination."""
    fd, path = tempfile.mkstemp(suffix=".csv.gz")
    os.close(fd)
    body = cn.get_object(Bucket=CN_CUR_BUCKET, Key=src_key)["Body"]
    with gzip.GzipFile(fileobj=body) as gin, \
            gzip.open(path, "wt", encoding="utf-8", newline="") as out:
        reader = csv.reader(io.TextIOWrapper(gin, encoding="utf-8", newline=""))
        writer = csv.writer(out, lineterminator="\n")
        header = next(reader)
        writer.writerow(header)
        money = [i for i, col in enumerate(header) if is_money(col)]
        currency = [i for i, col in enumerate(header) if col in CURRENCY_COLUMNS]
        code_i = header.index("lineItem/CurrencyCode")
        cost_i = header.index("lineItem/UnblendedCost")
        for row in reader:
            code = (row[code_i] or "CNY").upper()
            if code == "CNY":
                factor = rate
                totals["unblended_cny"] += dec(row[cost_i])
            elif code == "USD":
                factor = ONE
            else:
                raise ValueError(f"Unexpected currency {code} in {src_key}")
            for i in money:
                if row[i]:
                    row[i] = scale(row[i], factor)
            for i in currency:
                if row[i]:
                    row[i] = "USD"
            totals["unblended_usd"] += dec(row[cost_i])
            totals["rows"] += 1
            writer.writerow(row)
    dest.upload_file(path, DEST_BUCKET, dst_key)
    os.remove(path)
    return [header[i] for i in money]


def process_period(cn, dest, start, rates):
    period = billing_period_id(start)
    month = f"{start:%Y-%m}"
    if month not in rates:
        raise RuntimeError(f"No CNY to USD rate for {month} in {FX_RATES_KEY}")
    rate = Decimal(str(rates[month]))

    manifest_name = f"{CUR_REPORT}-Manifest.json"
    src_dir = f"{CN_CUR_PREFIX}/{CUR_REPORT}/{period}"
    dst_dir = f"{DEST_PREFIX}/{CUR_REPORT}/{period}"

    source = read_json(cn, CN_CUR_BUCKET, f"{src_dir}/{manifest_name}")
    if source is None:
        print(f"{period}: no China CUR yet, skipping")
        return

    # A new assembly ID whenever AWS publishes a new report version or the rate
    # changes, so CloudZero sees a new version to process. Otherwise, skip.
    assembly = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source['assemblyId']}|{rate}"))
    current = read_json(dest, DEST_BUCKET, f"{dst_dir}/{manifest_name}")
    if current and current.get("assemblyId") == assembly:
        print(f"{period}: already up to date")
        return

    totals = {"rows": 0, "unblended_cny": Decimal(0), "unblended_usd": Decimal(0)}
    dest_keys, converted = [], set()
    for src_key in source["reportKeys"]:
        dst_key = f"{dst_dir}/{assembly}/{src_key.rsplit('/', 1)[-1]}"
        converted.update(relay_file(cn, dest, src_key, dst_key, rate, totals))
        dest_keys.append(dst_key)

    # Same manifest AWS wrote, pointed at the new bucket, assembly and files
    manifest = {k: v for k, v in source.items() if k != "additionalArtifactKeys"}
    manifest.update({"assemblyId": assembly, "bucket": DEST_BUCKET, "reportKeys": dest_keys})
    body = json.dumps(manifest, indent=2)
    dest.put_object(Bucket=DEST_BUCKET, Key=f"{dst_dir}/{assembly}/{manifest_name}",
                    Body=body, ContentType="application/json")

    dest.put_object(
        Bucket=DEST_BUCKET,
        Key=f"{AUDIT_PREFIX}/{period}/{assembly}.json",
        Body=json.dumps({
            "period": period,
            "source_assembly_id": source["assemblyId"],
            "assembly_id": assembly,
            "rate_usd_per_cny": str(rate),
            "rows": totals["rows"],
            "unblended_cny": format(totals["unblended_cny"].quantize(Decimal("0.01")), "f"),
            "unblended_usd": format(totals["unblended_usd"].quantize(Decimal("0.01")), "f"),
            "converted_columns": sorted(converted),
        }, indent=2),
        ContentType="application/json",
    )

    # Written last: the top-level manifest is what points readers at the new version
    dest.put_object(Bucket=DEST_BUCKET, Key=f"{dst_dir}/{manifest_name}",
                    Body=body, ContentType="application/json")
    print(f"{period}: {totals['rows']} rows, CNY {totals['unblended_cny']} "
          f"-> USD {totals['unblended_usd']} at {rate}")


def lambda_handler(event=None, context=None):
    cn = china_s3()
    dest = boto3.client("s3")
    rates = read_json(dest, DEST_BUCKET, FX_RATES_KEY)
    if rates is None:
        raise RuntimeError(f"Rates file {FX_RATES_KEY} not found in {DEST_BUCKET}")
    for start in periods_to_process(event):
        process_period(cn, dest, start, rates)


handler = lambda_handler  # alias, if you save the file as relay.py and use relay.handler
