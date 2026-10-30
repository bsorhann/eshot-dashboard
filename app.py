"""
İzmir otobüs düzenliliği dashboard'u (Streamlit).

Veriyi data/islenmis/ klasöründen okur; bu klasörü her gece analiz iş akışı günceller.
Yerelde çalıştırmak için:  streamlit run app.py
"""

import json
import os

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

VERI = os.path.join("data", "islenmis")
MAVI = "#2a78d6"      # vurgulanan seri
GRI = "#c3c2b7"       # bağlam serileri
MIN_OLCUM_SAAT = 50   # bundan az ölçümü olan saatler gösterilmez

st.set_page_config(page_title="İzmir otobüs düzenliliği", page_icon="🚌", layout="wide")


# ---------------------------------------------------------------- veri

@st.cache_data(ttl=3600)
def yukle():
    oku = lambda ad: pd.read_csv(os.path.join(VERI, ad))
    with open(os.path.join(VERI, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    aralik = pd.read_csv(os.path.join(VERI, "sefer_araliklari.csv.gz"),
                         usecols=["hat_id", "rota_yon", "aralik_dk", "uzun", "yigilma"])
    return meta, oku("ozet.csv"), oku("saatlik.csv"), oku("durak_ozet.csv"), aralik


def yuzde(x):
    return f"%{x * 100:.0f}"


def her_n_de_bir(oran):
    if oran <= 0:
        return "neredeyse hiç"
    n = round(1 / oran)
    return "neredeyse her seferde" if n <= 1 else f"yaklaşık her {n} beklemeden 1'i"


def tarih(iso):
    t = pd.Timestamp(iso).tz_convert("Europe/Istanbul")
    aylar = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    return f"{t.day} {aylar[t.month - 1]} {t:%H:%M}"


try:
    meta, ozet, saatlik, durak, aralik = yukle()
except FileNotFoundError:
    st.error("Henüz işlenmiş veri yok. Analiz iş akışı ilk kez çalıştığında burası dolacak.")
    st.stop()


# ---------------------------------------------------------------- başlık

st.title("İzmir otobüs düzenliliği")
st.markdown(
    "ESHOT otobüslerinin canlı konumları her dakika kaydediliyor. Bu sayfa, otobüslerin "
    "duraklara **ne kadar düzenli** geldiğini ölçüyor: durakta tipik olarak ne kadar "
    "beklendiğini, otobüslerin ne sıklıkla üst üste geldiğini ve ne sıklıkla uzun bekleme oluştuğunu."
)
st.caption(
    f"Veri: {tarih(meta['ilk_olcum_utc'])} – {tarih(meta['son_olcum_utc'])} · "
    f"{meta['aralik_olcumu']:,} bekleme ölçümü · Kaynak: İzmir Büyükşehir Belediyesi Açık Veri Portalı"
    .replace(",", ".")
)

# ---------------------------------------------------------------- filtreler

ozet["etiket"] = ozet.hat_id.astype(str) + " · " + ozet.hat_adi
hatlar = ozet.drop_duplicates("hat_id").sort_values("hat_id")
c1, c2 = st.columns([2, 2])
with c1:
    hat_etiket = st.selectbox("Hat", hatlar.etiket.tolist(),
                              index=int((hatlar.hat_id == 532).values.argmax()))
hat = int(hat_etiket.split(" · ")[0])
yonler = ozet[ozet.hat_id == hat].sort_values("rota_yon")
with c2:
    yon_adi = st.radio("Yön", yonler.yon_adi.tolist(), horizontal=True)
yon = int(yonler[yonler.yon_adi == yon_adi].rota_yon.iloc[0])
secili = yonler[yonler.rota_yon == yon].iloc[0]

# ---------------------------------------------------------------- KPI'lar

k1, k2, k3, k4 = st.columns(4)
k1.metric("Tipik bekleme", f"{secili.medyan_dk:.0f} dk",
          help="Ardışık iki otobüs arasındaki sürenin medyanı. Durakta yolcunun en kötü "
               "durumda bekleyeceği tipik süre.")
k2.metric("10 beklemeden 9'u en fazla", f"{secili.p90_dk:.0f} dk",
          help="Aralıkların %90'ı bu süreden kısa.")
k3.metric("Uzun bekleme", yuzde(secili.uzun_bekleme_orani),
          help="Tipik beklemenin 1,5 katından uzun süren aralıkların oranı.")
k4.metric("Üst üste gelme (yığılma)", yuzde(secili.yigilma_orani),
          help="Tipik beklemenin yarısından kısa aralıkların oranı: iki otobüs neredeyse "
               "birlikte geliyor, ardından genelde uzun bir boşluk oluşuyor.")
st.markdown(
    f"**{hat} numaralı hatta, {yon_adi.replace(' yönü', '')} yönünde** durakta "
    f"{her_n_de_bir(secili.uzun_bekleme_orani)} normalden uzun sürüyor."
)

st.divider()

# ---------------------------------------------------------------- hat karşılaştırması

st.subheader("Hatlar arasında karşılaştırma")
st.caption("Uzun bekleme oranı: tipik beklemenin 1,5 katını aşan bekleyişlerin payı. Düşük olan daha iyi.")
k = ozet.assign(
    satir=ozet.hat_id.astype(str) + " · " + ozet.yon_adi,
    secili=(ozet.hat_id == hat) & (ozet.rota_yon == yon),
    oran_metin=ozet.uzun_bekleme_orani.map(yuzde),
    medyan_metin=ozet.medyan_dk.round().astype(int).astype(str) + " dk",
)
taban = alt.Chart(k).encode(
    y=alt.Y("satir:N", sort="-x", title=None, axis=alt.Axis(labelLimit=320)),
    x=alt.X("uzun_bekleme_orani:Q", title="Uzun bekleme oranı",
            axis=alt.Axis(format="%", grid=True, tickCount=5)),
)
cubuk = taban.mark_bar(cornerRadiusEnd=4, height=18).encode(
    color=alt.condition("datum.secili", alt.value(MAVI), alt.value(GRI)),
    tooltip=[alt.Tooltip("satir:N", title="Hat · yön"),
             alt.Tooltip("oran_metin:N", title="Uzun bekleme"),
             alt.Tooltip("medyan_metin:N", title="Tipik bekleme"),
             alt.Tooltip("olcum:Q", title="Ölçüm", format=",")],
)
etiket = taban.mark_text(align="left", dx=4, fontSize=12).encode(text="oran_metin:N",
                                                                  color=alt.value("#52514e"))
st.altair_chart((cubuk + etiket).properties(height=len(k) * 30 + 20), width="stretch")

st.divider()

# ---------------------------------------------------------------- saatlere göre

st.subheader("Günün saatine göre")
s = saatlik[(saatlik.hat_id == hat) & (saatlik.rota_yon == yon) & (saatlik.olcum >= MIN_OLCUM_SAAT)].copy()
if s.empty:
    st.info("Bu hat ve yön için saatlik analiz yapacak kadar veri yok.")
else:
    s["saat_etiket"] = s.saat.map(lambda h: f"{h:02d}:00")
    s["oran_metin"] = s.uzun_bekleme_orani.map(yuzde)
    s["medyan_metin"] = s.medyan_dk.round().astype(int).astype(str) + " dk"
    ipucu = [alt.Tooltip("saat_etiket:N", title="Saat"),
             alt.Tooltip("medyan_metin:N", title="Tipik bekleme"),
             alt.Tooltip("oran_metin:N", title="Uzun bekleme"),
             alt.Tooltip("olcum:Q", title="Ölçüm")]
    x = alt.X("saat:O", title="Saat", axis=alt.Axis(labelAngle=0))
    sol, sag = st.columns(2)
    with sol:
        st.markdown("**Tipik bekleme (dk)**")
        st.altair_chart(
            alt.Chart(s).mark_line(point=alt.OverlayMarkDef(size=60, filled=True),
                                   strokeWidth=2, color=MAVI)
            .encode(x=x, y=alt.Y("medyan_dk:Q", title=None, scale=alt.Scale(zero=True)), tooltip=ipucu)
            .properties(height=260), width="stretch")
    with sag:
        st.markdown("**Uzun bekleme oranı**")
        st.altair_chart(
            alt.Chart(s).mark_bar(cornerRadiusEnd=4, color=MAVI, size=16)
            .encode(x=x, y=alt.Y("uzun_bekleme_orani:Q", title=None, axis=alt.Axis(format="%")),
                    tooltip=ipucu)
            .properties(height=260), width="stretch")
    en_kotu = s.loc[s.uzun_bekleme_orani.idxmax()]
    st.caption(f"En sorunlu saat: **{int(en_kotu.saat):02d}:00** — bu saatte bekleyişlerin "
               f"{yuzde(en_kotu.uzun_bekleme_orani)}'i uzun sürüyor. "
               f"{MIN_OLCUM_SAAT}'den az ölçümü olan saatler gösterilmiyor.")

st.divider()

# ---------------------------------------------------------------- duraklar

st.subheader("Güzergah boyunca")
d = durak[(durak.hat_id == hat) & (durak.rota_yon == yon)].dropna(subset=["ENLEM"]).copy()
if d.empty:
    st.info("Bu hat ve yön için durak verisi yok.")
else:
    d = d.sort_values("durak_sira")
    d["oran_metin"] = d.uzun_bekleme_orani.map(yuzde)
    d["medyan_metin"] = d.medyan_dk.round().astype(int).astype(str) + " dk"
    sol, sag = st.columns([3, 2])
    with sol:
        # sıralı tek renk: açık = iyi, koyu = çok uzun bekleme
        enk, enb = d.uzun_bekleme_orani.min(), d.uzun_bekleme_orani.max()
        oran = ((d.uzun_bekleme_orani - enk) / (enb - enk if enb > enk else 1)).clip(0, 1)
        acik, koyu = (181, 212, 244), (12, 68, 124)
        d["renk"] = [[int(a + (b - a) * o) for a, b in zip(acik, koyu)] + [230] for o in oran]
        katman = pdk.Layer(
            "ScatterplotLayer", data=d, get_position="[BOYLAM, ENLEM]", get_fill_color="renk",
            get_line_color=[255, 255, 255], line_width_min_pixels=1.5, stroked=True,
            get_radius=55, radius_min_pixels=5, radius_max_pixels=12, pickable=True)
        st.pydeck_chart(pdk.Deck(
            layers=[katman],
            initial_view_state=pdk.ViewState(latitude=d.ENLEM.mean(), longitude=d.BOYLAM.mean(), zoom=12),
            tooltip={"html": "<b>{DURAK_ADI}</b><br/>Tipik bekleme: {medyan_metin}<br/>"
                             "Uzun bekleme: {oran_metin}"},
            map_style=None,
        ), height=420)
        st.caption("Koyu mavi duraklarda uzun bekleme daha sık.")
    with sag:
        st.markdown("**Durak sırasına göre uzun bekleme**")
        st.altair_chart(
            alt.Chart(d).mark_line(strokeWidth=2, color=MAVI,
                                   point=alt.OverlayMarkDef(size=40, filled=True))
            .encode(x=alt.X("durak_sira:Q", title="Durak sırası (başlangıçtan)"),
                    y=alt.Y("uzun_bekleme_orani:Q", title=None, axis=alt.Axis(format="%")),
                    tooltip=[alt.Tooltip("DURAK_ADI:N", title="Durak"),
                             alt.Tooltip("medyan_metin:N", title="Tipik bekleme"),
                             alt.Tooltip("oran_metin:N", title="Uzun bekleme")])
            .properties(height=380), width="stretch")
        st.caption("Çizgi yükseliyorsa düzensizlik yol boyunca birikiyor demektir.")

st.divider()

# ---------------------------------------------------------------- dağılım

st.subheader("Bekleme sürelerinin dağılımı")
a = aralik[(aralik.hat_id == hat) & (aralik.rota_yon == yon)]
ust = float(a.aralik_dk.quantile(0.99))
a = a[a.aralik_dk <= ust]
hist = alt.Chart(a).mark_bar(color=MAVI, cornerRadiusEnd=2).encode(
    x=alt.X("aralik_dk:Q", bin=alt.Bin(step=2), title="Ardışık iki otobüs arası (dk)"),
    y=alt.Y("count():Q", title="Ölçüm sayısı"),
    tooltip=[alt.Tooltip("aralik_dk:Q", bin=alt.Bin(step=2), title="Aralık (dk)"),
             alt.Tooltip("count():Q", title="Ölçüm")])
cizgi = alt.Chart(pd.DataFrame({"m": [secili.medyan_dk]})).mark_rule(
    strokeDash=[4, 4], color="#52514e", strokeWidth=1.5).encode(x="m:Q")
st.altair_chart((hist + cizgi).properties(height=260), width="stretch")
st.caption(f"Kesikli çizgi tipik beklemeyi ({secili.medyan_dk:.0f} dk) gösteriyor. Düzenli bir hatta "
           "çubuklar bu çizginin etrafında toplanır; yayıldıkça düzensizlik artar. "
           "En uzun %1'lik kısım grafikte gösterilmiyor.")

# ---------------------------------------------------------------- yöntem

with st.expander("Bu ölçümler nasıl yapıldı?"):
    st.markdown(f"""
1. **Veri toplama.** İzmir Büyükşehir Belediyesi'nin açık veri API'sinden seçilen hatlardaki
   otobüslerin konumları her dakika çekiliyor (GitHub Actions üzerinde, otomatik).
2. **Hayalet kayıt temizliği.** API, otobüslerin canlı konumunun yanında garaj, aktarma merkezi
   ve benzinlik gibi yerlerde kalmış eski konumlarını da döndürüyor. GPS'li kayıtların
   **{yuzde(meta['hayalet_orani'])}'i** bu tür hayalet kayıt; zaman içinde hiç değişmeyen
   koordinatlar ayıklanıyor.
3. **Güzergaha oturtma.** Her konum, hattın resmi güzergahı üzerine izdüşürülüyor ve
   "başlangıçtan kaç metre" bilgisine çevriliyor.
4. **Durak geçişi.** Otobüsün bir durağın önünden geçtiği an, iki ölçüm arasında oranlanarak bulunuyor.
5. **Bekleme (sefer aralığı).** Aynı durağa art arda gelen iki otobüs arasındaki süre. Veri
   toplanamayan dönemlere (gece, kesinti) denk gelen aralıklar ve hat uçlarındaki güvenilmez
   duraklar hesaba katılmıyor.

**Tanımlar:** *Tipik bekleme* = aralıkların medyanı. *Uzun bekleme* = tipik beklemenin 1,5 katından
uzun. *Yığılma* = tipik beklemenin yarısından kısa (otobüsler üst üste geliyor).

**Sınırlar:** Tarifedeki planlanan sefer aralıkları açık veride bulunmadığı için ölçümler
planlanana değil, hattın kendi tipik değerine göre yapılıyor. Akşam ve hafta sonu sefer
sıklığı değiştiğinden saat bazındaki grafikler daha doğru bir tablo verir.

Kaynak: [İzmir Büyükşehir Belediyesi Açık Veri Portalı](https://acikveri.bizizmir.com) ·
İzmir Metropolitan Municipality Open Data License · Son güncelleme: {tarih(meta['guncelleme_utc'])}
""")
