"""Generate a small, fully synthetic commercial AWS Legacy CUR for testing.

Every value is made up: account IDs, resource IDs, tags and prices. Nothing
comes from a real AWS account. The output is one billing period of hourly
data in the Legacy CUR 1.0 CSV layout, covering the line item types the relay
has to handle: Usage, SavingsPlanCoveredUsage, SavingsPlanNegation,
SavingsPlanRecurringFee and Tax.

Usage:
  python3 make_sample_commercial_cur.py --out sample-commercial-cur.csv.gz [--month 2026-08]
"""
import argparse
import csv
import gzip
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

PAYER, USAGE = "111111111111", "222222222222"
SP_ARN = f"arn:aws:savingsplans::{PAYER}:savingsplan/00000000-0000-0000-0000-000000000001"

HEADER = [
    "identity/LineItemId", "identity/TimeInterval",
    "bill/InvoiceId", "bill/InvoicingEntity", "bill/BillingEntity", "bill/BillType", "bill/PayerAccountId",
    "bill/BillingPeriodStartDate", "bill/BillingPeriodEndDate",
    "lineItem/UsageAccountId", "lineItem/LineItemType", "lineItem/UsageStartDate", "lineItem/UsageEndDate",
    "lineItem/ProductCode", "lineItem/UsageType", "lineItem/Operation", "lineItem/AvailabilityZone",
    "lineItem/ResourceId", "lineItem/UsageAmount", "lineItem/NormalizationFactor", "lineItem/NormalizedUsageAmount",
    "lineItem/CurrencyCode", "lineItem/UnblendedRate", "lineItem/UnblendedCost", "lineItem/BlendedRate",
    "lineItem/BlendedCost", "lineItem/LineItemDescription", "lineItem/TaxType", "lineItem/LegalEntity",
    "product/ProductName", "product/instanceType", "product/location", "product/fromLocation",
    "product/toLocation", "product/fromRegionCode", "product/toRegionCode", "product/operatingSystem",
    "product/productFamily", "product/region", "product/regionCode", "product/servicecode",
    "product/usagetype", "product/volumeType", "product/databaseEngine",
    "pricing/currency", "pricing/publicOnDemandCost", "pricing/publicOnDemandRate", "pricing/RateCode",
    "pricing/term", "pricing/unit",
    "savingsPlan/SavingsPlanARN", "savingsPlan/SavingsPlanRate", "savingsPlan/SavingsPlanEffectiveCost",
    "savingsPlan/TotalCommitmentToDate", "savingsPlan/UsedCommitment", "savingsPlan/RecurringCommitmentForBillingPeriod",
    "savingsPlan/OfferingType", "savingsPlan/PaymentOption", "savingsPlan/PurchaseTerm", "savingsPlan/Region",
    "resourceTags/user:team", "resourceTags/user:environment", "resourceTags/user:service",
]

LOC = {"us-east-1": ("US East (N. Virginia)", ""), "us-west-2": ("US West (Oregon)", "USW2-")}

# name, region, instance type, hourly rate, team, env, service, covered by the Savings Plan
EC2 = [
    ("i-0a1b2c3d4e5f60001", "us-east-1", "m5.large", "0.096", "platform", "production", "api", True),
    ("i-0a1b2c3d4e5f60002", "us-east-1", "m5.large", "0.096", "platform", "production", "api", False),
    ("i-0a1b2c3d4e5f60003", "us-east-1", "c5.xlarge", "0.17", "data", "production", "ingest", False),
    ("i-0a1b2c3d4e5f60004", "us-west-2", "t3.medium", "0.0416", "web", "staging", "frontend", False),
]
SP_RATE = Decimal("0.0624")  # Compute Savings Plan rate for m5.large


