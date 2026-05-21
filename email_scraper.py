import re
import json
import argparse
import urllib.parse
import html
from collections import deque, defaultdict
from urllib.parse import urlparse

import requests
from requests.exceptions import SSLError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import ssl
import urllib.request
import gzip
import zlib


# =========================
# Config
# =========================
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

ALLOWED_TLDS = {
    "de", "com", "net", "org", "info", "biz", "eu",
    "at", "ch", "nl", "be", "fr", "it", "es", "pl", "cz", "sk",
    "uk", "io", "me", "tr"
}

PRIORITY_KEYWORDS = [
    "kontakt", "contact", "impressum", "about", "team",
    "datenschutz", "privacy", "legal", "agb", "terms",
    "support", "hilfe", "service"
]

COMMON_PATHS = [
    "/", "/kontakt", "/kontakt/", "/impressum", "/impressum/",
    "/contact", "/contact/", "/about", "/about/",
    "/datenschutz", "/datenschutz/", "/privacy", "/privacy/",
    "/legal", "/legal/", "/terms", "/terms/"
]

SKIP_URL_SUBSTRINGS = [
    "/blog", "/news", "/tag", "/category", "/archive",
    "/page/", "?page=", "&page=", "utm_", "fbclid=",
]

EXTERNAL_DOMAIN_DENYLIST = {
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com", "youtu.be", "twitter.com", "x.com",
    "google.com", "google.de", "gstatic.com", "googleapis.com", "schema.org", "w3.org",
    "cloudflare.com", "cdnjs.cloudflare.com", "cloudflareinsights.com", "cloudfront.net",
    "wordpress.org", "wix.com", "jimdo.com", "ionos.de", "strato.de", "1und1.de",
}

GENERIC_EMAIL_PROVIDERS = {
    # Germany
    "web.de", "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "t-online.de", "mail.de", "posteo.de", "freenet.de",
    # Global
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.de",
    "icloud.com", "me.com",
    "protonmail.com", "proton.me", "protonmail.me",
    "aol.com",
}

BLOCK_STATUSES = {401, 403, 406, 429}

EXTERNAL_ROLE_KEYWORDS = {
    "datenschutz", "privacy", "privacypolicy", "dpo", "dsb",
    "legal", "law", "kanzlei", "anwalt", "attorney",
    "compliance", "gdpr", "avv", "auskunft", "privacyteam",
}

GENERIC_BRAND_TOKENS = {
    "shop", "store", "onlineshop", "online", "webshop", "ecommerce", "www", "mail", "email",
    "service", "gruppe", "group", "team", "kontakt", "contact", "info", "support",
    "official", "office", "company", "firma", "gmbh", "ug", "ag", "kg", "ohg", "ev",
    "consulting", "solutions", "digital", "media", "agentur", "agency", "systems", "systeme",
    "technik", "technics", "tec", "tech", "handel", "logistik", "logistics", "holding",
    "hotel", "restaurant", "praxis", "kanzlei", "rechtsanwalt", "anwalt", "sachverstaendiger",
    # Navigation/Cookie/Template-Wörter dürfen niemals Brand-Tokens werden.
    "impressum", "startseite", "home", "cookie", "cookies", "datenschutz", "privacy",
    "kontakt", "anfahrt", "seite", "seiten", "mehr", "weiter", "zurueck", "zurück",
    "menu", "menue", "menü", "navigation", "nav", "footer", "header", "content",
    "willkommen", "angebot", "angebote", "leistungen", "leistung", "produkte", "produkt",
    "verkauf", "kaufen", "beratung", "termin", "oeffnungszeiten", "öffnungszeiten",
    "telefon", "phone", "fax", "adresse", "address", "standort", "sitemap",
    "rechtliches", "legal", "agb", "widerruf", "newsletter", "captcha", "cloudflare",
}


# =========================
# Email helpers
# =========================
def is_plausible_email(email: str) -> bool:
    email = (email or "").strip().lower()
    if len(email) < 6 or len(email) > 254:
        return False
    if email.count("@") != 1:
        return False

    local, domain = email.split("@", 1)
    if not (1 <= len(local) <= 64):
        return False

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*", local):
        return False

    if ".." in local:
        return False
    if not any(c.isalpha() for c in local):
        return False

    if "." not in domain or ".." in domain:
        return False
    if domain.startswith("-") or domain.endswith("-"):
        return False

    tld = domain.rsplit(".", 1)[-1].lower()
    if tld not in ALLOWED_TLDS:
        return False

    alnum = sum(ch.isalnum() for ch in local)
    if alnum / max(1, len(local)) < 0.6:
        return False

    return True


def _deobfuscate_emailish_text(text: str) -> str:
    if not text:
        return ""
    t = text

    t = html.unescape(t)
    t = urllib.parse.unquote(t)

    try:
        # Manche Baukästen/JS-Snippets schreiben Mails als info\u0040domain.de.
        t = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda m: chr(int(m.group(1), 16)),
            t,
        )
        t = re.sub(
            r"\\x([0-9a-fA-F]{2})",
            lambda m: chr(int(m.group(1), 16)),
            t,
        )
    except Exception:
        pass

    replacements = {
        "(at)": "@", "[at]": "@", "{at}": "@", "[ät]": "@", "(ät)": "@",
        "(dot)": ".", "[dot]": ".", "{dot}": ".", "(punkt)": ".", "[punkt]": ".",
        " (at) ": "@", " (dot) ": ".", " (punkt) ": ".",
    }
    for k, v in replacements.items():
        t = re.sub(re.escape(k), v, t, flags=re.IGNORECASE)

    t = re.sub(r"\s+(at|ät|bei|beim)\s+", "@", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+(dot|punkt)\s+", ".", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*@\s*", "@", t)
    t = re.sub(r"(?<=\w)\s*\.\s*(?=\w)", ".", t)

    return t


