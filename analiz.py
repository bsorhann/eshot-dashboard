"""
Ham otobüs konumlarından durak geçişlerini ve sefer aralıklarını hesaplar.

Adımlar (her gün ve her hat için ayrı ayrı, bellek dostu):
  1. Temizlik           -> her ölçüm ve otobüs için tek canlı konum (temizlik.py)
  2. Güzergaha oturtma  -> konumu "güzergah başından kaç metre" bilgisine çevir
  3. Yön eşleştirme     -> API yönü (0/1) ile güzergah yönünü (1/2) veriden eşleştir
  4. Sefer bölme        -> otobüsün kesintisiz ilerlediği parçalar
  5. Durak geçişi       -> bir durağın önünden geçiş anını iki ölçüm arasında oranla
Sonra tüm günler birlikte:
  6. Sefer aralığı      -> aynı durağa ardışık iki otobüsün geliş farkı
  7. Özet tabloları     -> dashboard'un okuduğu küçük CSV'ler (data/islenmis/)

Sonuçlar takvim ayı bazında data/islenmis/<YYYY-MM>/ klasörlerine yazılır.

Kullanım:
    python analiz.py                 # dünün ve bugünün ayı (her gece çalışan varsayılan)
    python analiz.py --ay 2026-11    # belirli bir ay
    python analiz.py --hepsi         # verisi bulunan tüm aylar

Ham veri aranan yerler: data/raw/<gün>/*.csv.gz (eski, git içinde) ve
data/indirilen/<gün>/*.csv.gz (Release'lerden indirilen).
"""

import argparse
import glob
import json
import os
import re
import shutil
import tempfile
import time

import numpy as np
import pandas as pd

from temizlik import canli_konumlar

REF = os.path.join("data", "ref")
CIKTI = os.path.join("data", "islenmis")
HAM_DESENLER = [os.path.join("data", "raw", "*", "*.csv.gz"),
                os.path.join("data", "indirilen", "*", "*.csv.gz")]

GUZERGAH_MAKS_UZAKLIK_M = 150   # bundan uzak konumlar güzergah dışı sayılır
DURAK_MAKS_UZAKLIK_M = 50       # durak bu yönün güzergahına bu kadar yakın olmalı
SEFER_MAKS_BOSLUK_SN = 900      # iki ölçüm arası bundan uzunsa sefer bölünür (hız sınırı nedeniyle 5-8 dk örnekleme + 1 kayıp)
SEFER_MAKS_GERI_M = 300         # bu kadar geri gidiş = yeni sefer (hat sonuna gelip dönmüş)
YON_MAKS_ADIM_M = 8000          # yön eşleştirmede dikkate alınan en büyük adım
VERI_BOSLUK_ESIK_DK = 15        # bundan uzun veri gelmeyen dönemler aralık ölçümünden çıkarılır
GUVENILIR_DURAK_ORANI = 0.8     # geçiş sayısı medyanın bu oranından az olan duraklar dışarıda
DAGILIM_ADIM_DK = 2             # dağılım grafiği kutu genişliği
DAGILIM_UST_DK = 180            # bundan uzun aralıklar son kutuya yığılır
KOVA_SAYISI = 8                 # özetleme sırasında bellekte aynı anda hatların 1/8'i bulunur
YEREL_SAAT = "Europe/Istanbul"
HY = ["hat_id", "rota_yon"]


# ---------------------------------------------------------------- geometri

def _metre(lat, lon, lat0, lon0):
    """Küçük alanlar için yeterli düzlem yaklaşımı (eşdikdörtgen)."""
    x = (np.asarray(lon, dtype=float) - lon0) * np.cos(np.radians(lat0)) * 111_320
    y = (np.asarray(lat, dtype=float) - lat0) * 110_540
    return x, y