def money(d):
    return format(d.quantize(Decimal("0.0000000001")).normalize(), "f")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--month", default="2026-08")
    args = ap.parse_args()
    rnd = random.Random(42)

    start = datetime.fromisoformat(args.month + "-01").replace(tzinfo=timezone.utc)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    hours = int((end - start).total_seconds() // 3600)
    rows, tax = [], {}

    def row(**v):
        r = {c: "" for c in HEADER}
        r.update({
            "identity/LineItemId": uuid.UUID(int=rnd.getrandbits(128)).hex,
            "bill/InvoiceId": "100000000001", "bill/InvoicingEntity": "Amazon Web Services, Inc.",
            "bill/BillingEntity": "AWS", "bill/BillType": "Anniversary", "bill/PayerAccountId": PAYER,
            "bill/BillingPeriodStartDate": iso(start), "bill/BillingPeriodEndDate": iso(end),
            "lineItem/UsageAccountId": USAGE, "lineItem/CurrencyCode": "USD",
            "lineItem/LegalEntity": "Amazon Web Services, Inc.", "pricing/currency": "USD",
        })
        r.update(v)
        if r["lineItem/LineItemType"] not in ("Tax", "SavingsPlanNegation"):
            key = (r["lineItem/ProductCode"], r["lineItem/UsageType"], r["product/ProductName"])
            tax[key] = tax.get(key, Decimal(0)) + Decimal(r["lineItem/UnblendedCost"] or 0)
        rows.append(r)

    def place(region):
        loc, code = LOC[region]
        return {"product/region": region, "product/regionCode": region, "product/location": loc}, code

    for h in range(hours):
        a, b = start + timedelta(hours=h), start + timedelta(hours=h + 1)
        t = {"identity/TimeInterval": f"{iso(a)}/{iso(b)}", "lineItem/UsageStartDate": iso(a), "lineItem/UsageEndDate": iso(b)}

        for rid, region, itype, rate, team, env, svc, covered in EC2:
            p, code = place(region)
            tags = {"resourceTags/user:team": team, "resourceTags/user:environment": env, "resourceTags/user:service": svc}
            ut = f"{code}BoxUsage:{itype}"
            common = dict(t, **p, **tags, **{
                "lineItem/ProductCode": "AmazonEC2", "lineItem/UsageType": ut, "lineItem/Operation": "RunInstances",
                "lineItem/AvailabilityZone": region + "a", "lineItem/ResourceId": rid, "lineItem/UsageAmount": "1",
                "product/ProductName": "Amazon Elastic Compute Cloud", "product/instanceType": itype,
                "product/operatingSystem": "Linux", "product/productFamily": "Compute Instance",
                "product/servicecode": "AmazonEC2", "product/usagetype": ut,
                "pricing/publicOnDemandCost": rate, "pricing/publicOnDemandRate": rate, "pricing/unit": "Hrs"})
            if covered:
                row(**common, **{
                    "lineItem/LineItemType": "SavingsPlanCoveredUsage", "lineItem/UnblendedRate": rate,
                    "lineItem/UnblendedCost": rate, "lineItem/BlendedRate": rate, "lineItem/BlendedCost": rate,
                    "lineItem/LineItemDescription": f"Linux/UNIX {itype} covered by Compute Savings Plan",
                    "pricing/term": "OnDemand", "savingsPlan/SavingsPlanARN": SP_ARN,
                    "savingsPlan/SavingsPlanRate": money(SP_RATE), "savingsPlan/SavingsPlanEffectiveCost": money(SP_RATE)})
                row(**common, **{
                    "lineItem/LineItemType": "SavingsPlanNegation", "lineItem/UnblendedRate": "-" + rate,
                    "lineItem/UnblendedCost": "-" + rate, "lineItem/BlendedRate": "-" + rate, "lineItem/BlendedCost": "-" + rate,
                    "lineItem/LineItemDescription": f"SavingsPlanNegation used by AccountId : {USAGE} and UsageSku : SKU0001",
                    "pricing/term": "OnDemand", "savingsPlan/SavingsPlanARN": SP_ARN})
            else:
                row(**common, **{
                    "lineItem/LineItemType": "Usage", "lineItem/UnblendedRate": rate, "lineItem/UnblendedCost": rate,
                    "lineItem/BlendedRate": rate, "lineItem/BlendedCost": rate, "pricing/term": "OnDemand",
                    "lineItem/LineItemDescription": f"${rate} per On Demand Linux {itype} Instance Hour"})

        # NAT gateway hours in us-east-1
        p, code = place("us-east-1")
        row(**t, **p, **{"lineItem/LineItemType": "Usage", "lineItem/ProductCode": "AmazonEC2",
            "lineItem/UsageType": "NatGateway-Hours", "lineItem/Operation": "NatGateway",
            "lineItem/ResourceId": f"arn:aws:ec2:us-east-1:{USAGE}:natgateway/nat-0f1e2d3c4b5a60001",
            "lineItem/UsageAmount": "1", "lineItem/UnblendedRate": "0.045", "lineItem/UnblendedCost": "0.045",
            "lineItem/BlendedRate": "0.045", "lineItem/BlendedCost": "0.045",
            "lineItem/LineItemDescription": "$0.045 per NAT Gateway Hour", "product/ProductName": "Amazon Elastic Compute Cloud",
            "product/productFamily": "NAT Gateway", "product/servicecode": "AmazonEC2", "product/usagetype": "NatGateway-Hours",
            "pricing/publicOnDemandCost": "0.045", "pricing/publicOnDemandRate": "0.045", "pricing/term": "OnDemand",
            "pricing/unit": "Hrs", "resourceTags/user:team": "platform", "resourceTags/user:environment": "production"})

        # Lambda compute, varying by hour
        gbs = Decimal(rnd.randint(20000, 90000))
        cost = gbs * Decimal("0.0000166667")
        row(**t, **p, **{"lineItem/LineItemType": "Usage", "lineItem/ProductCode": "AWSLambda",
            "lineItem/UsageType": "Lambda-GB-Second", "lineItem/Operation": "Invoke",
            "lineItem/ResourceId": f"arn:aws:lambda:us-east-1:{USAGE}:function:order-processor",
            "lineItem/UsageAmount": str(gbs), "lineItem/UnblendedRate": "0.0000166667", "lineItem/UnblendedCost": money(cost),
            "lineItem/BlendedRate": "0.0000166667", "lineItem/BlendedCost": money(cost),
            "lineItem/LineItemDescription": "AWS Lambda - Total Compute - US East (Northern Virginia)",
            "product/ProductName": "AWS Lambda", "product/productFamily": "Serverless", "product/servicecode": "AWSLambda",
            "product/usagetype": "Lambda-GB-Second", "pricing/publicOnDemandCost": money(cost),
            "pricing/publicOnDemandRate": "0.0000166667", "pricing/term": "OnDemand", "pricing/unit": "Lambda-GB-Second",
            "resourceTags/user:team": "data", "resourceTags/user:environment": "production", "resourceTags/user:service": "orders"})

        # Cross-region data transfer, us-east-1 to us-west-2
        gb = Decimal(rnd.randint(5, 40)) / Decimal(10)
        cost = gb * Decimal("0.02")
        row(**t, **p, **{"lineItem/LineItemType": "Usage", "lineItem/ProductCode": "AWSDataTransfer",
            "lineItem/UsageType": "USE1-USW2-AWS-Out-Bytes", "lineItem/Operation": "",
            "lineItem/UsageAmount": str(gb), "lineItem/UnblendedRate": "0.02", "lineItem/UnblendedCost": money(cost),
            "lineItem/BlendedRate": "0.02", "lineItem/BlendedCost": money(cost),
            "lineItem/LineItemDescription": "$0.02 per GB - US East (Northern Virginia) data transfer to US West (Oregon)",
            "product/ProductName": "AWS Data Transfer", "product/productFamily": "Data Transfer",
            "product/fromLocation": "US East (N. Virginia)", "product/toLocation": "US West (Oregon)",
            "product/fromRegionCode": "us-east-1", "product/toRegionCode": "us-west-2",
            "product/servicecode": "AWSDataTransfer", "product/usagetype": "USE1-USW2-AWS-Out-Bytes",
            "pricing/publicOnDemandCost": money(cost), "pricing/publicOnDemandRate": "0.02", "pricing/term": "OnDemand",
            "pricing/unit": "GB"})

        # Savings Plan recurring fee, hourly commitment
        if True:
            row(**t, **{"lineItem/LineItemType": "SavingsPlanRecurringFee", "lineItem/ProductCode": "ComputeSavingsPlans",
                "lineItem/UsageType": "ComputeSP:1yrNoUpfront", "lineItem/UsageAmount": "1",
                "lineItem/UnblendedRate": money(SP_RATE), "lineItem/UnblendedCost": money(SP_RATE),
                "lineItem/BlendedRate": money(SP_RATE), "lineItem/BlendedCost": money(SP_RATE),
                "lineItem/LineItemDescription": "1 year No Upfront Compute Savings Plan",
                "product/ProductName": "Savings Plans for AWS Compute usage", "product/productFamily": "Savings Plans",
                "savingsPlan/SavingsPlanARN": SP_ARN, "savingsPlan/TotalCommitmentToDate": money(SP_RATE),
                "savingsPlan/UsedCommitment": money(SP_RATE), "savingsPlan/OfferingType": "ComputeSavingsPlans",
                "savingsPlan/PaymentOption": "No Upfront", "savingsPlan/PurchaseTerm": "1yr", "savingsPlan/Region": "Any"})

        # Daily storage for S3 and EBS, recorded in the first hour of each day
        if a.hour == 0:
            for region, bucket, gbmo in (("us-east-1", "example-data-lake", "41.9"), ("us-west-2", "example-web-assets", "3.2")):
                p2, code2 = place(region)
                cost = Decimal(gbmo) * Decimal("0.023")
                row(**t, **p2, **{"lineItem/LineItemType": "Usage", "lineItem/ProductCode": "AmazonS3",
                    "lineItem/UsageType": f"{code2}TimedStorage-ByteHrs", "lineItem/Operation": "StandardStorage",
                    "lineItem/ResourceId": bucket, "lineItem/UsageAmount": gbmo,
                    "lineItem/UnblendedRate": "0.023", "lineItem/UnblendedCost": money(cost),
                    "lineItem/BlendedRate": "0.023", "lineItem/BlendedCost": money(cost),
                    "lineItem/LineItemDescription": "$0.023 per GB - first 50 TB / month of storage used",
                    "product/ProductName": "Amazon Simple Storage Service", "product/productFamily": "Storage",
                    "product/servicecode": "AmazonS3", "product/usagetype": f"{code2}TimedStorage-ByteHrs",
                    "pricing/publicOnDemandCost": money(cost), "pricing/publicOnDemandRate": "0.023",
                    "pricing/term": "OnDemand", "pricing/unit": "GB-Mo", "resourceTags/user:team": "data"})
            for vol, gbmo in (("vol-0a1b2c3d4e5f60001", "3.33"), ("vol-0a1b2c3d4e5f60002", "6.67")):
                cost = Decimal(gbmo) * Decimal("0.08")
                row(**t, **p, **{"lineItem/LineItemType": "Usage", "lineItem/ProductCode": "AmazonEC2",
                    "lineItem/UsageType": "EBS:VolumeUsage.gp3", "lineItem/Operation": "CreateVolume-Gp3",
                    "lineItem/AvailabilityZone": "us-east-1a", "lineItem/ResourceId": vol, "lineItem/UsageAmount": gbmo,
                    "lineItem/UnblendedRate": "0.08", "lineItem/UnblendedCost": money(cost),
                    "lineItem/BlendedRate": "0.08", "lineItem/BlendedCost": money(cost),
                    "lineItem/LineItemDescription": "$0.08 per GB-month of General Purpose (gp3) provisioned storage - US East (Northern Virginia)",
                    "product/ProductName": "Amazon Elastic Compute Cloud", "product/productFamily": "Storage",
                    "product/servicecode": "AmazonEC2", "product/usagetype": "EBS:VolumeUsage.gp3", "product/volumeType": "General Purpose",
                    "pricing/publicOnDemandCost": money(cost), "pricing/publicOnDemandRate": "0.08",
                    "pricing/term": "OnDemand", "pricing/unit": "GB-Mo", "resourceTags/user:team": "platform"})

    # Monthly US sales tax per product and usage type
    for (product, ut, name), amount in sorted(tax.items()):
        t_amt = amount * Decimal("0.0625")
        if t_amt <= 0:
            continue
        row(**{"identity/TimeInterval": f"{iso(start)}/{iso(end)}", "lineItem/LineItemType": "Tax",
            "lineItem/UsageStartDate": iso(start), "lineItem/UsageEndDate": iso(end), "lineItem/ProductCode": product,
            "lineItem/UsageType": ut, "lineItem/UsageAmount": "1", "lineItem/UnblendedCost": money(t_amt),
            "lineItem/BlendedCost": money(t_amt), "lineItem/TaxType": "USSalesTax", "product/ProductName": name,
            "lineItem/LineItemDescription": f"Tax for product code {product} usage type {ut}", "pricing/currency": ""})

    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    total = sum(Decimal(r["lineItem/UnblendedCost"] or 0) for r in rows)
    print(f"{args.out}: {len(rows)} rows, {len(HEADER)} columns, unblended USD {total.quantize(Decimal('0.01'))}")


if __name__ == "__main__":
    main()
