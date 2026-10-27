"""
İzmir Açık Veri Portalı'ndan referans verilerini (duraklar, hatlar, güzergahlar) indirir.

Bu veriler nadiren değişir; ayda bir kez indirmek yeterli.
Çıktı: data/ref/ klasörü + data/ref/kaynaklar.json (neyin nereden geldiği)

Kullanım: python referans_indir.py
"""

import json
import os
import re
from datetime import datetime, timezone

import requests

CKAN = "https://acikveri.bizizmir.com/api/3/action"
KLASOR = os.path.join("data", "ref")

# Doğrudan bildiğimiz veri seti kimlikleri
VERI_SETLERI = ["eshot-otobus-duraklari", "eshot-otobus-hat-listesi"]
# Kimliğini bilmediklerimiz için arama
ARAMALAR = ["Otobüs Hat Güzergahları"]

session = requests.Session()
session.headers["User-Agent"] = "eshot-duzenlilik-projesi/0.1 (egitim amacli)"


def ckan(eylem, **params):
    r = session.get(f"{CKAN}/{eylem}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()["result"]


def dosya_adi(paket, kaynak):
    ad = kaynak.get("name") or kaynak["id"]
    ad = re.sub(r"[^\w\-]+", "_", ad.lower()).strip("_")[:60]
    return f"{paket}__{ad}.csv"


def paketi_indir(paket_id, kayit):
    paket = ckan("package_show", id=paket_id)
    for k in paket["resources"]:
        if (k.get("format") or "").lower() != "csv":
            continue
        hedef = os.path.join(KLASOR, dosya_adi(paket["name"], k))
        r = session.get(k["url"], timeout=120)
        r.raise_for_status()
        with open(hedef, "wb") as f:
            f.write(r.content)
        boyut = len(r.content) / 1024
        print(f"  OK  {hedef}  ({boyut:,.0f} KB)")
        kayit.append({"veri_seti": paket["title"], "paket": paket["name"],
                      "kaynak": k.get("name"), "url": k["url"], "dosya": hedef,
                      "kb": round(boyut)})


def main():
    os.makedirs(KLASOR, exist_ok=True)
    kayit = []

    paketler = list(VERI_SETLERI)
    for sorgu in ARAMALAR:
        sonuc = ckan("package_search", q=sorgu, rows=5)["results"]
        print(f"Arama '{sorgu}': {[p['name'] for p in sonuc]}")
        if sonuc:
            paketler.append(sonuc[0]["name"])

    for p in paketler:
        print(f"Indiriliyor: {p}")
        try:
            paketi_indir(p, kayit)
        except Exception as e:  # bir veri seti başarısız olursa diğerlerine devam et
            print(f"  HATA {p}: {e}")

    with open(os.path.join(KLASOR, "kaynaklar.json"), "w", encoding="utf-8") as f:
        json.dump({"indirme_zamani_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "lisans": "Izmir Metropolitan Municipality Open Data License",
                   "dosyalar": kayit}, f, ensure_ascii=False, indent=2)
    print(f"\n{len(kayit)} dosya indirildi.")


if __name__ == "__main__":
    main()