def guzergaha_oturt(lat, lon, rota_lat, rota_lon, parca=2000):
    """
    Noktaları bir çoklu çizgiye (güzergah) izdüşürür.
    Dönüş: (güzergah boyunca mesafe [m], güzergaha uzaklık [m])
    Bellek için noktalar 'parca' büyüklüğünde bloklar halinde işlenir.
    """
    lat0, lon0 = float(np.mean(rota_lat)), float(np.mean(rota_lon))
    rx, ry = _metre(rota_lat, rota_lon, lat0, lon0)
    px, py = _metre(lat, lon, lat0, lon0)

    ax, ay, bx, by = rx[:-1], ry[:-1], rx[1:], ry[1:]
    dx, dy = bx - ax, by - ay
    uzunluk2 = dx * dx + dy * dy
    uzunluk2[uzunluk2 == 0] = 1e-9
    kumulatif = np.concatenate([[0], np.cumsum(np.sqrt(dx * dx + dy * dy))])[:-1]

    mesafe = np.empty(len(px))
    uzaklik = np.empty(len(px))
    for b in range(0, len(px), parca):
        x, y = px[b:b + parca, None], py[b:b + parca, None]
        t = np.clip(((x - ax) * dx + (y - ay) * dy) / uzunluk2, 0, 1)
        u2 = (x - (ax + t * dx)) ** 2 + (y - (ay + t * dy)) ** 2
        en = np.argmin(u2, axis=1)
        satir = np.arange(len(en))
        mesafe[b:b + parca] = kumulatif[en] + t[satir, en] * np.sqrt(uzunluk2[en])
        uzaklik[b:b + parca] = np.sqrt(u2[satir, en])
    return mesafe, uzaklik


# ---------------------------------------------------------------- veri yükleme

def referans_yukle():
    oku = lambda desen: pd.read_csv(glob.glob(os.path.join(REF, desen))[0], sep=";")
    return (oku("otobus-hat-guzergahlari__*.csv"),
            oku("eshot-otobus-duraklari__*.csv"),
            oku("eshot-otobus-hat-listesi__*.csv"))


def rota_sozlugu(guzergah):
    """{(hat, yön): (enlem dizisi, boylam dizisi)} - her seferinde büyük tabloyu süzmemek için."""
    return {(int(h), int(y)): (g.ENLEM.values, g.BOYLAM.values)
            for (h, y), g in guzergah.groupby(["HAT_NO", "YON"]) if len(g) >= 2}


def gunlere_gore_dosyalar(gun_sayisi=14):
    """{gün: [dosyalar]} - klasör adı YYYY-MM-DD olan günler; son 'gun_sayisi' gün (0 = hepsi)."""
    gunler = {}
    for desen in HAM_DESENLER:
        for f in glob.glob(desen):
            gun = os.path.basename(os.path.dirname(f))
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", gun):
                gunler.setdefault(gun, []).append(f)
    if gun_sayisi:
        sinir = (pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=gun_sayisi)).strftime("%Y-%m-%d")
        gunler = {g: f for g, f in gunler.items() if g >= sinir}
    return dict(sorted(gunler.items()))


TIPLER = {"hat_id": "int64", "otobus_id": "int64", "yon": "float64",
          "koor_x": "float64", "koor_y": "float64", "sira": "int64"}


def ham_veri_yukle(dosyalar=None):
    """Verilen dosyaları (varsayılan: tüm ham veri) tek tabloda birleştirir."""
    if dosyalar is None:
        dosyalar = sorted(f for d in HAM_DESENLER for f in glob.glob(d))
    if not dosyalar:
        raise SystemExit("Ham veri bulunamadi.")
    parcalar = [pd.read_csv(f, dtype=TIPLER) for f in sorted(dosyalar)]
    parcalar = [p for p in parcalar if len(p)]  # gece: otobus yok, yalnizca baslik
    if not parcalar:
        return pd.DataFrame(columns=list(TIPLER) + ["zaman_utc"])
    return pd.concat(parcalar, ignore_index=True)


# ---------------------------------------------------------------- tek hat, tek gün

def konumlari_isle(temiz, rotalar):
    """Her konuma, hattın iki yönü için güzergah mesafesi (s1, s2) ve uzaklığı (d1, d2) ekler."""
    parcalar = []
    for hat, grup in temiz.groupby("hat_id"):
        grup = grup.copy()
        for yon in (1, 2):
            rota = rotalar.get((int(hat), yon))
            if rota is None:
                grup[f"s{yon}"], grup[f"d{yon}"] = np.nan, np.nan
                continue
            grup[f"s{yon}"], grup[f"d{yon}"] = guzergaha_oturt(grup.koor_x, grup.koor_y, *rota)
        parcalar.append(grup)
    return pd.concat(parcalar, ignore_index=True) if parcalar else temiz.iloc[0:0]