def extract_emails(text: str) -> set[str]:
    if not text:
        return set()

    text = _deobfuscate_emailish_text(text)
    found = set()

    for e in EMAIL_RE.findall(text):
        e = (e or "").strip().lower()
        if not is_plausible_email(e):
            continue
        found.add(e)

    return found


def decode_cloudflare_email(hex_string: str) -> str:
    """Dekodiert Cloudflare Email-Protection Werte aus data-cfemail bzw. /email-protection#..."""
    hx = re.sub(r"[^0-9a-fA-F]", "", hex_string or "")
    if len(hx) < 4 or len(hx) % 2 != 0:
        return ""
    try:
        key = int(hx[:2], 16)
        chars = [chr(int(hx[i:i + 2], 16) ^ key) for i in range(2, len(hx), 2)]
        email = "".join(chars).strip().lower()
        return email if is_plausible_email(email) else ""
    except Exception:
        return ""


def extract_cloudflare_protected_emails(text: str) -> set[str]:
    if not text:
        return set()
    found = set()
    patterns = [
        r'data-cfemail=["\']([0-9a-fA-F]+)["\']',
        r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)',
        r'email-protection#([0-9a-fA-F]+)',
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            decoded = decode_cloudflare_email(m.group(1))
            if decoded:
                found.add(decoded)
    return found


def extract_all_emails_from_text(text: str) -> set[str]:
    return extract_emails(text) | extract_cloudflare_protected_emails(text)


# =========================
# URL helpers
# =========================
def normalize_start_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("Leere URL.")
    p = urllib.parse.urlparse(url)
    if not p.scheme:
        url = "https://" + url.lstrip("/")
    return url


