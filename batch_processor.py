from pathlib import Path
import argparse

import pandas as pd

from Scraper import crawl_domain_for_emails


def join_values(values: list[str]) -> str:
    return " | ".join(values) if values else ""


def join_email_sources(emails: list[str], sources_per_email: dict[str, list[str]]) -> str:
    """
    Kompakte Darstellung für die Übersichts-Tabelle:
    email@example.de <- https://domain.de/kontakt | ...
    """
    parts = []
    for email in emails:
        sources = sources_per_email.get(email, [])
        if sources:
            parts.append(f"{email} <- {', '.join(sources)}")
        else:
            parts.append(f"{email} <- Quelle nicht gespeichert")
    return " | ".join(parts)


def read_input_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return pd.read_excel(path)
    if suffix == ".csv":
        return pd.read_csv(path)
    raise ValueError("Eingabedatei muss .xlsx, .xls oder .csv sein.")


def detect_url_column(df: pd.DataFrame) -> str:
    candidates = ["URL", "Url", "url", "Website", "website", "Domain", "domain", "Link", "link"]
    for col in candidates:
        if col in df.columns:
            return col
    raise ValueError(
        "Keine URL-Spalte gefunden. Verwende eine Spalte mit dem Namen URL, Website, Domain oder Link."
    )


def classification_label(classification: str) -> str:
    labels = {
        "matching_company_domain": "Domain-passend / Firmen-Domain",
        "matching_freemailer": "Domain-passend / Freemailer",
        "external_role": "Externe Rolle",
        "other": "Andere Email",
    }
    return labels.get(classification, classification or "Unbekannt")


