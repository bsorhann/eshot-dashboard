"""
ESHOT hat numarasını (hatId) bulmak için yardımcı araç.

İzmir Açık Veri Portalı'ndaki "ESHOT Otobüs Hat Listesi" veri setini indirir
ve verdiğin kelimeyi hat adı / güzergah içinde arar.

Kullanım:
    python hat_bul.py bornova
    python hat_bul.py "evka 3"
    python hat_bul.py --hepsi           # tüm listeyi hatlar.csv olarak kaydet
"""

import argparse
import io
import sys

import pandas as pd
import requests

CKAN = "https://acikveri.bizizmir.com/api/3/action/package_show"
VERI_SETI = "eshot-otobus-hat-listesi"


def hat_listesini_indir() -> pd.DataFrame:
    meta = requests.get(CKAN, params={"id": VERI_SETI}, timeout=20).json()
    kaynaklar = meta["result"]["resources"]
    csv = next(
        (k for k in kaynaklar if (k.get("format") or "").lower() == "csv"),
        kaynaklar[0],
    )
    ham = requests.get(csv["url"], timeout=30).content

    for kodlama in ("utf-8-sig", "cp1254", "iso-8859-9"):
        try:
            return pd.read_csv(io.BytesIO(ham), sep=None, engine="python", encoding=kodlama)
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    sys.exit("Hat listesi okunamadı; dosyayı portaldan elle indirip kontrol et.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("aranan", nargs="?", help="Hat adı veya semt (ör. bornova)")
    p.add_argument("--hepsi", action="store_true")
    args = p.parse_args()

    df = hat_listesini_indir()
    print("Sütunlar:", list(df.columns), "\n")

    if args.hepsi or not args.aranan:
        df.to_csv("hatlar.csv", index=False)
        print(f"{len(df)} hat hatlar.csv dosyasına kaydedildi.")
        return

    metin = df.astype(str).agg(" ".join, axis=1).str.lower()
    sonuc = df[metin.str.contains(args.aranan.lower(), regex=False)]
    if sonuc.empty:
        print("Eşleşen hat bulunamadı.")
    else:
        print(sonuc.head(30).to_string(index=False))


if __name__ == "__main__":
    main()
