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
    python collector.py --tum-hatlar --paralel 10 --aralik 300 --cikti veri.csv
                                                     # 446 hattın tamamı, 5 dakikada bir

Durdurmak için: Ctrl+C
"""

import argparse
import csv
import glob
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

API_URL = "https://openapi.izmir.bel.tr/api/iztek/hatotobuskonumlari/{hat_id}"

# İzlenecek hatlar. hat_bul.py ile hat numaralarını bulup buraya ekle.
VARSAYILAN_HATLAR = [532, 533, 534, 446, 304]
HAT_LISTESI_DESENI = os.path.join("data", "ref", "eshot-otobus-hat-listesi__*.csv")

DB_YOLU = "otobus_konumlari.db"
# (bağlantı, okuma) saniye. API her saatin ~15. dakikasında birkaç dakika yanıt
# vermiyor; kısa zaman aşımıyla o turlarda dakikalarca takılı kalmıyoruz.
ISTEK_ZAMAN_ASIMI = (5, 10)
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


class DonenCsvYazici:
    """
    Saatlik dosyalar: klasor/<HHMMSS>.csv'ye yazar; bir saat dolunca dosyayı gzip'leyip
    klasor/hazir/<YYYY-MM-DD>__<HHMMSS>.csv.gz olarak bırakır (iş akışı oradan yükler).
    Böylece toplayıcı tüm iş boyunca kesintisiz çalışır, öğrendiği hızı ve hat durumlarını korur.
    """

    def __init__(self, klasor, sure_sn=3600):
        import gzip
        import shutil as _shutil
        self._gzip, self._shutil = gzip, _shutil
        self.klasor, self.sure_sn = klasor, sure_sn
        os.makedirs(os.path.join(klasor, "hazir"), exist_ok=True)
        self._ac()

    def _ac(self):
        simdi = datetime.now(timezone.utc)
        self.baslangic = time.time()
        self.ad = f"{simdi:%Y-%m-%d}__{simdi:%H%M%S}"
        self.yol = os.path.join(self.klasor, f"{simdi:%H%M%S}.csv")
        with open(self.yol, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(SUTUNLAR)

    def _kapat_dosya(self):
        hedef = os.path.join(self.klasor, "hazir", self.ad + ".csv.gz")
        gecici = hedef + ".yaziliyor"
        with open(self.yol, "rb") as kaynak, self._gzip.open(gecici, "wb") as hedef_f:
            self._shutil.copyfileobj(kaynak, hedef_f)
        os.replace(gecici, hedef)  # yükleyici yarım dosya görmesin
        os.remove(self.yol)

    def yaz(self, satirlar, hatalar):
        with open(self.yol, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(satirlar)
        if time.time() - self.baslangic >= self.sure_sn:
            self._kapat_dosya()
            self._ac()

    def kapat(self):
        self._kapat_dosya()


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


_yerel = threading.local()


def _oturum():
    """Her iş parçacığına kendi HTTP oturumu."""
    if not hasattr(_yerel, "s"):
        _yerel.s = requests.Session()
        _yerel.s.headers["User-Agent"] = "eshot-duzenlilik-projesi/0.2 (egitim amacli)"
    return _yerel.s


def tek_hat(hat_id):
    """Bir hattı çeker. Zaman damgası istek anına aittir (paralel turlarda doğruluk için)."""
    kod, otobusler, hata = hat_konumlarini_cek(_oturum(), hat_id)
    zaman = datetime.now(timezone.utc).isoformat(timespec="seconds")
    satirlar = [
        (zaman, hat_id, o.get("OtobusId"), o.get("Yon"),
         koordinat(o.get("KoorX")), koordinat(o.get("KoorY")), i)
        for i, o in enumerate(otobusler)
        if o.get("OtobusId") is not None
    ]
    return satirlar, ((zaman, hat_id, kod, hata) if hata else None)


def bir_tur(yazici, hatlar, paralel=1):
    satirlar, hatalar = [], []
    if paralel <= 1:
        sonuclar = []
        for h in hatlar:
            sonuclar.append(tek_hat(h))
            time.sleep(1)  # tek tek çekerken API'ye nazik olalım
    else:
        with ThreadPoolExecutor(max_workers=paralel) as havuz:
            sonuclar = list(havuz.map(tek_hat, hatlar))
    for s, h in sonuclar:
        satirlar += s
        if h:
            hatalar.append(h)
    yazici.yaz(satirlar, hatalar)
    if hatalar:
        log.warning("%d hatta hata (ör. hat %s: %s)", len(hatalar), hatalar[0][1], hatalar[0][3][:120])
    log.info("%d hat tarandı, %d otobüs konumu kaydedildi", len(hatlar), len(satirlar))


# ---------------------------------------------------------------- hız sınırına uyan planlayıcı
#
# İzmir API'si istek sınırı uyguluyor (paralel 446 istekte ~60'ı geçiyor, gerisi
# "API rate limit exceeded"). Planlayıcı:
#   - istekleri ortak bir hız sınırlayıcıdan geçirir (sınır hatasında hızı %15 düşürür,
#     sorunsuz geçen her 20 saniyede biraz artırır) -> gerçek sınırın hemen altında gezinir;
#   - otobüsü olan hatları AKTIF_ARALIK_SN'de, boş dönenleri BOS_ARALIK_SN'de bir sorar;
#   - her dakika hız, hata ve kapsama özetini log'a yazar.

AKTIF_ARALIK_SN = 300
BOS_ARALIK_SN = 1800
SINIR_TEKRAR_SN = 45        # sınıra takılan hat bu kadar sonra yeniden denenir
HATA_TEKRAR_SN = 120


class HizSinirlayici:
    """İş parçacıkları arasında paylaşılan, uyarlanabilir istek hızı (istek/sn)."""

    def __init__(self, hiz, en_az=0.2, en_cok=3.0, artis=0.02, dusus=0.85):
        self.hiz, self.en_az, self.en_cok, self.artis, self.dusus = hiz, en_az, en_cok, artis, dusus
        self._kilit = threading.Lock()
        self._sonraki = time.monotonic()
        self._son_dusus = 0.0
        self._son_artis = time.monotonic()

    def bekle(self):
        with self._kilit:
            simdi = time.monotonic()
            an = max(simdi, self._sonraki)
            self._sonraki = an + 1.0 / self.hiz
        time.sleep(max(0.0, an - time.monotonic()))

    def sinira_takildi(self):
        with self._kilit:
            simdi = time.monotonic()
            if simdi - self._son_dusus > 20:  # aynı pencerede tekrar tekrar düşürme
                self.hiz = max(self.en_az, self.hiz * self.dusus)
                self._son_dusus = simdi
                self._sonraki = simdi + 15  # pencerenin boşalması için kısa ara
                self._son_artis = simdi
                return True
        return False

    def basarili(self):
        with self._kilit:
            simdi = time.monotonic()
            if simdi - self._son_artis >= 20 and simdi - self._son_dusus >= 60:
                self.hiz = min(self.en_cok, self.hiz + self.artis)
                self._son_artis = simdi


def _sinir_hatasi_mi(kod, hata):
    return kod == 429 or (hata is not None and "rate limit" in str(hata).lower())


def planlayici(yazici, hatlar, bitis, baslangic_hizi=0.8, isci=4):
    import heapq

    sinirlayici = HizSinirlayici(baslangic_hizi)
    simdi = time.time()
    sira = [(simdi + i * 0.001, h) for i, h in enumerate(hatlar)]  # başta hepsi hemen
    heapq.heapify(sira)
    aktif = {}                     # hat -> son başarılı yanıtta otobüs var mıydı
    son_basari = {}                # hat -> son başarılı yanıt zamanı
    kilit = threading.Lock()
    tampon, sayac = [], {"istek": 0, "basari": 0, "sinir": 0, "hata": 0, "satir": 0}
    son_yazma = son_rapor = time.time()

    def isle(hat):
        sinirlayici.bekle()
        kod, otobusler, hata = hat_konumlarini_cek(_oturum(), hat)
        zaman = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return hat, kod, otobusler, hata, zaman

    def rapor():
        t = time.time()
        aktifler = [h for h, a in aktif.items() if a]
        guncel = sum(1 for h in aktifler if t - son_basari.get(h, 0) <= AKTIF_ARALIK_SN + 90)
        log.info("Hız %.2f istek/sn | son dk: %d istek, %d başarılı, %d sınır, %d hata, %d satır | "
                 "aktif hat %d, güncel %d (%%%d) | yanıt alınan hat %d/%d",
                 sinirlayici.hiz, sayac["istek"], sayac["basari"], sayac["sinir"], sayac["hata"],
                 sayac["satir"], len(aktifler), guncel, 100 * guncel / max(1, len(aktifler)),
                 len(son_basari), len(hatlar))
        for k in sayac:
            sayac[k] = 0

    with ThreadPoolExecutor(max_workers=isci) as havuz:
        bekleyen = set()
        while time.time() < bitis:
            # Vadesi gelen hatları işçilere ver (işçi sayısının 2 katından fazla kuyruk tutma)
            while sira and sira[0][0] <= time.time() and len(bekleyen) < isci * 2:
                _, hat = heapq.heappop(sira)
                bekleyen.add(havuz.submit(isle, hat))
            biten = [f for f in bekleyen if f.done()]
            for f in biten:
                bekleyen.discard(f)
                hat, kod, otobusler, hata, zaman = f.result()
                sayac["istek"] += 1
                t = time.time()
                if _sinir_hatasi_mi(kod, hata):
                    sayac["sinir"] += 1
                    if sinirlayici.sinira_takildi():
                        log.warning("API sınırına takıldı; hız %.2f istek/sn'ye düşürüldü", sinirlayici.hiz)
                    heapq.heappush(sira, (t + SINIR_TEKRAR_SN, hat))
                    continue
                if kod not in (200, 204) or (hata and not otobusler):
                    sayac["hata"] += 1
                    heapq.heappush(sira, (t + HATA_TEKRAR_SN, hat))
                    continue
                sinirlayici.basarili()
                sayac["basari"] += 1
                satirlar = [
                    (zaman, hat, o.get("OtobusId"), o.get("Yon"),
                     koordinat(o.get("KoorX")), koordinat(o.get("KoorY")), i)
                    for i, o in enumerate(otobusler) if o.get("OtobusId") is not None
                ]
                tampon += satirlar
                sayac["satir"] += len(satirlar)
                aktif[hat] = bool(satirlar)
                son_basari[hat] = t
                heapq.heappush(sira, (t + (AKTIF_ARALIK_SN if satirlar else BOS_ARALIK_SN), hat))
            if time.time() - son_yazma >= 30 and tampon:
                yazici.yaz(tampon, [])
                tampon, son_yazma = [], time.time()
            if time.time() - son_rapor >= 60:
                rapor()
                son_rapor = time.time()
            time.sleep(0.05)
        for f in bekleyen:  # yarım kalanları topla
            hat, kod, otobusler, hata, zaman = f.result()
            if kod == 200 and otobusler:
                tampon += [(zaman, hat, o.get("OtobusId"), o.get("Yon"),
                            koordinat(o.get("KoorX")), koordinat(o.get("KoorY")), i)
                           for i, o in enumerate(otobusler) if o.get("OtobusId") is not None]
    if tampon:
        yazici.yaz(tampon, [])
    rapor()
    log.info("Süre doldu.")


def tum_hatlar(desen=HAT_LISTESI_DESENI):
    """Referans hat listesindeki tüm hat numaraları (data/ref, ';' ayraçlı)."""
    dosyalar = glob.glob(desen)
    if not dosyalar:
        raise SystemExit(f"Hat listesi bulunamadı: {desen} (önce referans_indir.py çalıştır)")
    with open(dosyalar[0], encoding="utf-8-sig", newline="") as f:
        return sorted({int(r["HAT_NO"]) for r in csv.DictReader(f, delimiter=";") if r["HAT_NO"].strip()})


# ---------------------------------------------------------------- ana döngü

def main():
    p = argparse.ArgumentParser(description="ESHOT canlı otobüs konumu toplayıcı")
    p.add_argument("--hatlar", type=int, nargs="+", default=VARSAYILAN_HATLAR)
    p.add_argument("--tum-hatlar", action="store_true", help="Referans listesindeki tüm hatlar")
    p.add_argument("--paralel", type=int, default=1, help="Aynı anda kaç istek")
    p.add_argument("--aralik", type=int, default=60, help="Turlar arası saniye")
    p.add_argument("--db", default=DB_YOLU, help="SQLite dosyası (varsayılan çıktı)")
    p.add_argument("--cikti", help="SQLite yerine bu CSV dosyasına yaz")
    p.add_argument("--sure", type=float, help="Bu kadar dakika sonra dur")
    p.add_argument("--tek-sefer", action="store_true", help="Bir tur çek ve çık")
    p.add_argument("--planlayici", action="store_true",
                   help="API hız sınırına uyan öncelikli planlayıcı (tüm hatlar için önerilen)")
    p.add_argument("--hiz", type=float, default=0.8, help="Planlayıcı başlangıç hızı (istek/sn)")
    p.add_argument("--donen-klasor", help="Saatlik dönen CSV dosyaları bu klasöre (hazir/ alt klasörüne gzip)")
    args = p.parse_args()

    hatlar = tum_hatlar() if args.tum_hatlar else args.hatlar
    if args.donen_klasor:
        yazici = DonenCsvYazici(args.donen_klasor)
    elif args.cikti:
        yazici = CsvYazici(args.cikti)
    else:
        yazici = SqliteYazici(args.db)
    bitis = time.time() + args.sure * 60 if args.sure else None

    if args.planlayici:
        log.info("Planlayıcı başladı. %d hat | başlangıç hızı %.2f istek/sn | Çıktı: %s | Süre: %s dk",
                 len(hatlar), args.hiz, args.donen_klasor or args.cikti or args.db, args.sure or "sınırsız")
        try:
            planlayici(yazici, hatlar, bitis or float("inf"), args.hiz)
        except KeyboardInterrupt:
            log.info("Durduruldu.")
        finally:
            yazici.kapat()
        return

    log.info("Başladı. %d hat | Paralel: %d | Aralık: %s sn | Çıktı: %s | Süre: %s dk",
             len(hatlar), args.paralel, args.aralik, args.cikti or args.db, args.sure or "sınırsız")
    try:
        while True:
            baslangic = time.time()
            bir_tur(yazici, hatlar, args.paralel)
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
