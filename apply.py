import argparse

from assistant import SafeJobApplyAssistant


def load_urls(path: str):
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def main():
    parser = argparse.ArgumentParser(
        description="Open job URLs, draft answers from a profile, and fill forms. Submission stays manual."
    )
    parser.add_argument("--profile", required=True, help="Text file with the candidate profile")
    parser.add_argument("--urls", required=True, help="Text file with one job URL per line")
    parser.add_argument("--cv", default=None, help="PDF to upload when a file input is present")
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--log-json", default="logs/application_drafts_log.json")
    parser.add_argument("--log-csv", default="logs/application_drafts_log.csv")
    args = parser.parse_args()

    with open(args.profile, encoding="utf-8") as f:
        profile = f.read()

    urls = load_urls(args.urls)
    if not urls:
        raise SystemExit(f"No URLs found in {args.urls}")

    assistant = SafeJobApplyAssistant(
        profile_context=profile,
        model=args.model,
        headless=args.headless,
        cv_path=args.cv,
    )
    assistant.run(urls)
    assistant.save_logs(json_path=args.log_json, csv_path=args.log_csv)


if __name__ == "__main__":
    main()
