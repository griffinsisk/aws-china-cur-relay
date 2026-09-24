# AWS China CUR relay for CloudZero

Bring AWS China (Beijing and Ningxia) spend into CloudZero, converted from CNY to USD.

CloudZero reads AWS billing data with an IAM role in the commercial AWS partition. IAM trust and S3 replication don't cross into the China partition (`aws-cn`), and China Cost and Usage Reports are billed in CNY. This project is a small Lambda function that closes both gaps:

1. It reads the China account's Legacy CUR with a read-only access key.
2. It converts every money column from CNY to USD, using a monthly rate your finance team controls.
3. It writes the result to a bucket in your commercial account, keeping the exact CUR layout and manifests.

Because the output is a valid AWS CUR, CloudZero can process it with its AWS billing processor, the same way it processes the rest of your AWS spend.

```
AWS China account            Your commercial AWS account                     CloudZero
China CUR bucket (CNY)  -->  Relay Lambda  -->  Destination bucket (USD)  -->  AWS billing processor
```

> This is a community project, not an official CloudZero product. Work with your CloudZero contact before relying on it in production.

## What's here

| Path | What it is |
|---|---|
| [`docs/setup-guide.pdf`](docs/setup-guide.pdf) ([HTML](docs/setup-guide.html)) | Step-by-step setup with a real China account |
| [`docs/test-guide.pdf`](docs/test-guide.pdf) ([HTML](docs/test-guide.html)) | End-to-end test in an ordinary AWS account, with no China account needed |
| [`relay/lambda_function.py`](relay/lambda_function.py) | The relay. Paste it into a Python 3.12 Lambda function. |
| [`test-data/fake-china-cur/`](test-data/fake-china-cur) | A ready-to-upload synthetic China CUR for August 2026: 7,580 rows across Ningxia and Beijing, in CNY |
| [`test-data/sample-commercial-cur-2026-08.csv.gz`](test-data/sample-commercial-cur-2026-08.csv.gz) | The synthetic commercial CUR the China sample was built from |
| [`tools/make_fake_china_cur.py`](tools/make_fake_china_cur.py) | Turns a commercial CUR export into a China-shaped CUR for testing |
| [`tools/make_sample_commercial_cur.py`](tools/make_sample_commercial_cur.py) | Generates the synthetic commercial CUR |
| [`tests/test_round_trip.py`](tests/test_round_trip.py) | Local round-trip test. Needs no AWS access. |

## How it behaves

- **Converted columns.** Any column in the `lineItem`, `pricing`, `reservation`, `savingsPlan` or `discount` categories whose name ends in `Cost`, `Rate`, `Fee`, `Commitment`, `Discount` or `UpfrontValue` is multiplied by the month's rate. Currency columns are set to `USD`. Every other column is copied unchanged. Each run writes an audit record listing the columns it converted.
- **Safe to re-run.** Each output version's ID is derived from the China report version and the rate. If neither has changed, the run does nothing.
- **Month-end restatement.** Update the month's rate and run the relay for that month. The rate change produces a new report version.
- **Fail-safe.** If a month has no rate, the relay stops rather than letting CNY land in CloudZero labeled as USD.

## Status

| Area | Status |
|---|---|
| Relay conversion, CUR layout and manifests, re-run safety | Tested end to end with a 124,516-row, 314-column CUR |
| Processing by CloudZero's AWS billing processor | Requires your CloudZero contact to switch the connection. Validate it with the test guide before using real data. |
| Month-end restatement in CloudZero | Covered by Step 9 of the test guide |

## Cost

For most China footprints this costs a few dollars a month: Lambda at roughly 10,000 rows per second, plus a Secrets Manager secret and a small amount of China data egress. A single Lambda run is capped at 15 minutes, so above roughly 8 million rows per month, run the same code as an ECS Fargate or AWS Batch task instead.

## Test data

Everything in `test-data/` is synthetic: made-up accounts, resources, tags and prices. The China sample covers EC2, EBS, S3, Lambda, a NAT gateway, cross-region transfer, a Compute Savings Plan, an RDS Reserved Instance, Enterprise Support fees and 6% VAT. At a rate of `0.13986014` USD per CNY, the relay should report 7,580 rows, CNY 10,717.04 and USD 1,498.89. Upload it with:

```bash
aws s3 sync ./test-data/fake-china-cur/ s3://YOUR_SOURCE_BUCKET/
```

To rebuild it:

```bash
python3 tools/make_sample_commercial_cur.py --out test-data/sample-commercial-cur-2026-08.csv.gz
python3 tools/make_fake_china_cur.py test-data/sample-commercial-cur-2026-08.csv.gz \
  --out test-data/fake-china-cur --prefix cur --report china-hourly --add-china-extras
```

## Run the local tests

```bash
python3 -m unittest discover tests
```

## Before you use it with real data

Moving billing data out of mainland China may be subject to cross-border data transfer rules. Billing data includes account IDs, resource IDs and tags. Clear it with your legal or compliance team first.

## License

MIT. See [LICENSE](LICENSE).