def yon_eslestir(konum):
    """
    API yönünü (0/1) güzergah yönüne (1/2) eşler: otobüs hangi güzergahta
    ileri doğru ilerliyorsa o yön doğrudur.
    Dönüş: {(hat, api_yon): (rota_yon, ileri_oran)}
    """
    eslesme = {}
    k = konum.sort_values(["hat_id", "otobus_id", "zaman_utc"])
    for (hat, api_yon), grup in k.groupby(["hat_id", "yon"]):
        skor = {}
        for rota_yon in (1, 2):
            fark = grup.groupby("otobus_id")[f"s{rota_yon}"].diff()
            yakin = grup[f"d{rota_yon}"] < GUZERGAH_MAKS_UZAKLIK_M
            hareket = fark[yakin & (fark.abs() > 20) & (fark.abs() < YON_MAKS_ADIM_M)]
            skor[rota_yon] = (hareket > 0).mean() if len(hareket) else 0
        en_iyi = max(skor, key=skor.get)
        eslesme[(int(hat), api_yon)] = (en_iyi, round(float(skor[en_iyi]), 3))
    return eslesme


def seferlere_bol(konum, eslesme):
    """Her konuma kendi yönünün güzergah mesafesini ve bir sefer numarası verir."""
    k = konum.copy()
    k["rota_yon"] = [eslesme.get((int(h), y), (np.nan,))[0] for h, y in zip(k.hat_id, k.yon)]
    k["s"] = np.where(k.rota_yon == 1, k.s1, k.s2)
    k["d"] = np.where(k.rota_yon == 1, k.d1, k.d2)
    k = k[k.d < GUZERGAH_MAKS_UZAKLIK_M].sort_values(["hat_id", "otobus_id", "zaman_utc"])

    onceki = k.groupby(["hat_id", "otobus_id"])
    yeni = (
        (onceki.rota_yon.shift() != k.rota_yon)
        | (onceki.zaman_utc.diff().dt.total_seconds() > SEFER_MAKS_BOSLUK_SN)
        | (onceki.s.diff() < -SEFER_MAKS_GERI_M)
    )
    k["sefer"] = yeni.cumsum()
    return k


def duraklari_hazirla(duraklar, rotalar, hatlar):
    """Her hat ve yön için güzergah üzerindeki durakları sırayla döndürür."""
    hat_durak = duraklar.assign(
        hat=duraklar.DURAKTAN_GECEN_HATLAR.fillna("").astype(str).str.split("-")).explode("hat")
    hat_durak = hat_durak[hat_durak.hat.str.strip().str.fullmatch(r"\d+")]
    hat_durak["hat"] = hat_durak.hat.astype(int)
    satirlar = []
    for hat, d in hat_durak[hat_durak.hat.isin(hatlar)].groupby("hat"):
        for yon in (1, 2):
            rota = rotalar.get((int(hat), yon))
            if rota is None:
                continue
            s, uz = guzergaha_oturt(d.ENLEM, d.BOYLAM, *rota)
            ek = d.assign(hat_id=int(hat), rota_yon=yon, s_durak=s, d_durak=uz)
            satirlar.append(ek[ek.d_durak < DURAK_MAKS_UZAKLIK_M])
    sonuc = pd.concat(satirlar, ignore_index=True).sort_values(["hat_id", "rota_yon", "s_durak"])
    sonuc["durak_sira"] = sonuc.groupby(["hat_id", "rota_yon"]).cumcount() + 1
    return sonuc[["hat_id", "rota_yon", "durak_sira", "DURAK_ID", "DURAK_ADI",
                  "ENLEM", "BOYLAM", "s_durak"]].reset_index(drop=True)


GECIS_SUTUNLARI = ["hat_id", "rota_yon", "durak_sira", "otobus_id", "sefer", "gecis_zamani"]


