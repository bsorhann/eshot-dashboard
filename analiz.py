"""
Ham otobüs konumlarından durak geçişlerini ve sefer aralıklarını hesaplar.

Adımlar:
  1. Temizlik           -> her tur ve otobüs için tek canlı konum (temizlik.py)
  2. Güzergaha oturtma  -> konumu "güzergah başından kaç metre" bilgisine çevir
  3. Yön eşleştirme     -> API yönü (0/1) ile güzergah yönünü (1/2) veriden eşleştir
  4. Sefer bölme        -> otobüsün kesintisiz ilerlediği parçalar
  5. Durak geçişi       -> bir durağın önünden geçiş anını iki ölçüm arasında oranla
  6. Sefer aralığı      -> aynı durağa ardışık iki otobüsün geliş farkı

Kullanım:
    python analiz.py                 # data/raw altındaki tüm veriyi işler, data/islenmis'e yazar
"""

import glob
import os

import numpy as np
import pandas as pd

from temizlik import canli_konumlar

REF = os.path.join("data", "ref")
CIKTI = os.path.join("data", "islenmis")

GUZERGAH_MAKS_UZAKLIK_M = 150   # bundan uzak konumlar güzergah dışı sayılır
DURAK_MAKS_UZAKLIK_M = 50       # durak bu yönün güzergahına bu kadar yakın olmalı
SEFER_MAKS_BOSLUK_SN = 300      # iki ölçüm arası bundan uzunsa sefer bölünür
SEFER_MAKS_GERI_M = 300         # bu kadar geri gidiş = yeni sefer (hat sonuna gelip dönmüş)


# ---------------------------------------------------------------- yardımcılar

def _metre(lat, lon, lat0, lon0):
    """Küçük alanlar için yeterli düzlem yaklaşımı (eşdikdörtgen)."""
    x = (np.asarray(lon) - lon0) * np.cos(np.radians(lat0)) * 111_320
    y = (np.asarray(lat) - lat0) * 110_540
    return x, y


def guzergaha_oturt(lat, lon, rota_lat, rota_lon):
    """
    Noktaları bir çoklu çizgiye (güzergah) izdüşürür.
    Dönüş: (güzergah boyunca mesafe [m], güzergaha uzaklık [m])
    """
    lat0, lon0 = float(np.mean(rota_lat)), float(np.mean(rota_lon))
    rx, ry = _metre(rota_lat, rota_lon, lat0, lon0)
    px, py = _metre(lat, lon, lat0, lon0)

    ax, ay, bx, by = rx[:-1], ry[:-1], rx[1:], ry[1:]
    dx, dy = bx - ax, by - ay
    uzunluk2 = dx * dx + dy * dy
    uzunluk2[uzunluk2 == 0] = 1e-9
    kumulatif = np.concatenate([[0], np.cumsum(np.sqrt(dx * dx + dy * dy))])[:-1]

    # (nokta x segment) matrisi
    t = ((px[:, None] - ax) * dx + (py[:, None] - ay) * dy) / uzunluk2
    t = np.clip(t, 0, 1)
    qx, qy = ax + t * dx, ay + t * dy
    uzaklik2 = (px[:, None] - qx) ** 2 + (py[:, None] - qy) ** 2
    en_yakin = np.argmin(uzaklik2, axis=1)
    satir = np.arange(len(px))
    mesafe = kumulatif[en_yakin] + t[satir, en_yakin] * np.sqrt(uzunluk2[en_yakin])
    return mesafe, np.sqrt(uzaklik2[satir, en_yakin])


def referans_yukle():
    oku = lambda desen: pd.read_csv(glob.glob(os.path.join(REF, desen))[0], sep=";")
    return (oku("otobus-hat-guzergahlari__*.csv"),
            oku("eshot-otobus-duraklari__*.csv"),
            oku("eshot-otobus-hat-listesi__*.csv"))


def ham_veri_yukle(desen=os.path.join("data", "raw", "*", "*.csv.gz")):
    dosyalar = sorted(glob.glob(desen))
    if not dosyalar:
        raise SystemExit(f"Veri bulunamadi: {desen}")
    return pd.concat((pd.read_csv(f) for f in dosyalar), ignore_index=True)


# ---------------------------------------------------------------- ana adımlar

def konumlari_isle(temiz, guzergah):
    """Her konuma, hattın iki yönü için güzergah mesafesi ve uzaklığı ekler."""
    parcalar = []
    for hat, grup in temiz.groupby("hat_id"):
        grup = grup.copy()
        for yon in (1, 2):
            rota = guzergah[(guzergah.HAT_NO == hat) & (guzergah.YON == yon)]
            if len(rota) < 2:
                grup[f"s{yon}"], grup[f"d{yon}"] = np.nan, np.nan
                continue
            s, d = guzergaha_oturt(grup.koor_x, grup.koor_y, rota.ENLEM.values, rota.BOYLAM.values)
            grup[f"s{yon}"], grup[f"d{yon}"] = s, d
        parcalar.append(grup)
    return pd.concat(parcalar, ignore_index=True)


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
            hareket = fark[yakin & (fark.abs() > 20) & (fark.abs() < 3000)]
            skor[rota_yon] = (hareket > 0).mean() if len(hareket) else 0
        en_iyi = max(skor, key=skor.get)
        eslesme[(hat, api_yon)] = (en_iyi, round(float(skor[en_iyi]), 3))
    return eslesme


def seferlere_bol(konum, eslesme):
    """Her konuma kendi yönünün güzergah mesafesini ve bir sefer numarası verir."""
    k = konum.copy()
    k["rota_yon"] = [eslesme.get((h, y), (np.nan,))[0] for h, y in zip(k.hat_id, k.yon)]
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


