"""
Translations for ePhoto Scan.

Source strings are English.  Translations live in `locale/<lang>.po` next to
this file and are read directly at startup — no msgfmt/.mo build step needed.
The language is chosen like gettext does: LANGUAGE, LC_ALL, LC_MESSAGES, LANG.

    from i18n import _, ngettext
    _("Scan")
    ngettext("{n} page", "{n} pages", n).format(n=n)
"""
import ast
import os
import re
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent / "locale"


def system_languages():
    """Preferred languages, most preferred first, e.g. ["de_CH", "de"]."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val:
            break
    else:
        return []
    out = []
    for item in val.split(":"):
        item = item.split(".")[0].split("@")[0]
        if not item or item in ("C", "POSIX"):
            continue
        for cand in (item, item.split("_")[0]):
            if cand not in out:
                out.append(cand)
    return out


def _unquote(s):
    return ast.literal_eval(s) if s else ""


def parse_po(path):
    """Minimal .po reader: {msgid: msgstr} and {msgid: [plural forms]} for plurals."""
    catalog, entry, key = {}, {}, None

    def flush():
        mid = entry.get("msgid")
        if mid:
            if "msgid_plural" in entry:
                forms = [entry[k] for k in sorted(k for k in entry if k.startswith("msgstr["))]
                if any(forms):
                    catalog[mid] = forms
            elif entry.get("msgstr"):
                catalog[mid] = entry["msgstr"]
        entry.clear()

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            if not line and entry:
                flush()
            continue
        m = re.match(r'(msgid_plural|msgid|msgstr(?:\[\d+\])?)\s+(".*")$', line)
        if m:
            if m.group(1) == "msgid" and entry:
                flush()
            key = m.group(1)
            entry[key] = _unquote(m.group(2))
        elif line.startswith('"') and key:
            entry[key] += _unquote(line)
    flush()
    return catalog


def _load():
    for lang in system_languages():
        if lang == "en" or lang.startswith("en_"):
            return lang, {}
        po = LOCALE_DIR / f"{lang}.po"
        if po.is_file():
            try:
                return lang, parse_po(po)
            except (OSError, ValueError, SyntaxError):
                pass
    return "en", {}


LANG, _CATALOG = _load()


def _(msg):
    t = _CATALOG.get(msg)
    return t if isinstance(t, str) else msg


def ngettext(singular, plural, n):
    forms = _CATALOG.get(singular)
    if isinstance(forms, list) and len(forms) >= 2:
        return forms[0] if n == 1 else forms[1]     # Germanic plural rule
    return singular if n == 1 else plural


def N_(msg):
    """Mark a string for translation without translating it yet."""
    return msg