def main():
    ap = argparse.ArgumentParser(description="Batch-Export für Email-Scraper mit Excel/CSV Input und Excel Output.")
    ap.add_argument("input_file", help="Pfad zur Eingabe-Datei (.xlsx / .xls / .csv)")
    ap.add_argument("--output", default="scraper_output.xlsx", help="Pfad zur Ausgabe-Datei (.xlsx)")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--max-assets", type=int, default=80)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    input_path = Path(args.input_file)
    output_path = Path(args.output)

    df = read_input_file(input_path)
    url_column = detect_url_column(df)

    summary_rows = []
    detail_rows = []

    for idx, raw_url in enumerate(df[url_column].fillna(""), start=1):
        url = str(raw_url).strip()
        if not url:
            summary_rows.append({
                "URL": "",
                "Status": "Fehlende URL",
                "Domain-passend / Firmen-Domain": "",
                "Domain-passend / Freemailer": "",
                "Domain-passend / Gesamt": "",
                "Externe Rollen": "",
                "Andere Emails": "",
                "Alle Emails": "",
                "Quellen Firmen-Domain": "",
                "Quellen Freemailer": "",
                "Quellen Externe Rollen": "",
                "Quellen Andere Emails": "",
                "Anzahl Firmen-Domain": 0,
                "Anzahl Freemailer": 0,
                "Anzahl passend gesamt": 0,
                "Anzahl externe Rollen": 0,
                "Anzahl andere": 0,
                "Anzahl alle": 0,
                "Seiten gecrawlt": 0,
                "Assets gecrawlt": 0,
                "Blocked": False,
                "Eingabe-Domain": "",
                "Erlaubte/Ziel-Domains": "",
                "Brand-Tokens": "",
                "Hinweis": "Leere URL-Zeile",
            })
            continue

        print(f"[{idx}/{len(df)}] Verarbeite: {url}")

        try:
            data = crawl_domain_for_emails(
                url,
                max_pages=args.max_pages,
                max_assets=args.max_assets,
                timeout=args.timeout,
                debug=args.debug,
                max_sources_per_email=3,
            )

            company_domain_emails = data.get("emails_matching_company_domain", [])
            freemailer_emails = data.get("emails_matching_freemailer", [])
            matching_emails = data.get("emails_matching_domain", [])
            external_role_emails = data.get("emails_external_role", [])
            other_emails = data.get("emails_other", [])
            all_emails = data.get("emails_all", [])
            sources_per_email = data.get("sources_per_email", {})
            classifications = data.get("email_classification", {})

            summary_rows.append({
                "URL": url,
                "Status": "OK" if not data["blocked"] else "Blocked",
                "Domain-passend / Firmen-Domain": join_values(company_domain_emails),
                "Domain-passend / Freemailer": join_values(freemailer_emails),
                "Domain-passend / Gesamt": join_values(matching_emails),
                "Externe Rollen": join_values(external_role_emails),
                "Andere Emails": join_values(other_emails),
                "Alle Emails": join_values(all_emails),
                "Quellen Firmen-Domain": join_email_sources(company_domain_emails, sources_per_email),
                "Quellen Freemailer": join_email_sources(freemailer_emails, sources_per_email),
                "Quellen Externe Rollen": join_email_sources(external_role_emails, sources_per_email),
                "Quellen Andere Emails": join_email_sources(other_emails, sources_per_email),
                "Anzahl Firmen-Domain": len(company_domain_emails),
                "Anzahl Freemailer": len(freemailer_emails),
                "Anzahl passend gesamt": len(matching_emails),
                "Anzahl externe Rollen": len(external_role_emails),
                "Anzahl andere": len(other_emails),
                "Anzahl alle": len(all_emails),
                "Seiten gecrawlt": data["pages_crawled"],
                "Assets gecrawlt": data["assets_crawled"],
                "Blocked": data["blocked"],
                "Eingabe-Domain": data.get("registrable_domain", ""),
                "Erlaubte/Ziel-Domains": join_values(data.get("allowed_reg_domains", [])),
                "Brand-Tokens": join_values(data.get("brand_tokens", [])),
                "Hinweis": "",
            })

            for email in all_emails:
                sources = sources_per_email.get(email, [])
                cls = classifications.get(email, "other")
                detail_rows.append({
                    "URL": url,
                    "Email": email,
                    "Kategorie": classification_label(cls),
                    "Technische Kategorie": cls,
                    "Quelle(n)": join_values(sources),
                    "Erste Quelle": sources[0] if sources else "",
                    "Eingabe-Domain": data.get("registrable_domain", ""),
                    "Erlaubte/Ziel-Domains": join_values(data.get("allowed_reg_domains", [])),
                    "Brand-Tokens": join_values(data.get("brand_tokens", [])),
                    "Seiten gecrawlt": data["pages_crawled"],
                    "Assets gecrawlt": data["assets_crawled"],
                    "Blocked": data["blocked"],
                })

        except Exception as e:
            summary_rows.append({
                "URL": url,
                "Status": "Fehler",
                "Domain-passend / Firmen-Domain": "",
                "Domain-passend / Freemailer": "",
                "Domain-passend / Gesamt": "",
                "Externe Rollen": "",
                "Andere Emails": "",
                "Alle Emails": "",
                "Quellen Firmen-Domain": "",
                "Quellen Freemailer": "",
                "Quellen Externe Rollen": "",
                "Quellen Andere Emails": "",
                "Anzahl Firmen-Domain": 0,
                "Anzahl Freemailer": 0,
                "Anzahl passend gesamt": 0,
                "Anzahl externe Rollen": 0,
                "Anzahl andere": 0,
                "Anzahl alle": 0,
                "Seiten gecrawlt": 0,
                "Assets gecrawlt": 0,
                "Blocked": False,
                "Eingabe-Domain": "",
                "Erlaubte/Ziel-Domains": "",
                "Brand-Tokens": "",
                "Hinweis": str(e),
            })

    summary_df = pd.DataFrame(summary_rows)
    details_df = pd.DataFrame(detail_rows)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, index=False, sheet_name="Übersicht")
        details_df.to_excel(writer, index=False, sheet_name="Email-Details")

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            ws.freeze_panes = "A2"
            for column_cells in ws.columns:
                max_len = 0
                col_letter = column_cells[0].column_letter
                for cell in column_cells:
                    value = "" if cell.value is None else str(cell.value)
                    max_len = max(max_len, min(len(value), 80))
                ws.column_dimensions[col_letter].width = max(12, min(max_len + 2, 80))

    print(f"\nFertig. Ausgabe gespeichert in: {output_path}")
    print("Enthaltene Tabellenblätter: Übersicht, Email-Details")


if __name__ == "__main__":
    main()