def duraklari_hazirla(duraklar, guzergah, hatlar):
    """Her hat ve yön için güzergah üzerindeki durakları sırayla döndürür."""
    gecen = duraklar.DURAKTAN_GECEN_HATLAR.fillna("").astype(str).str.split("-")
    satirlar = []
    for hat in hatlar:
        d = duraklar[gecen.apply(lambda l: str(hat) in l)]
        for yon in (1, 2):
            rota = guzergah[(guzergah.HAT_NO == hat) & (guzergah.YON == yon)]
            if d.empty or len(rota) < 2:
                continue
            s, uz = guzergaha_oturt(d.ENLEM, d.BOYLAM, rota.ENLEM.values, rota.BOYLAM.values)
            ek = d.assign(hat_id=hat, rota_yon=yon, s_durak=s, d_durak=uz)
            satirlar.append(ek[ek.d_durak < DURAK_MAKS_UZAKLIK_M])
    sonuc = pd.concat(satirlar, ignore_index=True).sort_values(["hat_id", "rota_yon", "s_durak"])
    sonuc["durak_sira"] = sonuc.groupby(["hat_id", "rota_yon"]).cumcount() + 1
    return sonuc[["hat_id", "rota_yon", "durak_sira", "DURAK_ID", "DURAK_ADI",
                  "ENLEM", "BOYLAM", "s_durak"]]


def durak_gecisleri(k, durak_tablo):
    """İki ardışık ölçüm arasında geçilen durakların geçiş anını doğrusal oranlamayla bulur."""
    k = k.sort_values(["sefer", "zaman_utc"])
    a = k.groupby("sefer").shift()
    adim = k.assign(s0=a.s, t0=a.zaman_utc).dropna(subset=["s0"])
    adim = adim[(adim.s > adim.s0)]
    if adim.empty:
        return pd.DataFrame()

    birlesik = adim.merge(durak_tablo, on=["hat_id", "rota_yon"])
    birlesik = birlesik[(birlesik.s_durak > birlesik.s0) & (birlesik.s_durak <= birlesik.s)]
    oran = (birlesik.s_durak - birlesik.s0) / (birlesik.s - birlesik.s0)
    birlesik["gecis_zamani"] = birlesik.t0 + (birlesik.zaman_utc - birlesik.t0) * oran
    return birlesik[["hat_id", "rota_yon", "durak_sira", "DURAK_ID", "DURAK_ADI",
                     "otobus_id", "sefer", "gecis_zamani"]].sort_values(
        ["hat_id", "rota_yon", "durak_sira", "gecis_zamani"])


def sefer_araliklari(gecis):
    """Aynı durağa ardışık iki farklı otobüsün geliş farkı (dakika)."""
    g = gecis.sort_values(["hat_id", "rota_yon", "durak_sira", "gecis_zamani"]).copy()
    grup = g.groupby(["hat_id", "rota_yon", "durak_sira"])
    g["onceki_otobus"] = grup.otobus_id.shift()
    g["aralik_dk"] = grup.gecis_zamani.diff().dt.total_seconds() / 60
    return g.dropna(subset=["aralik_dk"])[lambda x: x.onceki_otobus != x.otobus_id]


def ozet(aralik, hat_listesi):
    """Hat ve yön bazında düzenlilik metrikleri."""
    def metrik(x):
        med = x.median()
        return pd.Series({
            "olcum": len(x),
            "medyan_dk": med,
            "ort_dk": x.mean(),
            "dagilim_cv": x.std() / x.mean() if x.mean() else np.nan,
            "yigilma_orani": (x < 0.5 * med).mean(),        # beklenenin yarısından kısa
            "uzun_bekleme_orani": (x > 1.5 * med).mean(),   # beklenenin 1,5 katından uzun
        })
    o = aralik.groupby(["hat_id", "rota_yon"]).aralik_dk.apply(metrik).unstack().reset_index()
    adlar = hat_listesi.set_index("HAT_NO").HAT_ADI
    o.insert(1, "hat_adi", o.hat_id.map(adlar))
    return o


def main():
    guzergah, duraklar, hat_listesi = referans_yukle()
    ham = ham_veri_yukle()
    temiz = canli_konumlar(ham)
    temiz["zaman_utc"] = pd.to_datetime(temiz.zaman_utc, utc=True)

    konum = konumlari_isle(temiz, guzergah)
    eslesme = yon_eslestir(konum)
    k = seferlere_bol(konum, eslesme)
    durak_tablo = duraklari_hazirla(duraklar, guzergah, sorted(temiz.hat_id.unique()))
    gecis = durak_gecisleri(k, durak_tablo)
    aralik = sefer_araliklari(gecis)

    os.makedirs(CIKTI, exist_ok=True)
    durak_tablo.to_csv(os.path.join(CIKTI, "duraklar.csv"), index=False)
    gecis.to_csv(os.path.join(CIKTI, "durak_gecisleri.csv"), index=False)
    aralik.to_csv(os.path.join(CIKTI, "sefer_araliklari.csv"), index=False)
    o = ozet(aralik, hat_listesi)
    o.to_csv(os.path.join(CIKTI, "ozet.csv"), index=False)

    print(f"Ham: {len(ham):,} | Temiz: {len(temiz):,} | Guzergahta: {len(k):,} | "
          f"Sefer parcasi: {k.sefer.nunique():,}")
    print("Yon eslesmesi (hat, api_yon) -> (rota_yon, ileri giden oran):")
    for anahtar, deger in sorted(eslesme.items()):
        print(f"  {anahtar} -> {deger}")
    print(f"Durak (hat x yon): {len(durak_tablo)} | Gecis: {len(gecis):,} | Aralik: {len(aralik):,}")
    print("\n" + o.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