def netloc_of(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def hostname_of(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def same_domain(url: str, netloc: str) -> bool:
    try:
        return urllib.parse.urlparse(url).netloc.lower() == netloc.lower()
    except Exception:
        return False


def absolutize(base_url: str, href: str) -> str:
    return urllib.parse.urljoin(base_url, href)


def defrag(url: str) -> str:
    return urllib.parse.urldefrag(url).url


def canonicalize_url(url: str) -> str:
    url = defrag(url)
    p = urllib.parse.urlparse(url)
    scheme = (p.scheme or "https").lower()
    netloc = (p.netloc or "").lower()

    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = p.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    query = p.query or ""
    return urllib.parse.urlunparse((scheme, netloc, path, "", query, ""))


def score_url(url: str) -> int:
    u = url.lower()
    score = 0
    for k in PRIORITY_KEYWORDS:
        if k in u:
            score += 10
    score -= u.count("/")
    return score


def should_skip_url(url: str) -> bool:
    low = (url or "").lower()
    return any(s in low for s in SKIP_URL_SUBSTRINGS)


def is_denied_external_reg_domain(reg_domain: str) -> bool:
    reg_domain = (reg_domain or "").lower().strip(".")
    if not reg_domain:
        return True
    return any(reg_domain == denied or reg_domain.endswith("." + denied) for denied in EXTERNAL_DOMAIN_DENYLIST)


def looks_contact_relevant(url: str, text: str = "") -> bool:
    hay = ((url or "") + " " + (text or "")).lower()
    return any(k in hay for k in PRIORITY_KEYWORDS)


# =========================
# Domain matching + alias domains
# =========================
try:
    import tldextract
    _tld = tldextract.TLDExtract(suffix_list_urls=None)
except Exception:
    _tld = None


def registrable_domain_from_host(host: str) -> str:
    host = (host or "").lower().strip(".")
    if not host:
        return ""

    if _tld:
        ext = _tld(host)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}".lower()
        return host

    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def site_reg_domain_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return registrable_domain_from_host(host)


def email_domain(email: str) -> str:
    return (email.split("@", 1)[1] if "@" in email else "").lower().strip()


def email_local_part(email: str) -> str:
    return (email.split("@", 1)[0] if "@" in email else "").lower().strip()


def domain_label(reg_domain: str) -> str:
    return reg_domain.split(".", 1)[0].lower() if reg_domain else ""


def normalize_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def split_brand_parts(text: str) -> list[str]:
    return [p for p in re.split(r"[-_.]+", (text or "").lower()) if p]


def is_generic_provider_domain(reg_domain: str) -> bool:
    return reg_domain in GENERIC_EMAIL_PROVIDERS


def is_domain_allowed(candidate: str, primary: str) -> bool:
    candidate = (candidate or "").lower().strip().strip(".")
    primary = (primary or "").lower().strip().strip(".")

    if not candidate or not primary:
        return False

    return candidate == primary or candidate.endswith("." + primary)


def is_safe_alias_reg_domain(candidate: str, primary: str) -> bool:
    candidate = (candidate or "").lower().strip().strip(".")
    primary = (primary or "").lower().strip().strip(".")

    if not candidate or not primary:
        return False

    if is_domain_allowed(candidate, primary):
        return True
    
    return False


def build_brand_tokens(site_reg_domain: str) -> set[str]:
    label = domain_label(site_reg_domain)
    if not label:
        return set()

    raw_parts = split_brand_parts(label)
    tokens = set()

    compact = normalize_token(label)
    if len(compact) >= 4:
        tokens.add(compact)

    # Häufige Firmen-Domains hängen eine Orts-/Initial-Endung an: hettingerv -> hettinger.
    # Das ist bewusst konservativ: nur bei längeren Labels und nur als Zusatz-Token.
    if len(compact) >= 7 and compact[-1].isalpha():
        tokens.add(compact[:-1])

    if raw_parts:
        hyphen_joined = "-".join(raw_parts)
        if len(normalize_token(hyphen_joined)) >= 4:
            tokens.add(normalize_token(hyphen_joined))

    for part in raw_parts:
        np = normalize_token(part)
        if len(np) >= 4 and np not in GENERIC_BRAND_TOKENS:
            tokens.add(np)

    return {t for t in tokens if t}


def contains_brand_token(haystack: str, brand_tokens: set[str]) -> bool:
    hs = normalize_token(haystack)
    if not hs:
        return False
    return any(tok in hs for tok in brand_tokens if tok)




def build_context_brand_tokens_from_soup(soup: BeautifulSoup) -> set[str]:
    """
    Ergänzt Brand-Tokens nur aus echten Identitätsbereichen der Seite.
    Navigation, Cookie-Banner, Buttons und Footer-Texte werden bewusst ignoriert,
    damit Wörter wie impressum/startseite/cookie nicht als Marke auftauchen.
    """
    if soup is None:
        return set()

    pieces: list[str] = []

    selectors = [
        "meta[property='og:site_name']",
        "meta[name='application-name']",
        "meta[name='apple-mobile-web-app-title']",
        "meta[property='og:title']",
    ]
    for sel in selectors:
        for tag in soup.select(sel):
            val = tag.get("content") or ""
            if val:
                pieces.append(val)

    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(" ", strip=True)
        # Titel nach üblichen Trennern zerlegen; nur der Marken-/Seitenteil bleibt relevant.
        pieces.extend([part.strip() for part in re.split(r"[|–—-]", title) if part.strip()][:3])

    for tag in soup.select("header h1, main h1, .logo, .brand, .site-title, [class*='logo'], [class*='brand']")[:12]:
        val = tag.get("alt") or tag.get("title") or tag.get("aria-label") or tag.get_text(" ", strip=True)
        if val:
            pieces.append(val)

    tokens = set()
    for piece in pieces[:20]:
        raw_parts = re.split(r"[^A-Za-z0-9ÄÖÜäöüß]+", piece)
        parts = []
        for raw in raw_parts:
            p = normalize_token(raw)
            if len(p) < 4:
                continue
            if p in GENERIC_BRAND_TOKENS:
                continue
            if p.isdigit():
                continue
            parts.append(p)
            tokens.add(p)

        # Nur direkte Zweiwort-Kombinationen aus denselben Identitäts-Pieces.
        for i in range(len(parts) - 1):
            joined = parts[i] + parts[i + 1]
            if len(joined) >= 7 and joined not in GENERIC_BRAND_TOKENS:
                tokens.add(joined)

    return {t for t in tokens if t and t not in GENERIC_BRAND_TOKENS}


def is_external_role_email(email: str) -> bool:
    email = (email or "").lower().strip()
    if not email or "@" not in email:
        return False

    local = email_local_part(email)
    reg_dom = registrable_domain_from_host(email_domain(email))

    local_norm = normalize_token(local)
    dom_norm = normalize_token(domain_label(reg_dom))

    for kw in EXTERNAL_ROLE_KEYWORDS:
        nkw = normalize_token(kw)
        if nkw and (nkw in local_norm or nkw in dom_norm):
            return True
    return False


def email_match_kind_against_site(email: str, site_reg_domain: str, brand_tokens: set[str] | None = None) -> str:
    """
    Gibt die Art des Firmenmatches zurück:
    - company_domain: direkte Firmen-/Subdomain oder starke Markenübereinstimmung in der E-Mail-Domain
    - freemailer: bekannter Freemail-Provider + starker Markenbestandteil im Local-Part
    - none: kein plausibler Firmenmatch
    """
    e_dom = email_domain(email)
    if not e_dom:
        return "none"

    e_reg = registrable_domain_from_host(e_dom)
    local = email_local_part(email)
    brand_tokens = brand_tokens or set()

    if e_dom == site_reg_domain or e_dom.endswith("." + site_reg_domain) or e_reg == site_reg_domain:
        return "company_domain"

    if brand_tokens and contains_brand_token(domain_label(e_reg), brand_tokens):
        return "company_domain"

    # Wichtig für Fälle wie abvhettinger@aol.com, ffn-partner@fachverband-fliesen.de
    # oder agentur.rakete@axa.de: Die externe Domain allein passt nicht, aber der
    # Local-Part enthält eindeutig den Marken-/Firmennamen der geprüften Website.
    if brand_tokens and contains_brand_token(local, brand_tokens):
        if is_generic_provider_domain(e_reg):
            return "freemailer"
        return "company_domain"

    return "none"


def email_matches_site_strict_or_brand(email: str, site_reg_domain: str, brand_tokens: set[str] | None = None) -> bool:
    return email_match_kind_against_site(email, site_reg_domain, brand_tokens) != "none"

def extract_associated_reg_domains(base_url: str, html: str) -> set[str]:
    """
    Sammelt nur wirklich sichere Domain-Hinweise:
    - canonical
    - og:url
    - alternate / hreflang

    Normale <a href>-Links werden absichtlich NICHT berücksichtigt.
    """
    primary = site_reg_domain_from_url(base_url)
    assoc = {primary}

    if not html:
        return assoc

    soup = BeautifulSoup(html, "html.parser")

    def maybe_add(url_value: str) -> None:
        if not url_value:
            return
        abs_url = absolutize(base_url, url_value)
        rd = site_reg_domain_from_url(abs_url)
        if rd and is_safe_alias_reg_domain(rd, primary):
            assoc.add(rd)

    canon = soup.find("link", rel=lambda v: isinstance(v, str) and "canonical" in v.lower())
    if canon and canon.get("href"):
        maybe_add(canon["href"])

    og = soup.find("meta", property="og:url")
    if og and og.get("content"):
        maybe_add(og["content"])

    for tag in soup.find_all("link", rel=True, href=True):
        rel = tag.get("rel")
        if not rel:
            continue

        rel_values = [str(x).lower() for x in rel] if isinstance(rel, list) else [str(rel).lower()]
        if "alternate" in rel_values:
            maybe_add(tag.get("href"))

    return {d for d in assoc if d}


# =========================
# HTTP helpers
# =========================
def maybe_decompress_content(content: bytes, content_encoding: str) -> bytes:
    if not content:
        return b""

    enc = (content_encoding or "").lower().strip()
    try:
        if "gzip" in enc:
            return gzip.decompress(content)
        if "deflate" in enc:
            try:
                return zlib.decompress(content)
            except Exception:
                return zlib.decompress(content, -zlib.MAX_WBITS)
        if "br" in enc:
            try:
                import brotli
                return brotli.decompress(content)
            except Exception:
                return content
    except Exception:
        return content

    return content


def decode_response_bytes(content: bytes, response=None, content_encoding: str = "") -> str:
    if not content:
        return ""

    raw = maybe_decompress_content(content, content_encoding)

    encodings = []
    if response is not None:
        if getattr(response, "encoding", None):
            encodings.append(response.encoding)
        if getattr(response, "apparent_encoding", None):
            encodings.append(response.apparent_encoding)

    encodings.extend(["utf-8", "cp1252", "latin-1"])

    seen = set()
    for enc in encodings:
        enc = (enc or "").strip()
        if not enc or enc.lower() in seen:
            continue
        seen.add(enc.lower())
        try:
            return raw.decode(enc, errors="ignore")
        except Exception:
            pass

    return raw.decode("utf-8", errors="ignore")


def text_looks_decoded(text: str) -> bool:
    if not text:
        return False

    sample = text[:4000]
    printable = sum(1 for ch in sample if ch.isprintable() or ch in "")
    ratio = printable / max(1, len(sample))
    if ratio < 0.85:
        return False

    low = sample.lower()
    if "<html" in low or "<!doctype html" in low or "<body" in low or "<head" in low:
        return True

    if any(marker in low for marker in ["kontakt", "impressum", "telefon", "e-mail", "email", "datenschutz"]):
        return True

    return ratio >= 0.97


def fetch_via_urllib(url: str, timeout: int, verify: bool, headers: dict, debug: bool):
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()

    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        final_url = resp.geturl()
        status = getattr(resp, "status", 200) or 200
        content_type = resp.headers.get("Content-Type") or ""
        content = resp.read() or b""
        text = decode_response_bytes(content, response=None, content_encoding=resp.headers.get("Content-Encoding") or "")

        if debug:
            snip = text[:180].replace("\n", " ").replace("\r", " ")
            print("\nFETCH(URLLIB)", url)
            print("STATUS", status)
            print("FINAL", final_url)
            print("CT", content_type)
            print("SSL_VERIFY", verify)
            print("SNIP", snip)

        return status, final_url, content_type, text


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)

    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.com/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return s


