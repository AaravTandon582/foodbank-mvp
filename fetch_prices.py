"""Pull real prices from every configured source into the app.

  python3 fetch_prices.py            # only items running low
  python3 fetch_prices.py --all      # every tracked item
  python3 fetch_prices.py --dry-run  # show what would be written

Stores that turn up in the data but are not in your markets list get added
with distance 0 and left disabled, so a new store never silently wins the
shopping list before you have told it how far away it is.
"""
import argparse
import sys

import db
import refresh
import shopping


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="price every tracked item")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-demo", action="store_true",
                    help="skip demo gap-filling; real prices only")
    args = ap.parse_args()

    db.init()

    with db.session() as conn:
        if args.all:
            names = [r["name"] for r in conn.execute("SELECT name FROM items ORDER BY name")]
        else:
            names = [i["name"] for i in shopping.low_stock()]

    if not names:
        print("Nothing to price — everything is at par. Use --all to price everything.")
        return 0

    print(f"Pricing {len(names)} items\n")
    report = refresh.refresh(names, use_demo=not args.no_demo, dry_run=args.dry_run)

    for name, result in report["sources"].items():
        print(f"  {name:<24} {result}{'' if isinstance(result, str) else ' prices'}")

    if not report["observations"]:
        print("\nNo real prices found. Check network, API keys, or add rows to prices.csv.")
        return 1

    if args.dry_run:
        print(f"\n--dry-run, would write {len(report['observations'])} rows:")
        for o in sorted(report["observations"], key=lambda x: (x["item"], x["price"])):
            print(f"  {o['item']:<16} {o['store'][:24]:<26} ${o['price']:>7.2f}  [{o['source']}]")
        return 0

    print(f"\nWrote {report['written']} real prices.")
    if report["demo_filled"]:
        print(f"Filled {report['demo_filled']} remaining gaps with demo prices.")
    if report["rejected"]:
        print(f"Discarded {report['rejected']} observations that were not plausible prices.")
    if report["new_stores"]:
        print(f"\nNew stores found ({len(report['new_stores'])}) — added but switched "
              f"off until you set a distance on the Markets page:")
        for name in report["new_stores"]:
            print(f"  · {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
