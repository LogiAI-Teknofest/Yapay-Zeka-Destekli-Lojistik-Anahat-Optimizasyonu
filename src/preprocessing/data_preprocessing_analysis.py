from __future__ import annotations
from pathlib import Path
import pandas as pd

# Proje Kök Dizini Tanımı (src/preprocessing/.. -> root)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

def read_raw_file(path: Path | str) -> pd.DataFrame:
    """Excel dosyalarını pandas ile okur."""
    return pd.read_excel(path)

def parse_decimal_number(series: pd.Series) -> pd.Series:
    """Türkçe/Avrupa formatındaki ondalık virgülleri temizleyip float sayıya çevirir."""
    def parse_val(val):
        if pd.isna(val):
            return val
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        if "," in s:
            if "." in s and s.find(".") < s.find(","):
                s = s.replace(".", "")
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return pd.NA
    return series.apply(parse_val)

def excel_serial_to_datetime(series: pd.Series) -> pd.Series:
    """Excel seri tarihlerini pandas datetime nesnesine dönüştürür."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series)
    def convert_val(val):
        if pd.isna(val):
            return pd.NaT
        try:
            n = float(val)
            return pd.to_datetime(n, unit='D', origin='1899-12-30')
        except (ValueError, TypeError):
            return pd.to_datetime(val, errors='coerce')
    return series.apply(convert_val)

def turkish_lower(s: str) -> str:
    """Türkçe karakter kurallarına uygun şekilde küçük harfe çevirir."""
    s = s.replace("İ", "i").replace("I", "ı")
    return s.lower()

def normalize_city_name(value: object) -> str:
    """Şehir adını normalize eder."""
    if pd.isna(value):
        return ""
    s = str(value).strip()
    s = " ".join(s.split())
    return turkish_lower(s)

def turkish_title(s: str) -> str:
    """Türkçe karakter kurallarına uygun şekilde kelimelerin ilk harfini büyütür."""
    words = s.split()
    title_words = []
    for w in words:
        if not w:
            continue
        first = w[0]
        rest = w[1:]
        if first == "i":
            first_upper = "İ"
        elif first == "ı":
            first_upper = "I"
        else:
            first_upper = first.upper()
        rest_lower = rest.replace("İ", "i").replace("I", "ı").lower()
        title_words.append(first_upper + rest_lower)
    return " ".join(title_words)

def title_city_name(value: str) -> str:
    """Normalize edilmiş şehir adını başlık formatına getirir."""
    return turkish_title(value)