def fetch(session: requests.Session, url: str, timeout: int, verify_default: bool, ssl_fallback: bool, debug: bool):
    def requests_try(request_url: str, verify_value: bool, encoding_header: str | None = None):
        headers = {}
        if encoding_header:
            headers["Accept-Encoding"] = encoding_header

        r = session.get(
            request_url,
            timeout=(10, timeout),
            allow_redirects=True,
            verify=verify_value,
            headers=headers or None,
        )
        content_encoding = r.headers.get("Content-Encoding") or ""
        text = decode_response_bytes(r.content or b"", response=r, content_encoding=content_encoding)

        if debug:
            snip = text[:180].replace("\n", " ").replace("\r", " ")
            mode = "FETCH" if not encoding_header else f"FETCH({encoding_header})"
            print(f"\n{mode}", request_url)
            print("STATUS", r.status_code)
            print("FINAL", r.url)
            print("CT", r.headers.get("Content-Type"))
            print("CE", content_encoding)
            print("SSL_VERIFY", verify_value)
            print("TEXT_OK", text_looks_decoded(text))
            print("SNIP", snip)

        return r.status_code, r.url, r.headers.get("Content-Type") or "", text, content_encoding

    try:
        status, final_url, ct, text, ce = requests_try(url, verify_default)
        if status and text_looks_decoded(text):
            return status, final_url, ct, text, verify_default, False

        status2, final_url2, ct2, text2, ce2 = requests_try(url, verify_default, encoding_header="identity")
        if status2 and (text_looks_decoded(text2) or looks_like_html(ct2, text2)):
            return status2, final_url2, ct2, text2, verify_default, False

        if status:
            return status, final_url, ct, text, verify_default, False

    except SSLError as e:
        if debug:
            print("ERR", url, "ssl error:", repr(e))
    except requests.RequestException as e:
        if debug:
            print("ERR", url, repr(e))

    if verify_default and ssl_fallback:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

        try:
            status, final_url, ct, text, ce = requests_try(url, False)
            if status and text_looks_decoded(text):
                return status, final_url, ct, text, False, True

            status2, final_url2, ct2, text2, ce2 = requests_try(url, False, encoding_header="identity")
            if status2 and (text_looks_decoded(text2) or looks_like_html(ct2, text2)):
                return status2, final_url2, ct2, text2, False, True

            if status:
                return status, final_url, ct, text, False, True

        except requests.RequestException as e2:
            if debug:
                print("ERR", url, "ssl-fallback requests failed:", repr(e2))

    try:
        status, final_url, ct, text = fetch_via_urllib(url, timeout, verify_default, dict(session.headers), debug)
        if status:
            return status, final_url, ct, text, verify_default, False
    except Exception as e3:
        if debug:
            print("ERR", url, "urllib failed:", repr(e3))

    if verify_default and ssl_fallback:
        try:
            status, final_url, ct, text = fetch_via_urllib(url, timeout, False, dict(session.headers), debug)
            if status:
                return status, final_url, ct, text, False, True
        except Exception as e4:
            if debug:
                print("ERR", url, "urllib ssl-fallback failed:", repr(e4))

    return 0, url, "", "", verify_default, False


