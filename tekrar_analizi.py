"""
Hayalet kayıt analizi.

Hipotez: Aynı turda bir otobüs birden fazla kez geliyorsa, yanıttaki İLK kaydı
(en düşük 'sira') canlı konumdur; diğerleri zaman içinde hiç değişmeyen eski kayıtlardır.

Kullanım: python tekrar_analizi.py
"""

import sqlite3

import pandas as pd

c = sqlite3.connect("otobus_konumlari.db")
df = pd.read_sql("select * from konumlar where koor_x is not null", c)

# Her (tur, hat, otobüs) içinde kaydın kaçıncı olduğu: 0 = ilk kayıt
df = df.sort_values(["zaman_utc", "hat_id", "otobus_id", "sira"])
df["kayit_no"] = df.groupby(["zaman_utc", "hat_id", "otobus_id"]).cumcount()

# Bir koordinat (hat, otobüs, x, y) kaç farklı turda birebir aynı tekrar etmiş?
df["ayni_konum_tur"] = df.groupby(["hat_id", "otobus_id", "koor_x", "koor_y"])[
    "zaman_utc"
].transform("nunique")
tur_sayisi = df["zaman_utc"].nunique()
df["donmus"] = df["ayni_konum_tur"] >= max(5, tur_sayisi // 2)

print(f"Tur sayisi: {tur_sayisi}, GPS'li kayit: {len(df)}")
print("\n1) Kayit sirasina gore donmus kayit orani:")
ozet = df.groupby(df["kayit_no"].clip(upper=1))["donmus"].agg(["count", "mean"])
ozet.index = ["Ilk kayit (kayit_no=0)", "Sonraki kayitlar (kayit_no>=1)"][: len(ozet)]
ozet.columns = ["kayit sayisi", "donmus orani"]
print(ozet.to_string(float_format=lambda x: f"{x:.1%}"))

print("\n2) Hipotezle celisen durumlar:")
ilk_donmus = df[(df["kayit_no"] == 0) & df["donmus"]]
sonraki_canli = df[(df["kayit_no"] >= 1) & ~df["donmus"]]
print(f"   Ilk kaydi donmus olan: {len(ilk_donmus)} "
      f"({ilk_donmus[['hat_id', 'otobus_id']].drop_duplicates().shape[0]} otobus)")
print(f"   Sonraki kaydi canli olan: {len(sonraki_canli)} "
      f"({sonraki_canli[['hat_id', 'otobus_id']].drop_duplicates().shape[0]} otobus)")
if len(ilk_donmus):
    print("   Ornek (ilk kaydi donmus; garajda bekleyen otobus olabilir):")
    print(ilk_donmus.drop_duplicates(["hat_id", "otobus_id"]).head(5)
          [["hat_id", "otobus_id", "koor_x", "koor_y", "ayni_konum_tur"]].to_string(index=False))

print("\n3) Donmus kayitlarin toplandigi noktalar (garaj olabilir):")
donmus = df[df["donmus"]].copy()
donmus["nokta"] = donmus["koor_x"].round(3).astype(str) + ", " + donmus["koor_y"].round(3).astype(str)
noktalar = (donmus.groupby("nokta")
            .agg(otobus=("otobus_id", "nunique"), hat=("hat_id", "nunique"))
            .sort_values("otobus", ascending=False).head(8))
print(noktalar.to_string())

print("\n4) Temizlik sonrasi: tur basina hat basina canli otobus sayisi (ortalama):")
temiz = df[df["kayit_no"] == 0]
print(temiz.groupby(["hat_id", "zaman_utc"]).size().groupby("hat_id").mean().round(1).to_string())
