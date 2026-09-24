"""Turn a commercial AWS Legacy CUR into a fake AWS China CUR for testing.

Reads CUR CSV (.csv or .csv.gz) files from a commercial account and writes a
China-shaped Legacy CUR, with the folder layout and manifests AWS produces:

  <out>/<prefix>/<report>/<YYYYMMDD-YYYYMMDD>/<report>-Manifest.json
  <out>/<prefix>/<report>/<YYYYMMDD-YYYYMMDD>/<assemblyId>/<report>-0000N.csv.gz
  <out>/<prefix>/<report>/<YYYYMMDD-YYYYMMDD>/<assemblyId>/<report>-Manifest.json

What changes, and why it matches a real China CUR:
  - Currency: lineItem/CurrencyCode and pricing/currency become CNY, and every
    money column is multiplied by --usd-to-cny. China Regions bill in CNY.
  - Regions: commercial regions map to cn-northwest-1 (Ningxia) or cn-north-1
    (Beijing), including product/region, product/regionCode, availability zones
    and product/location ("China (Ningxia)", "China (Beijing)").
  - Usage types: region prefixes (USE1-, USW2-, or none for us-east-1) become
    CNW1- or CNN1-, the codes AWS uses for Ningxia and Beijing.
  - ARNs: arn:aws: becomes arn:aws-cn:, with the region swapped.
  - Billing entity: bill/BillingEntity becomes "Ningxia" or "Beijing", which is
    how China CURs have appeared when ingested by CloudZero.
  - Tax: lineItem/TaxType becomes VAT. China Regions charge 6% VAT, which is
    the ratio seen between China fee and tax lines.
  - Legal and invoicing entity: lineItem/LegalEntity and bill/InvoicingEntity
    become the regional operator,
    Ningxia Western Cloud Data Technology Co., Ltd. (NWCD) or
    Beijing Sinnet Technology Co., Ltd. (Sinnet).
  - Accounts (optional, --remap-accounts): account IDs are replaced with stable
    fake IDs so test data can't collide with real accounts.

  - Descriptions: prices in lineItem/LineItemDescription are converted to CNY
    and written as "CNY 0.4455 ...", the format real China CURs use (for
    example "CNY 0.4455 hourly fee per PostgreSQL, db.r7g.large instance").
    Region names are swapped too.

With --add-china-extras, the script also adds line items a small commercial
export usually lacks: an Enterprise Support fee per billing entity
(OCBPremiumSupport, as China bills show it), one RDS Reserved Instance (an
RIFee line plus hourly DiscountedUsage, at a real China RI rate), and 6% VAT
per billing entity, product and usage type in place of commercial sales tax.

Everything else is copied unchanged. China CURs use the same English column
names and values as commercial CURs; no language conversion is involved.

Every column in the source export is kept, so a full CUR in gives a full
China-shaped CUR out. Rows are streamed, so a full month of data is fine.

Usage:
  python3 make_fake_china_cur.py SOURCE_DIR_OR_FILES... --out ./fake-cn \
      --prefix cur --report china-hourly [--manifest SOURCE-Manifest.json] \
      [--usd-to-cny 7.15] [--beijing-regions us-west-2,eu-west-1] [--remap-accounts]
Then upload:  aws s3 sync ./fake-cn s3://YOUR-FAKE-CHINA-BUCKET/
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import pathlib
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

NINGXIA, BEIJING = "cn-northwest-1", "cn-north-1"
CN = {
    NINGXIA: {"code": "CNW1", "location": "China (Ningxia)", "entity": "Ningxia",
              "legal": "Ningxia Western Cloud Data Technology Co., Ltd."},
    BEIJING: {"code": "CNN1", "location": "China (Beijing)", "entity": "Beijing",
              "legal": "Beijing Sinnet Technology Co., Ltd."},
}
# Commercial usage-type region prefixes (us-east-1 often has no prefix)
REGION_CODES = {
    "APE1": "ap-east-1", "APN1": "ap-northeast-1", "APN2": "ap-northeast-2", "APN3": "ap-northeast-3",
    "APS1": "ap-southeast-1", "APS2": "ap-southeast-2", "APS3": "ap-south-1", "APS4": "ap-southeast-3",
    "APS5": "ap-south-2", "APS6": "ap-southeast-4", "CAN1": "ca-central-1", "CAN2": "ca-west-1",
    "AFS1": "af-south-1", "EUC1": "eu-central-1", "EUC2": "eu-central-2", "EUN1": "eu-north-1",
    "EU": "eu-west-1", "EUW1": "eu-west-1", "EUW2": "eu-west-2", "EUW3": "eu-west-3",
    "EUS1": "eu-south-1", "EUS2": "eu-south-2", "ILC1": "il-central-1", "MEC1": "me-central-1",
    "MES1": "me-south-1", "SAE1": "sa-east-1", "USE1": "us-east-1", "USE2": "us-east-2",
    "USW1": "us-west-1", "USW2": "us-west-2",
}
LOCATION_NAMES = re.compile(
    r"(?:US East|US West|EU|Europe|Asia Pacific|Canada|Canada West|South America|Middle East|Africa|Israel|Mexico)"
    r" \([^)]*\)"
)
LOCATION_TO_REGION = {
    "US East (N. Virginia)": "us-east-1", "US East (Northern Virginia)": "us-east-1", "US East (Ohio)": "us-east-2",
    "US West (N. California)": "us-west-1", "US West (Northern California)": "us-west-1", "US West (Oregon)": "us-west-2",
    "Canada (Central)": "ca-central-1", "Canada West (Calgary)": "ca-west-1", "South America (Sao Paulo)": "sa-east-1",
    "South America (São Paulo)": "sa-east-1", "EU (Ireland)": "eu-west-1", "Europe (Ireland)": "eu-west-1",
    "EU (London)": "eu-west-2", "Europe (London)": "eu-west-2", "EU (Paris)": "eu-west-3", "Europe (Paris)": "eu-west-3",
    "EU (Frankfurt)": "eu-central-1", "Europe (Frankfurt)": "eu-central-1", "EU (Stockholm)": "eu-north-1",
    "Europe (Stockholm)": "eu-north-1", "EU (Milan)": "eu-south-1", "Europe (Milan)": "eu-south-1",
    "EU (Spain)": "eu-south-2", "Europe (Spain)": "eu-south-2", "EU (Zurich)": "eu-central-2", "Europe (Zurich)": "eu-central-2",
    "Asia Pacific (Tokyo)": "ap-northeast-1", "Asia Pacific (Seoul)": "ap-northeast-2", "Asia Pacific (Osaka)": "ap-northeast-3",
    "Asia Pacific (Singapore)": "ap-southeast-1", "Asia Pacific (Sydney)": "ap-southeast-2", "Asia Pacific (Jakarta)": "ap-southeast-3",
    "Asia Pacific (Melbourne)": "ap-southeast-4", "Asia Pacific (Mumbai)": "ap-south-1", "Asia Pacific (Hyderabad)": "ap-south-2",
    "Asia Pacific (Hong Kong)": "ap-east-1", "Middle East (Bahrain)": "me-south-1", "Middle East (UAE)": "me-central-1",
    "Africa (Cape Town)": "af-south-1", "Israel (Tel Aviv)": "il-central-1",
}
CODE_IN_TEXT = re.compile(r"\b(" + "|".join(sorted((k for k in REGION_CODES if k != "EU"), key=len, reverse=True)) + r")-")
CLOUDFRONT_GEO = re.compile(r"^(US|CA|EU|JP|AP|IN|SA|AU|ZA|ME|KR|ID)-")
MONEY_CATEGORIES = {"lineItem", "pricing", "reservation", "savingsPlan", "discount"}
MONEY_SUFFIX = re.compile(r"(Cost|Rate|Fee|Commitment|Discount|UpfrontValue)$")
PRICE_IN_TEXT = re.compile(r"(?:USD\s?|\$)(\d+(?:\.\d+)?)")
COMMERCIAL_REGION = re.compile(r"\b(us|eu|ap|ca|sa|me|af|il|mx)-(east|west|north|south|central|northeast|southeast|northwest|southwest)-\d(?!\d)")
MAX_ROWS = 500_000


def dec(value):
    try:
        return Decimal(value) if value else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def is_money(col):
    cat, _, name = col.partition("/")
    return cat in MONEY_CATEGORIES and bool(MONEY_SUFFIX.search(name))


def scale(value, factor):
    try:
        return format(Decimal(value) * factor, "f")
    except InvalidOperation:
        return value


def fake_account(acct):
    if not re.fullmatch(r"\d{12}", acct or ""):
        return acct
    return str(int(hashlib.sha256(acct.encode()).hexdigest(), 16))[:12].rjust(12, "9")


OTHER = {NINGXIA: BEIJING, BEIJING: NINGXIA}


class Mapper:
    def __init__(self, beijing_regions, remap_accounts):
        self.beijing = set(beijing_regions)
        self.remap = remap_accounts

    def cn_region(self, region):
        return BEIJING if region in self.beijing else NINGXIA

    def swap_regions(self, text, target):
        return COMMERCIAL_REGION.sub(target, text)

    def usage_type(self, value, target, product_code=""):
        if not value:
            return value
        if product_code == "AmazonCloudFront":
            return CLOUDFRONT_GEO.sub("CN-", value)
        if COMMERCIAL_REGION.match(value):  # e.g. us-east-1-KMS-Requests
            return self.swap_regions(value, target or NINGXIA)
        parts = value.split("-")
        mapped, i = [], 0
        while i < len(parts) - 1 and parts[i] in REGION_CODES:
            region = self.cn_region(REGION_CODES[parts[i]])
            if mapped and region == mapped[-1]:  # two commercial regions collapsed into one
                region = OTHER[region]
            mapped.append(region)
            parts[i] = CN[region]["code"]
            i += 1
        out = "-".join(parts)
        if i == 0 and target:  # no prefix, which AWS uses for us-east-1
            out = f"{CN[target]['code']}-{value}"
        return out

    def text(self, value, target):
        value = PRICE_IN_TEXT.sub(lambda m: "CNY " + format((Decimal(m.group(1)) * self.factor).normalize(), "f"), value)
        seen = {}

        def loc(m):
            region = self.cn_region(LOCATION_TO_REGION.get(m.group(0), "")) if m.group(0) in LOCATION_TO_REGION else target
            if seen and region in seen.values() and m.group(0) not in seen:
                region = OTHER[region]  # "X data transfer to Y" stays cross-region
            seen.setdefault(m.group(0), region)
            return CN[seen[m.group(0)]]["location"]

        value = LOCATION_NAMES.sub(loc, value)
        value = CODE_IN_TEXT.sub(lambda m: CN[self.cn_region(REGION_CODES[m.group(1)])]["code"] + "-", value)
        return self.swap_regions(value, target)

    def row(self, header, row, factor, idx):
        self.factor = factor
        region = (row[idx["product/region"]] if "product/region" in idx else "") or \
                 (row[idx["product/regionCode"]] if "product/regionCode" in idx else "")
        target = self.cn_region(region) if region and region != "global" else None
        target_or_default = target or NINGXIA
        product_code = row[idx["lineItem/ProductCode"]] if "lineItem/ProductCode" in idx else ""
        from_cn, from_orig = None, None
        for i, col in enumerate(header):
            v = row[i]
            if not v:
                continue
            if is_money(col):
                row[i] = scale(v, factor)
            elif col in ("lineItem/CurrencyCode", "pricing/currency"):
                row[i] = "CNY"
            elif col in ("product/region", "product/regionCode", "product/fromRegionCode", "product/toRegionCode"):
                if COMMERCIAL_REGION.match(v):  # includes Local Zones such as ap-northeast-1-tpe-1
                    v = COMMERCIAL_REGION.match(v).group(0)
                    cn = self.cn_region(v)
                    if col == "product/fromRegionCode":
                        from_cn, from_orig = cn, v
                    if col == "product/toRegionCode" and from_cn == cn and v != from_orig:
                        cn = OTHER[cn]  # a cross-region transfer stays cross-region
                    row[i] = cn
            elif col == "lineItem/ResourceId" and not v.startswith("arn:"):
                row[i] = self.swap_regions(v, target_or_default)
            elif col == "lineItem/AvailabilityZone":
                row[i] = self.swap_regions(v, target_or_default)
            elif col in ("product/location", "product/fromLocation", "product/toLocation"):
                row[i] = LOCATION_NAMES.sub(CN[target_or_default]["location"], v)
            elif col in ("lineItem/UsageType", "product/usagetype"):
                row[i] = self.usage_type(v, target, product_code)
            elif col == "bill/BillingEntity" and v == "AWS":
                row[i] = CN[target_or_default]["entity"]
            elif col in ("lineItem/LegalEntity", "bill/InvoicingEntity") and v.startswith("Amazon Web Services"):
                row[i] = CN[target_or_default]["legal"]
            elif col == "lineItem/TaxType" and v:
                row[i] = "VAT"
            elif col == "lineItem/LineItemDescription":
                row[i] = self.text(v, target_or_default)
            elif self.remap and col in ("bill/PayerAccountId", "lineItem/UsageAccountId"):
                row[i] = fake_account(v)
            if row[i].startswith("arn:aws:"):
                row[i] = self.swap_regions("arn:aws-cn:" + row[i][len("arn:aws:"):], target_or_default)
                if self.remap:
                    row[i] = re.sub(r":(\d{12}):", lambda m: ":" + fake_account(m.group(1)) + ":", row[i])
        return row


def open_text(path):
    if str(path).endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


def col_type(col):
    return "OptionalBigDecimal" if is_money(col) or col.endswith("Amount") else "OptionalString"


# ---------- optional China-specific rows (--add-china-extras) ----------
VAT_RATE = Decimal("0.06")          # ratio seen between China fee and tax lines
RI_RATE_CNY = Decimal("0.4455")     # a real China RDS RI rate: "CNY 0.4455 hourly fee per PostgreSQL, db.r7g.large instance"
RI_ON_DEMAND_CNY = Decimal("0.8910")


def _money(d):
    return format(d.quantize(Decimal("0.0000000001")).normalize(), "f")


class Extras:
    """Collects what's needed to add China-specific line items to one period."""

    def __init__(self, header):
        self.header = header
        self.idx = {c: i for i, c in enumerate(header)}
        self.base = None
        self.tax = {}  # (entity, product code, usage type, product name) -> CNY

    def observe(self, row):
        g = lambda c: row[self.idx[c]] if c in self.idx else ""
        if self.base is None and g("lineItem/LineItemType") not in ("Tax",):
            self.base = list(row)
        if g("lineItem/LineItemType") in ("Tax", "SavingsPlanNegation"):
            return
        key = (g("bill/BillingEntity"), g("lineItem/ProductCode"), g("lineItem/UsageType"), g("product/ProductName"))
        self.tax[key] = self.tax.get(key, Decimal(0)) + dec(g("lineItem/UnblendedCost"))

    def _row(self, **values):
        row = [""] * len(self.header)
        for c in ("bill/InvoiceId", "bill/BillType", "bill/PayerAccountId", "bill/BillingPeriodStartDate",
                  "bill/BillingPeriodEndDate", "lineItem/UsageAccountId"):
            if c in self.idx:
                row[self.idx[c]] = self.base[self.idx[c]]
        values.setdefault("lineItem/CurrencyCode", "CNY")
        for c, v in values.items():
            if c in self.idx:
                row[self.idx[c]] = v
        return row

    def rows(self, period, account):
        start, end = self.base[self.idx["bill/BillingPeriodStartDate"]], self.base[self.idx["bill/BillingPeriodEndDate"]]
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        hours = int((e - s).total_seconds() // 3600)
        prev = (s - timedelta(days=1)).strftime("%Y/%m")
        out = []
        entity_usage = {}
        for (entity, *_), v in self.tax.items():
            entity_usage[entity] = entity_usage.get(entity, Decimal(0)) + v

        # Enterprise Support fee per billing entity, billed for the previous month
        for entity, usage in entity_usage.items():
            region = NINGXIA if entity == "Ningxia" else BEIJING
            code, legal = CN[region]["code"], CN[region]["legal"]
            fee = max(Decimal("1000"), (usage * Decimal("0.10")))
            out.append(self._row(**{
                "identity/LineItemId": uuid.uuid4().hex, "identity/TimeInterval": f"{start}/{end}",
                "bill/BillingEntity": entity, "bill/InvoicingEntity": legal, "lineItem/LegalEntity": legal,
                "lineItem/LineItemType": "Fee", "lineItem/UsageStartDate": start, "lineItem/UsageEndDate": end,
                "lineItem/ProductCode": "OCBPremiumSupport", "lineItem/UsageType": f"{code}-OCB-OCBPremiumSupport-Units",
                "lineItem/Operation": f"{code}-OCB", "lineItem/UsageAmount": "1",
                "lineItem/UnblendedRate": _money(fee), "lineItem/UnblendedCost": _money(fee),
                "lineItem/BlendedRate": _money(fee), "lineItem/BlendedCost": _money(fee),
                "lineItem/LineItemDescription": f"Enterprise Support for month of {prev} - {entity} Region",
                "product/ProductName": "Enterprise Support", "product/productFamily": "Fee",
                "product/region": region, "product/regionCode": region, "product/location": CN[region]["location"],
                "pricing/unit": "Units", "pricing/currency": "CNY",
            }))
            key = (entity, "OCBPremiumSupport", f"{code}-OCB-OCBPremiumSupport-Units", "Enterprise Support")
            self.tax[key] = self.tax.get(key, Decimal(0)) + fee

        # One Ningxia RDS Reserved Instance: an RIFee line plus hourly DiscountedUsage
        region, code, legal = NINGXIA, "CNW1", CN[NINGXIA]["legal"]
        ri_arn = f"arn:aws-cn:rds:{region}:{account}:ri:cloudzero-test-ri"
        db_arn = f"arn:aws-cn:rds:{region}:{account}:db:cloudzero-test-db"
        common = {
            "bill/BillingEntity": "Ningxia", "bill/InvoicingEntity": legal, "lineItem/LegalEntity": legal,
            "lineItem/ProductCode": "AmazonRDS", "lineItem/Operation": "CreateDBInstance:0014",
            "product/ProductName": "Amazon Relational Database Service", "product/region": region,
            "product/regionCode": region, "product/location": CN[region]["location"], "product/databaseEngine": "PostgreSQL",
            "product/instanceType": "db.r7g.large", "pricing/currency": "CNY", "pricing/unit": "Hrs",
            "pricing/term": "Reserved", "pricing/PurchaseOption": "No Upfront", "pricing/LeaseContractLength": "1yr",
            "pricing/OfferingClass": "standard", "reservation/ReservationARN": ri_arn,
        }
        fee = RI_RATE_CNY * hours
        out.append(self._row(**common, **{
            "identity/LineItemId": uuid.uuid4().hex, "identity/TimeInterval": f"{start}/{end}",
            "lineItem/LineItemType": "RIFee", "lineItem/UsageStartDate": start, "lineItem/UsageEndDate": end,
            "lineItem/UsageType": f"{code}-HeavyUsage:db.r7g.large", "lineItem/UsageAmount": str(hours),
            "lineItem/UnblendedRate": _money(RI_RATE_CNY), "lineItem/UnblendedCost": _money(fee),
            "lineItem/BlendedRate": _money(RI_RATE_CNY), "lineItem/BlendedCost": _money(fee),
            "lineItem/LineItemDescription": f"CNY {RI_RATE_CNY} hourly fee per PostgreSQL, db.r7g.large instance",
            "product/productFamily": "Database Instance", "reservation/NumberOfReservations": "1",
            "reservation/TotalReservedUnits": str(hours), "reservation/UnusedQuantity": "0",
            "reservation/UnusedRecurringFee": "0", "reservation/AmortizedUpfrontFeeForBillingPeriod": "0",
            "reservation/UnusedAmortizedUpfrontFeeForBillingPeriod": "0", "reservation/UpfrontValue": "0",
            "reservation/StartTime": start, "reservation/EndTime": end,
        }))
        for h in range(hours):
            a = (s + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            b = (s + timedelta(hours=h + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append(self._row(**common, **{
                "identity/LineItemId": uuid.uuid4().hex, "identity/TimeInterval": f"{a}/{b}",
                "lineItem/LineItemType": "DiscountedUsage", "lineItem/UsageStartDate": a, "lineItem/UsageEndDate": b,
                "lineItem/UsageType": f"{code}-InstanceUsage:db.r7g.large", "lineItem/ResourceId": db_arn,
                "lineItem/UsageAmount": "1", "lineItem/UnblendedRate": "0", "lineItem/UnblendedCost": "0",
                "lineItem/BlendedRate": "0", "lineItem/BlendedCost": "0",
                "lineItem/LineItemDescription": "PostgreSQL, db.r7g.large reserved instance applied",
                "product/productFamily": "Database Instance",
                "pricing/publicOnDemandRate": _money(RI_ON_DEMAND_CNY), "pricing/publicOnDemandCost": _money(RI_ON_DEMAND_CNY),
                "reservation/EffectiveCost": _money(RI_RATE_CNY), "reservation/RecurringFeeForUsage": _money(RI_RATE_CNY),
                "reservation/AmortizedUpfrontCostForUsage": "0",
            }))
        key = ("Ningxia", "AmazonRDS", f"{code}-HeavyUsage:db.r7g.large", "Amazon Relational Database Service")
        self.tax[key] = self.tax.get(key, Decimal(0)) + fee

        # 6% VAT per billing entity, product and usage type, replacing commercial sales tax
        for (entity, product, usage_type, name), amount in sorted(self.tax.items()):
            t = amount * VAT_RATE
            if t <= 0:
                continue
            region = NINGXIA if entity == "Ningxia" else BEIJING
            legal = CN[region]["legal"]
            out.append(self._row(**{
                "identity/LineItemId": uuid.uuid4().hex, "identity/TimeInterval": f"{start}/{end}",
                "bill/BillingEntity": entity, "bill/InvoicingEntity": legal, "lineItem/LegalEntity": legal,
                "lineItem/LineItemType": "Tax", "lineItem/UsageStartDate": start, "lineItem/UsageEndDate": end,
                "lineItem/ProductCode": product, "lineItem/UsageType": usage_type, "lineItem/UsageAmount": "1",
                "lineItem/UnblendedCost": _money(t), "lineItem/BlendedCost": _money(t),
                "lineItem/LineItemDescription": f"Tax for product code {product} usage type {usage_type}",
                "lineItem/TaxType": "VAT", "product/ProductName": name,
            }))
        return out


class PeriodWriter:
    """Streams rows for one billing period into rolling gzip CSV files."""

    def __init__(self, out, prefix, report, period, header):
        self.prefix, self.report, self.period, self.header = prefix, report, period, header
        self.assembly = str(uuid.uuid4())
        self.base = out / prefix / report / period
        self.dir = self.base / self.assembly
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keys, self.rows, self.in_file, self.fh, self.first_payer = [], 0, 0, None, None
        self._roll()

    def _roll(self):
        if self.fh:
            self.fh.close()
        name = f"{self.report}-{len(self.keys) + 1:05d}.csv.gz"
        self.fh = gzip.open(self.dir / name, "wt", encoding="utf-8", newline="")
        self.writer = csv.writer(self.fh, lineterminator="\n")
        self.writer.writerow(self.header)
        self.keys.append(f"{self.prefix}/{self.report}/{self.period}/{self.assembly}/{name}")
        self.in_file = 0

    def write(self, row):
        if self.in_file >= MAX_ROWS:
            self._roll()
        self.writer.writerow(row)
        self.in_file += 1
        self.rows += 1

    def close(self):
        self.fh.close()


def load_source_manifest(path):
    if not path:
        return None
    m = json.loads(pathlib.Path(path).read_text())
    return {f"{c['category']}/{c['name']}": c for c in m.get("columns", [])}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+", help="CUR CSV files or directories containing them")
    ap.add_argument("--out", required=True)
    ap.add_argument("--prefix", default="cur")
    ap.add_argument("--report", default="china-hourly")
    ap.add_argument("--bucket", default="YOUR-FAKE-CHINA-BUCKET", help="written into the manifests")
    ap.add_argument("--manifest", help="the source export's own Manifest.json, to reuse AWS's real column types")
    ap.add_argument("--usd-to-cny", default="7.15", help="CNY per 1 USD used to fake the yuan amounts")
    ap.add_argument("--beijing-regions", default="us-west-2",
                    help="comma-separated commercial regions to map to Beijing; all others map to Ningxia")
    ap.add_argument("--remap-accounts", action="store_true")
    ap.add_argument("--add-china-extras", action="store_true",
                    help="add China line items the source lacks: Enterprise Support fees, an RDS RI, 6%% VAT")
    args = ap.parse_args()

    files = []
    for src in args.sources:
        p = pathlib.Path(src)
        files += sorted(x for x in p.rglob("*") if x.name.endswith((".csv", ".csv.gz"))) if p.is_dir() else [p]
    if not files:
        raise SystemExit("No CUR CSV files found")

    factor = Decimal(args.usd_to_cny)
    mapper = Mapper([r for r in args.beijing_regions.split(",") if r], args.remap_accounts)
    source_cols = load_source_manifest(args.manifest)
    out = pathlib.Path(args.out)
    writers, extras, header = {}, {}, None

    for f in files:
        with open_text(f) as fh:
            reader = csv.reader(fh)
            h = next(reader)
            if header is None:
                header = h
                idx = {c: i for i, c in enumerate(header)}
                for need in ("bill/BillingPeriodStartDate", "bill/BillingPeriodEndDate", "bill/PayerAccountId"):
                    if need not in idx:
                        raise SystemExit(f"{f} is missing {need}; is this a Legacy CUR CSV?")
            elif h != header:
                raise SystemExit(f"{f} has a different header from the first file; convert it separately")
            for row in reader:
                if len(row) < len(header):
                    row += [""] * (len(header) - len(row))
                start = row[idx["bill/BillingPeriodStartDate"]][:10].replace("-", "")
                end = row[idx["bill/BillingPeriodEndDate"]][:10].replace("-", "")
                period = f"{start}-{end}"
                w = writers.get(period)
                if w is None:
                    w = writers[period] = PeriodWriter(out, args.prefix, args.report, period, header)
                    w.first_payer = row[idx["bill/PayerAccountId"]]
                    extras[period] = Extras(header)
                if args.add_china_extras and row[idx["lineItem/LineItemType"]] == "Tax":
                    continue  # replaced by VAT lines below
                row = mapper.row(header, row, factor, idx)
                extras[period].observe(row)
                w.write(row)

    for period, w in writers.items():
        if args.add_china_extras:
            account = extras[period].base[header.index("lineItem/UsageAccountId")]
            added = extras[period].rows(period, account)
            for r in added:
                w.write(r)
            print(f"{period}: added {len(added)} China line items (support fees, RDS RI, VAT)")
        w.close()
        s_, e_ = period.split("-")
        columns = []
        for c in header:
            cat, name = c.split("/", 1)
            t = source_cols[c]["type"] if source_cols and c in source_cols else col_type(c)
            columns.append({"category": cat, "name": name, "type": t})
        payer = fake_account(w.first_payer) if args.remap_accounts else w.first_payer
        manifest = {
            "assemblyId": w.assembly,
            "account": payer,
            "columns": columns,
            "charset": "UTF-8",
            "compression": "GZIP",
            "contentType": "text/csv",
            "reportId": hashlib.sha256(args.report.encode()).hexdigest(),
            "reportName": args.report,
            "billingPeriod": {"start": f"{s_}T000000.000Z", "end": f"{e_}T000000.000Z"},
            "bucket": args.bucket,
            "reportKeys": w.keys,
            "additionalArtifactKeys": [],
        }
        body = json.dumps(manifest, indent=2)
        (w.dir / f"{args.report}-Manifest.json").write_text(body)
        (w.base / f"{args.report}-Manifest.json").write_text(body)
        money = sum(1 for c in header if is_money(c))
        print(f"{period}: {w.rows} rows, {len(header)} columns ({money} converted to CNY) "
              f"-> {len(w.keys)} file(s), assembly {w.assembly}")


if __name__ == "__main__":
    main()