def durak_gecisleri(k, durak_tablo):
    """İki ardışık ölçüm arasında geçilen durakların geçiş anını doğrusal oranlamayla bulur."""
    k = k.sort_values(["sefer", "zaman_utc"])
    a = k.groupby("sefer").shift()
    adim = k.assign(s0=a.s, t0=a.zaman_utc).dropna(subset=["s0"])
    adim = adim[adim.s > adim.s0]
    if adim.empty:
        return pd.DataFrame(columns=GECIS_SUTUNLARI)

    parcalar = []
    for (hat, yon), grup in adim.groupby(HY):  # hat-yön bazında birleştir: bellek dostu
        d = durak_tablo[(durak_tablo.hat_id == hat) & (durak_tablo.rota_yon == yon)]
        if d.empty:
            continue
        b = grup[["hat_id", "rota_yon", "otobus_id", "sefer", "s0", "s", "t0", "zaman_utc"]].merge(
            d[["hat_id", "rota_yon", "durak_sira", "s_durak"]], on=HY)
        b = b[(b.s_durak > b.s0) & (b.s_durak <= b.s)]
        oran = (b.s_durak - b.s0) / (b.s - b.s0)
        b["gecis_zamani"] = b.t0 + (b.zaman_utc - b.t0) * oran
        parcalar.append(b[GECIS_SUTUNLARI])
    return pd.concat(parcalar, ignore_index=True) if parcalar else pd.DataFrame(columns=GECIS_SUTUNLARI)


def gun_isle(ham, rotalar, durak_tablo, sefer_ofset=0):
    """Bir günün ham verisinden durak geçişlerini çıkarır."""
    gps = int(ham.koor_x.notna().sum())
    temiz = canli_konumlar(ham)
    temiz["zaman_utc"] = pd.to_datetime(temiz.zaman_utc, utc=True)
    konum = konumlari_isle(temiz, rotalar)
    if konum.empty:
        return pd.DataFrame(columns=GECIS_SUTUNLARI), {"gps": gps, "canli": 0}, {}
    eslesme = yon_eslestir(konum)
    k = seferlere_bol(konum, eslesme)
    k["sefer"] = k.sefer + sefer_ofset
    gecis = durak_gecisleri(k, durak_tablo)
    return gecis, {"gps": gps, "canli": len(temiz)}, eslesme


# ---------------------------------------------------------------- tüm günler

def durak_anahtari(df):
    """(hat, yön, durak sırası) için tek bir tamsayı anahtar (hızlı süzme için)."""
    return (df.hat_id.astype("int32") * 100_000 + df.rota_yon.astype("int32") * 10_000
            + df.durak_sira.astype("int32"))


def guvenilir_durak_isaretle(gecis_sayilari, durak_tablo, oran=GUVENILIR_DURAK_ORANI):
    """
    Hat sonlarındaki duraklarda otobüs bekleyip döndüğü için geçişler kaçırılır ve
    sahte uzun aralıklar oluşur. Geçiş sayısı, o hat-yöndeki durakların medyanının
    %80'inden az olan duraklar güvenilmez sayılır.
    gecis_sayilari: (hat_id, rota_yon, durak_sira) indeksli geçiş sayıları.
    """
    say = gecis_sayilari.rename("gecis_sayisi")
    d = durak_tablo.merge(say.reset_index(), on=["hat_id", "rota_yon", "durak_sira"], how="left")
    d["gecis_sayisi"] = d.gecis_sayisi.fillna(0).astype(int)
    medyan = d.groupby(["hat_id", "rota_yon"]).gecis_sayisi.transform("median")
    d["guvenilir"] = (d.gecis_sayisi >= oran * medyan) & (d.gecis_sayisi > 0)
    return d


def veri_bosluklari(zamanlar, esik_dk=VERI_BOSLUK_ESIK_DK):
    """Hiç veri gelmeyen dönemler (gece, iş geçişi, API kesintisi): [(başlangıç, bitiş), ...]"""
    t = pd.Series(pd.to_datetime(pd.unique(zamanlar), utc=True)).sort_values().reset_index(drop=True)
    fark = t.diff()
    i = fark[fark > pd.Timedelta(minutes=esik_dk)].index
    return pd.DataFrame({"bas": t[i - 1].values, "bit": t[i].values})