def looks_like_html(content_type: str, text: str) -> bool:
    ct = (content_type or "").lower()
    if "text/html" in ct:
        return True
    t = (text or "").lower()
    return ("<html" in t) or ("<!doctype html" in t) or ("<body" in t) or ("<head" in t)


def alternate_start_variants(start_url: str) -> list[str]:
    start_url = normalize_start_url(start_url)
    p = urllib.parse.urlparse(start_url)
    host = (p.hostname or "").lower()
    path = p.path or "/"
    query = f"?{p.query}" if p.query else ""

    host_variants = [host]
    if host.startswith("www."):
        host_variants.append(host[4:])
    else:
        host_variants.append("www." + host)

    scheme_variants = []
    if (p.scheme or "https").lower() == "https":
        scheme_variants = ["https", "http"]
    else:
        scheme_variants = ["http", "https"]

    out = []
    seen = set()
    for scheme in scheme_variants:
        for hv in host_variants:
            if not hv:
                continue
            candidate = canonicalize_url(f"{scheme}://{hv}{path}{query}")
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


# =========================
# Main crawler
# =========================
def crawl_domain_for_emails(
    start_url: str,
    max_pages: int = 200,
    max_assets: int = 200,
    timeout: int = 20,
    insecure: bool = False,
    ssl_fallback: bool = True,
    debug: bool = False,
    max_sources_per_email: int = 1,
    alias_domains: bool = True,
    promote_email_domain_threshold: int = 2,
    early_block_attempts: int = 5,
    early_block_hits: int = 2,
    crawl_external_contact_domains: bool = True,
    max_external_contact_domains: int = 8,
) -> dict:
    start_url = normalize_start_url(start_url)
    primary_domain = netloc_of(start_url)
    primary_reg_domain = site_reg_domain_from_url(start_url)
    primary_brand_tokens = build_brand_tokens(primary_reg_domain)

    session = make_session()

    verify_default = not insecure
    if insecure:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    all_emails: set[str] = set()
    matching_emails: set[str] = set()
    matching_company_domain_emails: set[str] = set()
    matching_freemailer_emails: set[str] = set()
    other_emails: set[str] = set()
    external_role_emails: set[str] = set()

    email_sources: dict[str, list[str]] = defaultdict(list)
    email_reg_domain_counts: dict[str, int] = defaultdict(int)

    associated_reg_domains: set[str] = {primary_reg_domain}
    allowed_reg_domains: set[str] = {primary_reg_domain}
    allowed_netlocs: set[str] = set()
    external_contact_reg_domains: set[str] = set()
    brand_tokens_by_reg_domain: dict[str, set[str]] = defaultdict(set)
    brand_tokens_by_reg_domain[primary_reg_domain].update(primary_brand_tokens)

    ssl_fallback_used = 0

    blocked_hits = 0
    attempts = 0
    blocked_early = False
    blocked_examples = []

    def remember_source(email: str, page_url: str) -> None:
        if max_sources_per_email <= 0:
            return
        srcs = email_sources[email]
        if page_url in srcs:
            return
        if len(srcs) < max_sources_per_email:
            srcs.append(page_url)

    def active_brand_tokens() -> set[str]:
        tokens = set(primary_brand_tokens)
        for reg in associated_reg_domains:
            tokens.update(brand_tokens_by_reg_domain.get(reg, set()))
        for reg in allowed_reg_domains:
            tokens.update(brand_tokens_by_reg_domain.get(reg, set()))
        return {t for t in tokens if t}

    def maybe_promote_email_domain(email: str) -> None:
        e_dom = email_domain(email)
        if not e_dom:
            return

        e_reg = registrable_domain_from_host(e_dom)
        if not e_reg or is_generic_provider_domain(e_reg):
            return

        email_reg_domain_counts[e_reg] += 1

        if alias_domains and promote_email_domain_threshold > 0:
            if email_reg_domain_counts[e_reg] >= promote_email_domain_threshold:
                if contains_brand_token(domain_label(e_reg), active_brand_tokens()):
                    associated_reg_domains.add(e_reg)
                    brand_tokens_by_reg_domain[e_reg].update(build_brand_tokens(e_reg))

    def email_match_kind(email: str) -> str:
        e_dom = email_domain(email)
        if not e_dom:
            return "none"

        e_reg = registrable_domain_from_host(e_dom)

        # Redirect-/Alias-Domains zählen als echte Firmen-Domain, solange sie kein Freemailer sind.
        if alias_domains and not is_generic_provider_domain(e_reg):
            if any(is_domain_allowed(e_reg, allowed) for allowed in allowed_reg_domains):
                return "company_domain"

        return email_match_kind_against_site(email, primary_reg_domain, brand_tokens=active_brand_tokens())

    def is_domain_passend(email: str) -> bool:
        return email_match_kind(email) != "none"

    visited_pages: set[str] = set()
    visited_assets: set[str] = set()

    page_q = deque()
    asset_q = deque()

    def is_allowed_reg_domain(reg: str) -> bool:
        reg = (reg or "").lower().strip().strip(".")
        if not reg:
            return False
        return any(is_domain_allowed(reg, allowed) for allowed in allowed_reg_domains)

    def is_allowed_source_url(url: str) -> bool:
        try:
            host = urllib.parse.urlparse(url).hostname or ""
            reg = registrable_domain_from_host(host)
            return is_allowed_reg_domain(reg)
        except Exception:
            return False

    def is_allowed_netloc(url: str) -> bool:
        try:
            n = urllib.parse.urlparse(url).netloc.lower()
            host = urllib.parse.urlparse(url).hostname or ""
            reg = registrable_domain_from_host(host)
            return n in allowed_netlocs and is_allowed_reg_domain(reg)
        except Exception:
            return False

    def maybe_allow_external_contact_url(target_url: str, link_text: str = "") -> bool:
        if not crawl_external_contact_domains:
            return False
        try:
            p = urllib.parse.urlparse(target_url)
            host = p.hostname or ""
            netloc = p.netloc.lower()
            reg = registrable_domain_from_host(host)
        except Exception:
            return False

        if not reg or is_allowed_reg_domain(reg) or is_denied_external_reg_domain(reg):
            return False
        if len(external_contact_reg_domains) >= max_external_contact_domains:
            return False

        domain_has_brand = contains_brand_token(domain_label(reg), active_brand_tokens())

        # Streng: Externe Domains werden nur gecrawlt, wenn die externe Domain selbst
        # ein starkes Brand-Token enthält. Kontakt-/Impressum-Links allein reichen nicht.
        # Beispiel erlaubt: heizung-sanitaer-stadler.de -> stadler.works.
        # Beispiel nicht erlaubt: Cloudflare, Google, Social/CDN/Tracking-Domains.
        if not domain_has_brand:
            return False

        allowed_reg_domains.add(reg)
        allowed_netlocs.add(netloc)
        associated_reg_domains.add(reg)
        external_contact_reg_domains.add(reg)
        brand_tokens_by_reg_domain[reg].update(build_brand_tokens(reg))
        return True

    def enqueue_page(u: str) -> None:
        u = canonicalize_url(u)
        if not u:
            return
        if should_skip_url(u):
            return

        p = urllib.parse.urlparse(u)
        if p.scheme not in ("http", "https"):
            return
        if not is_allowed_netloc(u):
            return
        if u in visited_pages:
            return
        if len(visited_pages) + len(page_q) < max_pages * 6:
            page_q.append(u)

    def enqueue_asset(u: str) -> None:
        u = canonicalize_url(u)
        if not u:
            return

        p = urllib.parse.urlparse(u)
        if p.scheme not in ("http", "https"):
            return
        if not is_allowed_netloc(u):
            return
        if u in visited_assets:
            return

        low = u.lower()
        if low.endswith(".js") or low.endswith(".css") or "/_nuxt/" in low or "/assets/" in low:
            if len(visited_assets) + len(asset_q) < max_assets * 6:
                asset_q.append(u)

    def classify_email(email: str) -> str:
        mk = email_match_kind(email)
        if mk == "company_domain":
            return "matching_company_domain"
        if mk == "freemailer":
            return "matching_freemailer"
        if is_external_role_email(email):
            return "external_role"
        return "other"

    def ingest_emails(found: set[str], page_url: str) -> None:
        if not is_allowed_source_url(page_url):
            return

        for e in found:
            all_emails.add(e)
            remember_source(e, page_url)
            maybe_promote_email_domain(e)

            cls = classify_email(e)
            if cls == "matching_company_domain":
                matching_emails.add(e)
                matching_company_domain_emails.add(e)
                matching_freemailer_emails.discard(e)
                other_emails.discard(e)
                external_role_emails.discard(e)
            elif cls == "matching_freemailer":
                matching_emails.add(e)
                matching_freemailer_emails.add(e)
                matching_company_domain_emails.discard(e)
                other_emails.discard(e)
                external_role_emails.discard(e)
            elif cls == "external_role":
                if e not in matching_emails:
                    external_role_emails.add(e)
                    other_emails.discard(e)
            else:
                if e not in matching_emails and e not in external_role_emails:
                    other_emails.add(e)

    start_variants = alternate_start_variants(start_url)

    seed_seen = set()
    seeded_common_paths_for_netlocs: set[str] = set()

    for variant in start_variants:
        cu = canonicalize_url(variant)
        if cu not in seed_seen and not should_skip_url(cu):
            seed_seen.add(cu)
            page_q.append(cu)

    while page_q and len(visited_pages) < max_pages:
        url = page_q.popleft()
        if url in visited_pages:
            continue

        visited_pages.add(url)

        status, final_url, ct, text, used_verify, did_ssl_fallback = fetch(
            session, url, timeout, verify_default, ssl_fallback, debug
        )
        if did_ssl_fallback:
            ssl_fallback_used += 1

        attempts += 1

        if status in BLOCK_STATUSES:
            blocked_hits += 1
            if len(blocked_examples) < 3:
                blocked_examples.append({
                    "url": url,
                    "status": status,
                    "content_type": ct
                })

            if attempts <= early_block_attempts and blocked_hits >= early_block_hits:
                blocked_early = True
                break
            continue

        if status == 0 or status >= 400:
            continue

        final_c = canonicalize_url(final_url)
        final_reg_domain = site_reg_domain_from_url(final_c)
        request_reg_domain = site_reg_domain_from_url(url)

        if request_reg_domain == primary_reg_domain and final_reg_domain and not is_denied_external_reg_domain(final_reg_domain):
            allowed_reg_domains.add(final_reg_domain)
            brand_tokens_by_reg_domain[final_reg_domain].update(build_brand_tokens(final_reg_domain))

        if not is_allowed_reg_domain(final_reg_domain):
            continue

        if is_denied_external_reg_domain(final_reg_domain):
            continue

        allowed_reg_domains.add(final_reg_domain)
        brand_tokens_by_reg_domain[final_reg_domain].update(build_brand_tokens(final_reg_domain))
        allowed_netlocs.add(netloc_of(final_c))
        associated_reg_domains.add(final_reg_domain)

        final_netloc = netloc_of(final_c)
        if final_netloc and final_netloc not in seeded_common_paths_for_netlocs:
            seeded_common_paths_for_netlocs.add(final_netloc)
            for pth in COMMON_PATHS:
                enqueue_page(urllib.parse.urljoin(final_c, pth))

        ingest_emails(extract_all_emails_from_text(text), final_c)

        if not looks_like_html(ct, text):
            continue

        if alias_domains:
            for assoc in extract_associated_reg_domains(final_c, text):
                associated_reg_domains.add(assoc)
                brand_tokens_by_reg_domain[assoc].update(build_brand_tokens(assoc))

        soup = BeautifulSoup(text, "html.parser")

        context_tokens = build_context_brand_tokens_from_soup(soup)
        if context_tokens:
            brand_tokens_by_reg_domain[final_reg_domain].update(context_tokens)

        for a in soup.select("a[href^='mailto:']"):
            href = (a.get("href") or "").strip()
            val = href.split(":", 1)[1].split("?", 1)[0].strip()
            if val:
                val = _deobfuscate_emailish_text(val).lower()
                if is_plausible_email(val):
                    ingest_emails({val}, final_c)

        visible_text = soup.get_text(" ", strip=True)
        ingest_emails(extract_all_emails_from_text(visible_text), final_c)

        for tag in soup.find_all(True):
            for attr_val in tag.attrs.values():
                if isinstance(attr_val, str):
                    ingest_emails(extract_all_emails_from_text(attr_val), final_c)
                elif isinstance(attr_val, list):
                    joined = " ".join(str(x) for x in attr_val if x is not None)
                    ingest_emails(extract_all_emails_from_text(joined), final_c)

        for sc in soup.select("script"):
            if sc.get("src"):
                continue
            script_text = sc.get_text(" ", strip=True)
            if script_text:
                ingest_emails(extract_all_emails_from_text(script_text), final_c)

        links = []
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href or href.startswith("#"):
                continue
            abs_link = absolutize(final_url, href)
            link_text = a.get_text(" ", strip=True) or ""
            maybe_allow_external_contact_url(abs_link, link_text)
            links.append(abs_link)

        for l in sorted(set(links), key=score_url, reverse=True):
            enqueue_page(l)

        for s in soup.select("script[src]"):
            src = (s.get("src") or "").strip()
            if src:
                enqueue_asset(absolutize(final_url, src))

        for lnk in soup.select("link[href]"):
            href = (lnk.get("href") or "").strip()
            if href:
                enqueue_asset(absolutize(final_url, href))

    if not blocked_early:
        while asset_q and len(visited_assets) < max_assets:
            asset_url = asset_q.popleft()
            if asset_url in visited_assets:
                continue

            visited_assets.add(asset_url)

            status, final_url, ct, text, used_verify, did_ssl_fallback = fetch(
                session, asset_url, timeout, verify_default, ssl_fallback, debug
            )
            if did_ssl_fallback:
                ssl_fallback_used += 1

            attempts += 1

            if status in BLOCK_STATUSES:
                blocked_hits += 1
                if len(blocked_examples) < 3:
                    blocked_examples.append({
                        "url": asset_url,
                        "status": status,
                        "content_type": ct
                    })
                continue

            if status == 0 or status >= 400:
                continue

            final_c = canonicalize_url(final_url)
            if not is_allowed_source_url(final_c):
                continue

            ingest_emails(extract_all_emails_from_text(text), final_c)

    matching_emails = set()
    matching_company_domain_emails = set()
    matching_freemailer_emails = set()
    other_emails = set()
    external_role_emails = set()
    email_classification = {}

    for e in all_emails:
        cls = classify_email(e)
        email_classification[e] = cls
        if cls == "matching_company_domain":
            matching_emails.add(e)
            matching_company_domain_emails.add(e)
        elif cls == "matching_freemailer":
            matching_emails.add(e)
            matching_freemailer_emails.add(e)
        elif cls == "external_role":
            external_role_emails.add(e)
        else:
            other_emails.add(e)

    sources_per_email = {e: email_sources.get(e, []) for e in sorted(all_emails)}

    return {
        "start_url": start_url,
        "domain": primary_domain,
        "registrable_domain": primary_reg_domain,
        "brand_tokens": sorted(active_brand_tokens()),
        "associated_reg_domains": sorted(associated_reg_domains),
        "allowed_reg_domains": sorted(allowed_reg_domains),
        "allowed_netlocs": sorted(allowed_netlocs),
        "external_contact_reg_domains": sorted(external_contact_reg_domains),

        "blocked": blocked_early,
        "blocked_hits": blocked_hits,
        "attempts": attempts,
        "blocked_examples": blocked_examples,
        "block_statuses": sorted(BLOCK_STATUSES),

        "pages_crawled": len(visited_pages),
        "assets_crawled": len(visited_assets),

        "emails_matching_domain": sorted(matching_emails),
        "emails_matching_company_domain": sorted(matching_company_domain_emails),
        "emails_matching_freemailer": sorted(matching_freemailer_emails),
        "emails_external_role": sorted(external_role_emails),
        "emails_other": sorted(other_emails),
        "emails_all": sorted(all_emails),
        "sources_per_email": sources_per_email,
        "email_classification": email_classification,

        "ssl_verify_default": verify_default,
        "ssl_fallback_enabled": ssl_fallback and (not insecure),
        "ssl_fallback_used_count": ssl_fallback_used,

        "max_sources_per_email": max_sources_per_email,
        "alias_domains": alias_domains,
        "promote_email_domain_threshold": promote_email_domain_threshold,
        "crawl_external_contact_domains": crawl_external_contact_domains,
        "max_external_contact_domains": max_external_contact_domains,

        "notes": {
            "email_rule_added": "local-part must start with [A-Za-z0-9] (no leading special chars)",
            "domain_rule": "Company-domain matches are exact/redirect-aware domains or strong brand tokens in the email domain. Freemailer matches are separated and only accepted when a known freemail provider is used and the local-part contains a strong brand token.",
            "external_role_rule": "Privacy / DPO / legal style emails stay in emails_all but are separated into emails_external_role when they are not company matches.",
            "blocked_logic": f"Early block if blocked_hits>={early_block_hits} within first {early_block_attempts} attempts.",
            "start_variants": "original + www/non-www + http/https fallback",
            "source_url_rule": "Emails are only ingested from responses whose final registrable domain is in the allowed redirect-aware domain set.",
            "fetch_fallbacks": "requests retry + Accept-Encoding identity retry + urllib fallback + optional SSL fallback + Cloudflare email-protection decoding",
            "external_contact_domain_rule": "Strict mode: only external domains whose registrable domain contains an active brand token are crawled; contact/impressum links alone are ignored.",
            "local_part_brand_rule": "Emails whose local part contains a strong brand token are treated as domain-passend; generic providers remain separated as freemailer."
        }
    }


