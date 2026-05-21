# Business Email Scraper

Python-based web scraper for extracting and classifying publicly available business email addresses.

## Features

- Website crawling
- Email extraction
- Domain matching
- Freemailer detection
- SSL fallback handling
- Batch processing
- Excel export
- Email classification
- Brand token matching
- External role detection

## Technologies

- Python
- BeautifulSoup
- Requests
- Pandas
- OpenPyXL
- urllib3
- tldextract

## Installation

Clone the repository:

```bash
git clone https://github.com/ismailkarab/business-email-scraper.git
cd business-email-scraper
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

Run the scraper for a single website:

```bash
python email_scraper.py https://example.com
```

Example with additional options:

```bash
python email_scraper.py https://example.com --max-pages 50 --debug
```

## Batch Processing

Run the scraper with an Excel or CSV input file:

```bash
python batch_processor.py input.xlsx
```

Custom output file:

```bash
python batch_processor.py input.xlsx --output results.xlsx
```

## Output

The batch processor generates an Excel file with:

- Overview sheet
- Email details sheet
- Email classifications
- Domain matching information
- Crawled source URLs

## Email Categories

The scraper classifies emails into:

- Matching company domain
- Matching freemailer
- External role emails
- Other emails

## Example Use Cases

- Business contact research
- Public company email extraction
- Data enrichment
- Lead generation preparation
- Domain-based email validation

## Disclaimer

This project is intended for educational and research purposes only.

Users are responsible for complying with all applicable laws, website terms of service, and privacy regulations when using this software.

## License

MIT License
