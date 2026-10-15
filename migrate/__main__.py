import argparse
import sys

from . import pipeline, report, sample


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m migrate", description="Legacy ERP to target ERP data migration")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="write a synthetic legacy export")
    s.add_argument("--out", default="data/legacy")
    r = sub.add_parser("run", help="cleanse, validate, reconcile and write the load files and report")
    r.add_argument("--source", default="data/legacy")
    r.add_argument("--out", default="output")
    r.add_argument("--mapping", default=None)
    r.add_argument("--decisions", default=None, help="YAML file with business decisions on blocking issues")
    r.add_argument("--strict", action="store_true", help="exit 1 unless the status is GO")
    a = p.parse_args(argv)

    if a.cmd == "sample":
        print(f"wrote {sample.generate(a.out)}")
        return 0
    result = pipeline.run(a.source, pipeline.load_mapping(a.mapping) if a.mapping else None,
                          pipeline.load_decisions(a.decisions))
    path = report.write(result, a.out)
    for k, v in report.summary_lines(result):
        print(f"{k:36} {v}")
    print(f"\nreport: {path}")
    return 0 if (result.go or not a.strict) else 1


if __name__ == "__main__":
    sys.exit(main())