def main():
    ap = argparse.ArgumentParser(description="Email crawler with strict same-site domain matching + brand-token matching + early block detection.")
    ap.add_argument("url", help="Start URL, z.B. https://example.com")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--max-assets", type=int, default=200)
    ap.add_argument("--timeout", type=int, default=20)

    ap.add_argument("--insecure", action="store_true", help="SSL verify global deaktivieren (nur wenn nötig)")
    ap.add_argument("--no-ssl-fallback", action="store_true", help="Kein SSL-Fallback (kein Retry mit verify=False)")

    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--max-sources-per-email", type=int, default=1)

    ap.add_argument("--no-alias-domains", action="store_true")
    ap.add_argument("--promote-email-domain-threshold", type=int, default=2)

    ap.add_argument("--early-block-attempts", type=int, default=5)
    ap.add_argument("--early-block-hits", type=int, default=2)
    ap.add_argument("--no-external-contact-domains", action="store_true", help="Keine begrenzten externen Kontakt-/Alias-Domains crawlen")
    ap.add_argument("--max-external-contact-domains", type=int, default=8)

    ap.add_argument("--out", default="", help="Optional: JSON Ausgabe-Datei")
    args = ap.parse_args()

    result = crawl_domain_for_emails(
        args.url,
        max_pages=args.max_pages,
        max_assets=args.max_assets,
        timeout=args.timeout,
        insecure=args.insecure,
        ssl_fallback=(not args.no_ssl_fallback),
        debug=args.debug,
        max_sources_per_email=args.max_sources_per_email,
        alias_domains=(not args.no_alias_domains),
        promote_email_domain_threshold=args.promote_email_domain_threshold,
        early_block_attempts=args.early_block_attempts,
        early_block_hits=args.early_block_hits,
        crawl_external_contact_domains=(not args.no_external_contact_domains),
        max_external_contact_domains=args.max_external_contact_domains,
    )

    if result["blocked"]:
        print("\nBLOCKED: Website blockt Crawler (Early-Exit).")
        print("Blocked hits:", result["blocked_hits"], "Attempts:", result["attempts"])
        print("Examples:")
        for ex in result["blocked_examples"]:
            print(" -", ex["status"], ex["url"])
    else:
        print("\nAllowed netlocs:")
        for n in result["allowed_netlocs"]:
            print(" -", n)

        print("\nAssociated registrable domains:")
        for d in result["associated_reg_domains"]:
            print(" -", d)

        print("\nBrand tokens:")
        for t in result["brand_tokens"]:
            print(" -", t)

        print("\nEmails (Domain-passend / Firmen-Domain):")
        for e in result["emails_matching_company_domain"]:
            print(" -", e)

        print("\nEmails (Domain-passend / Freemailer):")
        for e in result["emails_matching_freemailer"]:
            print(" -", e)

        print("\nEmails (Domain-passend / Gesamt):")
        for e in result["emails_matching_domain"]:
            print(" -", e)

        print("\nEmails (Externe Rollen / Datenschutz / Legal):")
        for e in result["emails_external_role"]:
            print(" -", e)

        print("\nEmails (andere Domains / extern):")
        for e in result["emails_other"]:
            print(" -", e)

    j = json.dumps(result, ensure_ascii=False, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(j)
        print(f"\nJSON gespeichert in: {args.out}")
    else:
        print("\nJSON Result:\n", j)


if __name__ == "__main__":
    main()
