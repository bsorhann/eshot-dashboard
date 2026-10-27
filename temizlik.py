"""
Hayalet kayıt temizliği.

Bulgu (27 tur, 5 hat): API her otobüs için canlı konumun yanında, zaman içinde hiç
değişmeyen eski ("donmuş") kayıtlar da döndürüyor ve sıralama bir şey ifade etmiyor.

Kural:
  1. Bir koordinat aynı otobüs için çok sayıda turda birebir tekrar ediyorsa -> donmuş.
  2. Her (tur, hat, otobüs) için donmuş olmayan kaydı tut.
  3. Hiç donmamış kayıt yoksa (otobüs gerçekten bekliyor) -> otobüsün bir önceki
     canlı konumuna en yakın kaydı seç.

Kullanım (rapor):  python temizlik.py
Kullanım (kodda):  from temizlik import canli_konumlar
"""

import sqlite3

import numpy as np
import pandas as pd

GRUP = ["zaman_utc", "hat_id", "otobus_id"]


def donmus_isaretle(df: pd.DataFrame, esik: int = 5) -> pd.DataFrame:
    """Aynı otobüsün aynı koordinatı >= esik farklı turda gördüğü kayıtları işaretler."""
    df = df.copy()
    df["ayni_konum_tur"] = df.groupby(["hat_id", "otobus_id", "koor_x", "koor_y"])[
        "zaman_utc"
    ].transform("nunique")
    df["donmus"] = df["ayni_konum_tur"] >= esik
    return df


def canli_konumlar(df: pd.DataFrame, esik: int = 5) -> pd.DataFrame:
    """Her (tur, hat, otobüs) için tek bir canlı konum döndürür."""
    df = donmus_isaretle(df.dropna(subset=["koor_x", "koor_y"]), esik)
    df["zaman_utc"] = pd.to_datetime(df["zaman_utc"], utc=True)
    df = df.sort_values(GRUP + ["sira"])

    hareketli = df[~df["donmus"]]
    adet = hareketli.groupby(GRUP)["sira"].transform("size")

    # Kesin durum: grupta tam olarak bir hareketli kayıt var (~%96)
    canli = hareketli[adet == 1]

    # Belirsiz gruplar: hiç hareketli kayıt yok (otobüs bekliyor) ya da birden fazla var.
    # Adaylar: varsa hareketli kayıtlar, yoksa tüm kayıtlar. Önceki canlı konuma en yakın olan seçilir.
    belirsiz = df.merge(canli[GRUP], on=GRUP, how="left", indicator=True)
    belirsiz = belirsiz[belirsiz["_merge"] == "left_only"].drop(columns="_merge")
    hareketli_var = belirsiz.groupby(GRUP)["donmus"].transform(lambda s: (~s).any())
    kalan = belirsiz[~hareketli_var | ~belirsiz["donmus"]]
    if not kalan.empty:
        son = canli.sort_values("zaman_utc")[["hat_id", "otobus_id", "zaman_utc", "koor_x", "koor_y"]]
        kalan = kalan.sort_values("zaman_utc")
        kalan = pd.merge_asof(
            kalan, son.rename(columns={"koor_x": "onceki_x", "koor_y": "onceki_y",
                                       "zaman_utc": "onceki_zaman"}),
            left_on="zaman_utc", right_on="onceki_zaman",
            by=["hat_id", "otobus_id"], allow_exact_matches=False,
        )
        kalan["mesafe"] = np.hypot(kalan["koor_x"] - kalan["onceki_x"],
                                   kalan["koor_y"] - kalan["onceki_y"]).fillna(0)
        secilen = kalan.sort_values("mesafe").drop_duplicates(GRUP, keep="first")
        canli = pd.concat([canli, secilen[canli.columns]], ignore_index=True)

    return canli.sort_values(GRUP).reset_index(drop=True)


if __name__ == "__main__":
    c = sqlite3.connect("otobus_konumlari.db")
    ham = pd.read_sql("select * from konumlar", c)
    gps = ham.dropna(subset=["koor_x", "koor_y"])
    isaretli = donmus_isaretle(gps)

    hareketli_sayisi = isaretli.groupby(GRUP)["donmus"].apply(lambda s: (~s).sum())
    print(f"Tur: {ham['zaman_utc'].nunique()} | Ham kayit: {len(ham)} | GPS'li: {len(gps)}")
    print(f"Grup (tur x otobus): {len(hareketli_sayisi)}")
    print("Grup basina hareket eden kayit sayisi dagilimi:")
    print(hareketli_sayisi.value_counts().sort_index().rename("grup sayisi").to_string())

    temiz = canli_konumlar(ham)
    print(f"\nTemiz kayit: {len(temiz)} (ham verinin %{100 * len(temiz) / len(ham):.0f}'i)")
    print("Hat basina tur basina ortalama canli otobus:")
    print(temiz.groupby(["hat_id", "zaman_utc"]).size().groupby("hat_id").mean().round(1).to_string())

    temiz.to_csv("temiz_konumlar.csv", index=False)
    print("\ntemiz_konumlar.csv kaydedildi.")
