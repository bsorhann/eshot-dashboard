"""Toplanan verinin hızlı özeti. Kullanım: python kontrol.py"""

import sqlite3

c = sqlite3.connect("otobus_konumlari.db")

print("Toplam kayit:", c.execute("select count(*) from konumlar").fetchone()[0])
print("Ilk / son kayit zamani:", c.execute("select min(zaman_utc), max(zaman_utc) from konumlar").fetchone())
print("Tur sayisi:", c.execute("select count(distinct zaman_utc) from konumlar").fetchone()[0])
print("Hat basina otobus sayisi:", c.execute(
    "select hat_id, count(distinct otobus_id) from konumlar group by hat_id").fetchall())
print("Yon degerleri:", c.execute("select distinct yon from konumlar").fetchall())
print("Koordinat tipi:", c.execute("select distinct typeof(koor_x) from konumlar").fetchall())
print("GPS'siz kayit (bos koordinat):", c.execute(
    "select count(*) from konumlar where koor_x is null").fetchone()[0])

print("\nOrnek kayitlar:")
for r in c.execute("select * from konumlar limit 5"):
    print(r)

# --- Ayni turda ayni otobus birden fazla kez geliyor mu? ---
print("\nAyni turda tekrar eden otobusler (ilk 10):")
tekrar = c.execute("""
    select zaman_utc, hat_id, otobus_id, count(*) as adet
    from konumlar
    group by zaman_utc, hat_id, otobus_id
    having count(*) > 1
    limit 10
""").fetchall()
for r in tekrar:
    print(r)
if not tekrar:
    print("Tekrar yok.")
else:
    z, h, o, _ = tekrar[0]
    print(f"\nOrnek: {z} turunda hat {h}, otobus {o} icin tum kayitlar:")
    for r in c.execute(
        "select * from konumlar where zaman_utc=? and hat_id=? and otobus_id=?", (z, h, o)
    ):
        print(r)
    print(f"\nAyni otobusun ({o}) zaman icindeki hareketi:")
    for r in c.execute(
        "select zaman_utc, yon, koor_x, koor_y, sira from konumlar "
        "where hat_id=? and otobus_id=? order by zaman_utc, sira limit 12", (h, o)
    ):
        print(r)

print("\nHatalar:")
for r in c.execute("select * from hatalar limit 10"):
    print(r)
