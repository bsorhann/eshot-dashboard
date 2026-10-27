"""
ESHOT canlı otobüs konumu toplayıcı.

İzmir Açık Veri API'sinden seçilen hatların otobüs konumlarını belirli aralıklarla
çeker ve zaman damgasıyla kaydeder. İki çıktı türü var:
  - SQLite veritabanı (varsayılan, kendi bilgisayarında çalıştırmak için)
  - CSV dosyası (--cikti ile; GitHub Actions bunu kullanır)

Kullanım:
    python collector.py                          # varsayılan hatlar, 60 sn arayla, SQLite
    python collector.py --hatlar 446 202         # belirli hatlar
    python collector.py --tek-sefer              # bir tur çek ve çık (test)
    python collector.py --cikti veri.csv --sure 55   # CSV'ye yaz, 55 dakika sonra çık

Durdurmak için: Ctrl+C
"""

import argparse
import csv
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests

API_URL = "https://openapi.izmir.bel.tr/api/iztek/hatotobuskonumlari/{hat_id}"

# İzlenecek hatlar. hat_bul.py ile hat numaralarını bulup buraya ekle.
VARSAYILAN_HATLAR = [532, 533, 534, 446, 304]

DB_YOLU = "otobus_konumlari.db"
ISTEK_ZAMAN_ASIMI = 20  # saniye
SUTUNLAR = ["zaman_utc", "hat_id", "otobus_id", "yon", "koor_x", "koor_y", "sira"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("toplayici")


# ---------------------------------------------------------------- çıktı türleri

class SqliteYazici:
    def __init__(self, yol: str):
        self.conn = sqlite3.connect(yol)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS konumlar (
                zaman_utc  TEXT    NOT NULL,   -- kaydın alındığı an (ISO 8601, UTC)
                hat_id     INTEGER NOT NULL,
                otobus_id  INTEGER NOT NULL,
                yon        INTEGER,
                koor_x     REAL,               -- enlem
                koor_y     REAL,               -- boylam
                sira       INTEGER             -- kaydın API yanıtındaki sırası
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_hat_zaman ON konumlar (hat_id, zaman_utc)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS hatalar "
            "(zaman_utc TEXT, hat_id INTEGER, durum_kodu INTEGER, mesaj TEXT)"
        )
        self.conn.commit()

    def yaz(self, satirlar, hatalar):
        self.conn.executemany("INSERT INTO konumlar VALUES (?, ?, ?, ?, ?, ?, ?)", satirlar)
        self.conn.executemany("INSERT INTO hatalar VALUES (?, ?, ?, ?)", hatalar)
        self.conn.commit()

    def kapat(self):
        self.conn.close()


class CsvYazici:
    """Her turda dosyanın sonuna ekler; iş yarıda kesilse bile o ana kadarki veri kalır."""

    def __init__(self, yol: str):
        self.yol = yol
        if not os.path.exists(yol):
            with open(yol, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(SUTUNLAR)

    def yaz(self, satirlar, hatalar):
        with open(self.yol, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(satirlar)
        # Hatalar Actions loglarında görünsün diye sadece log'a yazılır

    def kapat(self):
        pass


def veritabani_hazirla(yol: str) -> sqlite3.Connection:
    """Geriye dönük uyumluluk: kontrol/test betikleri için SQLite bağlantısı döndürür."""
    return SqliteYazici(yol).conn


# ---------------------------------------------------------------- API

def hat_konumlarini_cek(session: requests.Session, hat_id: int):
    """(durum_kodu, otobüs listesi, hata mesajı) döndürür."""
    try:
        r = session.get(API_URL.format(hat_id=hat_id), timeout=ISTEK_ZAMAN_ASIMI)
    except requests.RequestException as e:
        return None, [], f"Bağlantı hatası: {e}"

    if r.status_code == 204:  # o an hatta otobüs yok (ör. gece)
        return 204, [], None
    if r.status_code != 200:
        return r.status_code, [], r.text[:300]

    try:
        veri = r.json()
    except ValueError:
        return r.status_code, [], "JSON çözümlenemedi: " + r.text[:300]

    otobusler = veri.get("HatOtobusKonumlari") or []
    hata = veri.get("HataMesaj") or None
    return 200, otobusler, hata


def koordinat(deger):
    """'38,45338167' gibi virgüllü metni sayıya çevirir. 0 veya geçersizse None."""
    if deger is None:
        return None
    try:
        sayi = float(str(deger).replace(",", "."))
    except ValueError:
        return None
    return sayi if sayi != 0 else None  # 0,0 = GPS sinyali yok


def bir_tur(yazici, session: requests.Session, hatlar):
    zaman = datetime.now(timezone.utc).isoformat(timespec="seconds")
    satirlar, hatalar = [], []
    for hat_id in hatlar:
        kod, otobusler, hata = hat_konumlarini_cek(session, hat_id)
        if hata:
            hatalar.append((zaman, hat_id, kod, hata))
            log.warning("Hat %s: %s (kod %s)", hat_id, hata, kod)
        satirlar += [
            (
                zaman,
                hat_id,
                o.get("OtobusId"),
                o.get("Yon"),
                koordinat(o.get("KoorX")),
                koordinat(o.get("KoorY")),
                i,
            )
            for i, o in enumerate(otobusler)
            if o.get("OtobusId") is not None
        ]
        time.sleep(1)  # API'ye nazik olalım: hatlar arasında 1 sn bekle
    yazici.yaz(satirlar, hatalar)
    log.info("%d hat tarandı, %d otobüs konumu kaydedildi", len(hatlar), len(satirlar))


# ---------------------------------------------------------------- ana döngü

def main():
    p = argparse.ArgumentParser(description="ESHOT canlı otobüs konumu toplayıcı")
    p.add_argument("--hatlar", type=int, nargs="+", default=VARSAYILAN_HATLAR)
    p.add_argument("--aralik", type=int, default=60, help="Turlar arası saniye")
    p.add_argument("--db", default=DB_YOLU, help="SQLite dosyası (varsayılan çıktı)")
    p.add_argument("--cikti", help="SQLite yerine bu CSV dosyasına yaz")
    p.add_argument("--sure", type=float, help="Bu kadar dakika sonra dur")
    p.add_argument("--tek-sefer", action="store_true", help="Bir tur çek ve çık")
    args = p.parse_args()

    yazici = CsvYazici(args.cikti) if args.cikti else SqliteYazici(args.db)
    session = requests.Session()
    session.headers["User-Agent"] = "eshot-duzenlilik-projesi/0.1 (egitim amacli)"
    bitis = time.time() + args.sure * 60 if args.sure else None

    log.info("Başladı. Hatlar: %s | Aralık: %s sn | Çıktı: %s | Süre: %s dk",
             args.hatlar, args.aralik, args.cikti or args.db, args.sure or "sınırsız")
    try:
        while True:
            baslangic = time.time()
            bir_tur(yazici, session, args.hatlar)
            if args.tek_sefer:
                break
            bekle = max(0, args.aralik - (time.time() - baslangic))
            if bitis and time.time() + bekle >= bitis:
                log.info("Süre doldu.")
                break
            time.sleep(bekle)
    except KeyboardInterrupt:
        log.info("Durduruldu.")
    finally:
        yazici.kapat()


if __name__ == "__main__":
    main()