def sefer_araliklari(gecis, bosluklar=None):
    """
    Aynı durağa ardışık iki farklı otobüsün geliş farkı (dakika).
    İki geçiş arasına bir veri boşluğu giriyorsa aralık ölçülemez, atlanır.
    """
    g = gecis.sort_values(["hat_id", "rota_yon", "durak_sira", "gecis_zamani"]).copy()
    grup = g.groupby(["hat_id", "rota_yon", "durak_sira"])
    g["onceki_otobus"] = grup.otobus_id.shift()
    g["onceki_gecis"] = grup.gecis_zamani.shift()
    g["aralik_dk"] = (g.gecis_zamani - g.onceki_gecis).dt.total_seconds() / 60
    g = g.dropna(subset=["aralik_dk"])
    g = g[g.onceki_otobus != g.otobus_id]

    if bosluklar is not None and len(bosluklar):
        bas = bosluklar.bas.values
        bit = bosluklar.bit.values
        o = g.onceki_gecis.values
        s = g.gecis_zamani.values
        # aralık [o, s] ile herhangi bir boşluk [bas, bit] kesişiyor mu?
        j = np.searchsorted(bit, o, side="left")       # o'dan sonra biten ilk boşluk
        j_ok = j < len(bas)
        kesisir = np.zeros(len(g), dtype=bool)
        kesisir[j_ok] = bas[j[j_ok]] < s[j_ok]
        g = g[~kesisir]
    return g.drop(columns=["onceki_gecis", "onceki_otobus"])


def aralik_sikistir(aralik):
    """Gün sonunda yalnızca gereken sütunlar, küçük veri tipleriyle (14 gün x 446 hat için bellek)."""
    yerel = pd.to_datetime(aralik.gecis_zamani, utc=True).dt.tz_convert(YEREL_SAAT)
    return pd.DataFrame({
        "hat_id": aralik.hat_id.astype("int16").values,
        "rota_yon": aralik.rota_yon.astype("int8").values,
        "durak_sira": aralik.durak_sira.astype("int16").values,
        "aralik_dk": aralik.aralik_dk.astype("float32").values,
        "saat": yerel.dt.hour.astype("int8").values,
        "gun": pd.Categorical(yerel.dt.strftime("%Y-%m-%d").values),
    })


# Saat dilimleri (yerel saat). Tarifede sefer sıklığı dilime göre değiştiği için her
# bekleme, aynı hat-yönün AYNI DİLİMDEKİ tipik beklemesiyle karşılaştırılır. Tüm günün
# medyanıyla karşılaştırmak, akşam/gece planlı seyrek seferleri "uzun bekleme" gibi gösteriyordu.
SAAT_DILIMI = np.array([0] * 6 + [1] * 4 + [2] * 6 + [3] * 4 + [4] * 4)  # 0-5 gece, 6-9 sabah zirvesi,
#                                                                          10-15 gün içi, 16-19 akşam zirvesi, 20-23 akşam
MIN_DILIM_OLCUM = 20  # dilimde bundan az ölçüm varsa hattın tüm gün medyanı kullanılır


def aralik_isaretle(a):
    """
    Her aralığı, kendi hat-yönünün aynı saat dilimindeki tipik aralığına (medyan) göre etiketler:
      yigilma = medyanın yarısından kısa (otobüsler üst üste geldi)
      uzun    = medyanın 1,5 katından uzun (durakta uzun bekleme)
    """
    dilim = SAAT_DILIMI[a.saat.astype(int).values]
    g = a.assign(_dilim=dilim).groupby(HY + ["_dilim"]).aralik_dk
    referans = g.transform("median")
    yeterli = g.transform("size") >= MIN_DILIM_OLCUM
    referans = referans.where(yeterli, a.groupby(HY).aralik_dk.transform("median"))
    a["yigilma"] = a.aralik_dk < 0.5 * referans
    a["uzun"] = a.aralik_dk > 1.5 * referans
    return a


def metrikler(a, anahtar):
    """Gruplar için düzenlilik metrikleri (vektörel, binlerce grupta hızlı)."""
    g = a.groupby(anahtar, observed=True)
    m = g.aralik_dk.agg(olcum="size", medyan_dk="median", ort_dk="mean", std_dk="std")
    m["p90_dk"] = g.aralik_dk.quantile(0.9)
    m["p99_dk"] = g.aralik_dk.quantile(0.99)
    m["dagilim_cv"] = m.std_dk / m.ort_dk
    m["yigilma_orani"] = g.yigilma.mean()
    m["uzun_bekleme_orani"] = g.uzun.mean()
    return m.drop(columns="std_dk").reset_index()


def tr_baslik(metin):
    """'BOSTANLI İSKELE' -> 'Bostanlı İskele' (Türkçe I/İ kurallarıyla)."""
    if not isinstance(metin, str):
        return metin
    kelimeler = []
    for k in metin.strip().split():
        kucuk = k.replace("I", "ı").replace("İ", "i").lower()
        ilk = {"i": "İ", "ı": "I"}.get(kucuk[:1], kucuk[:1].upper())
        kelimeler.append(ilk + kucuk[1:])
    return " ".join(kelimeler)


def yon_etiketleri(hat_listesi):
    """Güzergah yönü 1: başlangıç -> bitiş, yön 2: bitiş -> başlangıç (veriyle doğrulandı)."""
    h = hat_listesi.drop_duplicates("HAT_NO").set_index("HAT_NO")
    # Uçlar hat adından alınır; ayrı sütunlarda yazım tutarsızlıkları var (HASTANESI/HASTANESİ)
    parcalar = h.HAT_ADI.astype(str).str.split(r"\s+-\s+", n=1, regex=True)
    bas = parcalar.str[0].map(tr_baslik)
    bit = parcalar.str[-1].map(tr_baslik)
    return pd.concat([
        pd.DataFrame({"hat_id": h.index, "rota_yon": 1, "yon_adi": (bit + " yönü").values}),
        pd.DataFrame({"hat_id": h.index, "rota_yon": 2, "yon_adi": (bas + " yönü").values}),
    ])


def ozet_tablolari(a, durak_tablo, hat_listesi):
    adlar = (hat_listesi.drop_duplicates("HAT_NO").set_index("HAT_NO").HAT_ADI.astype(str)
             .str.split(r"\s+-\s+", regex=True).map(lambda p: " – ".join(tr_baslik(x) for x in p)))

    ozet = metrikler(a, HY)
    ozet.insert(1, "hat_adi", ozet.hat_id.map(adlar))
    ozet = ozet.merge(yon_etiketleri(hat_listesi), on=HY, how="left")

    saatlik = metrikler(a, HY + ["saat"])

    durak = metrikler(a, HY + ["durak_sira"]).merge(
        durak_tablo[HY + ["durak_sira", "DURAK_ID", "DURAK_ADI", "ENLEM", "BOYLAM"]],
        on=HY + ["durak_sira"], how="left")

    gunluk = metrikler(a, HY + ["gun"])

    kutu = (np.minimum(a.aralik_dk, DAGILIM_UST_DK) // DAGILIM_ADIM_DK * DAGILIM_ADIM_DK).astype(int)
    dagilim = a.assign(kutu_dk=kutu).groupby(HY + ["kutu_dk"], observed=True).size().rename("adet").reset_index()
    return ozet, saatlik, durak, gunluk, dagilim


# ---------------------------------------------------------------- ana akış

def yaz(df, klasor, ad):
    """CSV yazar. Metrikler 4 ondalık basamak; koordinatlar 5 basamak (~1 m).
    (Genel %.4g biçimi koordinatları 38.37'ye indirip durakları yüzlerce metre kaydırıyordu.)"""
    df = df.copy()
    for s in df.select_dtypes("float").columns:
        df[s] = df[s].round(5 if s in ("ENLEM", "BOYLAM") else 4)
    df.to_csv(os.path.join(klasor, ad), index=False)


def ay_isle(ay, gunler, rotalar, durak_tablo_tum, hat_listesi, t0):
    """Bir takvim ayının günlerini işler ve data/islenmis/<ay>/ klasörüne yazar."""
    print(f"== {ay}: {len(gunler)} gun ({list(gunler)[0]} .. {list(gunler)[-1]})")

    # Her gün ayrı işlenir; gün sınırı 00:00 UTC = 03:00 yerel, sefer olmayan saat.
    # Bir durağın aralıkları yalnızca o durağın geçişlerine bağlı olduğundan, güvenilmez
    # durakları en sonda süzmek, önce süzmekle aynı sonucu verir.
    # Bellek için: günlük aralıklar hat numarasına göre KOVA_SAYISI kovaya bölünüp diske
    # yazılır; sonunda her kova ayrı özetlenir (tüm metrikler hat içinde hesaplanıyor).
    gecici = tempfile.mkdtemp(prefix="aralik_")
    gecis_sayilari, tum_zamanlar = [], []
    sayac = {"ham": 0, "gps": 0, "canli": 0, "gecis": 0, "aralik": 0}
    for gun, dosyalar in gunler.items():
        ham = ham_veri_yukle(dosyalar)
        if ham.empty:
            continue
        zamanlar = pd.unique(ham.zaman_utc)
        gecis, s, _ = gun_isle(ham, rotalar, durak_tablo_tum)
        if len(gecis):
            gecis_sayilari.append(gecis.groupby(["hat_id", "rota_yon", "durak_sira"]).size())
            a = aralik_sikistir(sefer_araliklari(gecis, veri_bosluklari(zamanlar)))
            for k, parca in a.groupby(a.hat_id % KOVA_SAYISI):
                parca.to_pickle(os.path.join(gecici, f"k{k}_{gun}.pkl"))
        tum_zamanlar.append(zamanlar)
        sayac["ham"] += len(ham)
        sayac["gps"] += s["gps"]
        sayac["canli"] += s["canli"]
        sayac["gecis"] += len(gecis)
        print(f"  {gun}: {len(dosyalar)} dosya, {len(ham):,} kayit, {ham.hat_id.nunique()} hat, "
              f"{len(gecis):,} gecis ({time.time() - t0:.0f} sn)")
        del ham, gecis

    if not gecis_sayilari:
        print(f"  {ay}: hic gecis bulunamadi, atlandi.")
        shutil.rmtree(gecici, ignore_errors=True)
        return
    toplam = pd.concat(gecis_sayilari).groupby(level=[0, 1, 2]).sum()
    durak_tablo = guvenilir_durak_isaretle(toplam, durak_tablo_tum)
    guvenilir_durak = durak_tablo[durak_tablo.guvenilir]
    guvenilir_anahtar = durak_anahtari(guvenilir_durak).values

    sonuclar = []
    for k in range(KOVA_SAYISI):
        dosyalar = sorted(glob.glob(os.path.join(gecici, f"k{k}_*.pkl")))
        if not dosyalar:
            continue
        aralik = pd.concat([pd.read_pickle(f) for f in dosyalar], ignore_index=True)
        aralik["gun"] = aralik.gun.astype("category")
        aralik = aralik[np.isin(durak_anahtari(aralik).values, guvenilir_anahtar)].reset_index(drop=True)
        if aralik.empty:
            continue
        sayac["aralik"] += len(aralik)
        sonuclar.append(ozet_tablolari(aralik_isaretle(aralik), guvenilir_durak, hat_listesi))
        del aralik
    shutil.rmtree(gecici, ignore_errors=True)
    if not sonuclar:
        print(f"  {ay}: olculebilen aralik yok, atlandi.")
        return
    o, saatlik, durak_ozet, _gunluk, dagilim = (
        pd.concat([s[i] for s in sonuclar], ignore_index=True) for i in range(5))

    # Her gece depoya kaydedildiği için yalnızca dashboard'un kullandığı sütunlar yazılır.
    klasor = os.path.join(CIKTI, ay)
    os.makedirs(klasor, exist_ok=True)
    yaz(o, klasor, "ozet.csv")
    yaz(saatlik[HY + ["saat", "olcum", "medyan_dk", "uzun_bekleme_orani"]], klasor, "saatlik.csv")
    yaz(durak_ozet[HY + ["durak_sira", "DURAK_ADI", "ENLEM", "BOYLAM", "olcum", "medyan_dk",
                        "uzun_bekleme_orani"]], klasor, "durak_ozet.csv")
    yaz(dagilim, klasor, "dagilim.csv")

    zt = pd.to_datetime(pd.Series(pd.unique(np.concatenate(tum_zamanlar))), utc=True)
    meta = {
        "ay": ay,
        "guncelleme_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
        "ilk_olcum_utc": zt.min().isoformat(),
        "son_olcum_utc": zt.max().isoformat(),
        "gun_sayisi": len(gunler),
        "ham_kayit": int(sayac["ham"]),
        "gps_kayit": int(sayac["gps"]),
        "canli_konum": int(sayac["canli"]),
        "hayalet_orani": round(1 - sayac["canli"] / sayac["gps"], 3) if sayac["gps"] else None,
        "aralik_olcumu": int(sayac["aralik"]),
        "hat_sayisi": int(o.hat_id.nunique()),
    }
    with open(os.path.join(klasor, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"  {ay}: gecis {sayac['gecis']:,} | aralik {sayac['aralik']:,} | hat {meta['hat_sayisi']} | "
          f"sure {time.time() - t0:.0f} sn")
    print(o.sort_values("uzun_bekleme_orani", ascending=False).head(5)[
        ["hat_id", "yon_adi", "olcum", "medyan_dk", "uzun_bekleme_orani"]].round(2).to_string(index=False))


def ay_listesini_guncelle():
    """data/islenmis/aylar.json: dashboard'un ay seçimi için mevcut aylar (yeniden eskiye)."""
    bu_ay = pd.Timestamp.now(tz="UTC").strftime("%Y-%m")
    aylar = []
    for m in sorted(glob.glob(os.path.join(CIKTI, "*", "meta.json")), reverse=True):
        with open(m, encoding="utf-8") as f:
            meta = json.load(f)
        aylar.append({"ay": meta["ay"], "gun_sayisi": meta["gun_sayisi"],
                      "tamamlandi": meta["ay"] < bu_ay, "hat_sayisi": meta["hat_sayisi"]})
    with open(os.path.join(CIKTI, "aylar.json"), "w", encoding="utf-8") as f:
        json.dump(aylar, f, ensure_ascii=False, indent=2)
    print("Aylar:", [(a["ay"], a["gun_sayisi"]) for a in aylar])


def eski_duz_dosyalari_sil():
    """Ay klasörlerinden önceki düz yapıdan kalan dosyalar."""
    for ad in ("ozet.csv", "saatlik.csv", "durak_ozet.csv", "dagilim.csv", "meta.json", "duraklar.csv",
               "gunluk.csv", "durak_gecisleri.csv.gz", "sefer_araliklari.csv.gz"):
        yol = os.path.join(CIKTI, ad)
        if os.path.exists(yol):
            os.remove(yol)


def main():
    p = argparse.ArgumentParser(description="Takvim ayı bazında düzenlilik analizi")
    p.add_argument("--ay", nargs="*",
                   help="İşlenecek aylar (YYYY-MM). Verilmezse dünün ve bugünün ayı (UTC): "
                        "her gece içinde bulunulan ay güncellenir, ayın 1'inde biten ay kesinleşir.")
    p.add_argument("--hepsi", action="store_true", help="Verisi bulunan tüm aylar")
    args = p.parse_args()
    t0 = time.time()

    tum_gunler = gunlere_gore_dosyalar(0)
    if not tum_gunler:
        raise SystemExit("Islenecek veri bulunamadi.")
    if args.hepsi:
        aylar = sorted({g[:7] for g in tum_gunler})
    elif args.ay:
        aylar = sorted(set(args.ay))
    else:
        simdi = pd.Timestamp.now(tz="UTC")
        aylar = sorted({(simdi - pd.Timedelta(days=1)).strftime("%Y-%m"), simdi.strftime("%Y-%m")})

    guzergah, duraklar, hat_listesi = referans_yukle()
    rotalar = rota_sozlugu(guzergah)
    durak_tablo = duraklari_hazirla(duraklar, rotalar, sorted({h for h, _ in rotalar}))
    print(f"Aylar: {aylar} | durak tablosu: {len(durak_tablo):,} satir ({time.time() - t0:.0f} sn)")

    os.makedirs(CIKTI, exist_ok=True)
    eski_duz_dosyalari_sil()
    for ay in aylar:
        gunler = {g: f for g, f in tum_gunler.items() if g.startswith(ay)}
        if gunler:
            ay_isle(ay, gunler, rotalar, durak_tablo, hat_listesi, t0)
        else:
            print(f"== {ay}: veri yok")
    ay_listesini_guncelle()
    print(f"Toplam sure: {time.time() - t0:.0f} sn")


if __name__ == "__main__":
    main()
