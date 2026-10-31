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
GRI = "#9a998f"       # bağlam serileri (açık ve koyu temada okunur)
YAZI = "#898781"      # grafik içi etiket/çizgi rengi (iki temada da okunur)
MIN_OLCUM_SAAT = 50   # bundan az ölçümü olan saatler gösterilmez
MIN_OLCUM_HAT = 200   # sıralamaya girmek için gereken en az ölçüm (az veriyle sıralama yanıltır)
SIRALAMA_N = 15       # karşılaştırma grafiğinde gösterilen hat-yön sayısı
MIN_MEDYAN_DK = 3     # tipik beklemesi bundan kısa hat-yönler gerçekçi değil (veri/güzergah sorunu); yarışmaz

st.set_page_config(page_title="İzmir otobüs düzenliliği", page_icon="🚌", layout="wide")


# ---------------------------------------------------------------- veri

AY_ADLARI = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
MIN_GUN_BU_AY = 7     # içinde bulunulan ay bundan az gün içeriyorsa varsayılan olarak geçen ay açılır


def surum(*parcalar):
    """Dosyanın değiştirilme zamanı: önbellek anahtarı. Yeni analiz gelince önbellek kendiliğinden yenilenir."""
    try:
        return os.path.getmtime(os.path.join(VERI, *parcalar))
    except OSError:
        return 0


@st.cache_data
def aylari_yukle(surum_anahtari):
    with open(os.path.join(VERI, "aylar.json"), encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(max_entries=6)
def yukle(ay, surum_anahtari):
    klasor = os.path.join(VERI, ay)
    oku = lambda ad: pd.read_csv(os.path.join(klasor, ad))
    with open(os.path.join(klasor, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    ozet = oku("ozet.csv")
    ozet["hat_adi"] = ozet.hat_adi.fillna("")
    ozet["yon_adi"] = ozet.yon_adi.fillna(ozet.rota_yon.map({1: "Gidiş", 2: "Dönüş"}))
    return meta, ozet, oku("saatlik.csv"), oku("durak_ozet.csv"), oku("dagilim.csv")


def yuzde(x):
    return f"%{x * 100:.0f}"


def her_n_de_bir(oran):
    if oran <= 0:
        return "neredeyse hiç"
    n = round(1 / oran)
    return "neredeyse her seferde" if n <= 1 else f"yaklaşık her {n} beklemeden 1'i"


def tarih(iso):
    t = pd.Timestamp(iso).tz_convert("Europe/Istanbul")
    return f"{t.day} {AY_ADLARI[t.month - 1]} {t:%H:%M}"


def ay_adi(ay):
    yil, no = ay.split("-")
    return f"{AY_ADLARI[int(no) - 1]} {yil}"


def ay_etiketi(a):
    if a["tamamlandi"]:
        return ay_adi(a["ay"])
    return f"{ay_adi(a['ay'])} (devam ediyor · {a['gun_sayisi']} gün)"


try:
    aylar = aylari_yukle(surum("aylar.json"))
except FileNotFoundError:
    st.error("Henüz işlenmiş veri yok. Analiz iş akışı ilk kez çalıştığında burası dolacak.")
    st.stop()
if not aylar:
    st.error("Henüz işlenmiş veri yok.")
    st.stop()


# ---------------------------------------------------------------- başlık ve ay seçimi

st.title("İzmir otobüs düzenliliği")
st.markdown(
    "ESHOT'un tüm otobüs hatlarındaki araçların canlı konumları 5 dakikada bir kaydediliyor. "
    "Bu sayfa, otobüslerin duraklara **ne kadar düzenli** geldiğini ölçüyor: durakta tipik olarak "
    "ne kadar beklendiğini, otobüslerin ne sıklıkla üst üste geldiğini ve ne sıklıkla uzun bekleme oluştuğunu."
)

# Ayın ilk günlerinde (az veri) varsayılan olarak tamamlanmış son ay açılır.
varsayilan_ay = 0
if not aylar[0]["tamamlandi"] and aylar[0]["gun_sayisi"] < MIN_GUN_BU_AY and len(aylar) > 1:
    varsayilan_ay = 1
secim = st.selectbox("Dönem", aylar, index=varsayilan_ay, format_func=ay_etiketi)
ay = secim["ay"]
meta, ozet, saatlik, durak, dagilim = yukle(ay, surum(ay, "meta.json"))

st.caption(
    (f"Veri: {tarih(meta['ilk_olcum_utc'])} – {tarih(meta['son_olcum_utc'])} · "
     f"{meta.get('hat_sayisi', ozet.hat_id.nunique())} hat · "
     f"{meta['aralik_olcumu']:,} bekleme ölçümü").replace(",", ".")
    + " · Kaynak: İzmir Büyükşehir Belediyesi Açık Veri Portalı"
)
if not secim["tamamlandi"] and secim["gun_sayisi"] < MIN_GUN_BU_AY:
    st.warning(f"{ay_adi(ay)} için şimdilik yalnızca {secim['gun_sayisi']} günlük veri var. "
               "Sonuçlar veri biriktikçe oturacak.")

# ---------------------------------------------------------------- ayın öne çıkanları

aday = ozet[(ozet.olcum >= MIN_OLCUM_HAT) & (ozet.medyan_dk >= MIN_MEDYAN_DK)]
if len(aday) >= 3:
    st.subheader(f"{ay_adi(ay)}: öne çıkanlar" + ("" if secim["tamamlandi"] else " (şimdilik)"))
    kartlar = [
        ("En dakik", aday.nsmallest(1, "uzun_bekleme_orani").iloc[0],
         lambda r: ("Hiçbir bekleyiş normalden uzun sürmedi" if r.uzun_bekleme_orani < 0.005
                    else f"Bekleyişlerin yalnızca {yuzde(r.uzun_bekleme_orani)}'i uzun sürdü")),
        ("En çok bekleten", aday.nlargest(1, "uzun_bekleme_orani").iloc[0],
         lambda r: f"{her_n_de_bir(r.uzun_bekleme_orani).capitalize()} normalden uzun sürdü"),
        ("El ele gezenler", aday.nlargest(1, "yigilma_orani").iloc[0],
         lambda r: f"Otobüsler {yuzde(r.yigilma_orani)} oranında üst üste geldi"),
    ]
    for kolon, (baslik, r, cumle) in zip(st.columns(3), kartlar):
        with kolon.container(border=True):
            st.caption(baslik)
            st.markdown(f"### {int(r.hat_id)} · {r.yon_adi}")
            st.markdown(f"{r.hat_adi}  \n{cumle(r)}. Tipik bekleme {r.medyan_dk:.0f} dk.")
    st.caption(f"Yalnızca en az {MIN_OLCUM_HAT} ölçümü olan ve tipik beklemesi en az {MIN_MEDYAN_DK} dk "
               "olan hat-yönler yarışıyor. Oranlar her hattın "
               "kendi tipik beklemesine göre hesaplandığı için seyrek ve sık hatlar adil biçimde karşılaştırılıyor.")

st.divider()

# ---------------------------------------------------------------- filtreler

ozet["etiket"] = ozet.hat_id.astype(str) + " · " + ozet.hat_adi
hatlar = ozet.drop_duplicates("hat_id").sort_values("hat_id")
c1, c2 = st.columns([2, 2])
with c1:
    varsayilan = int((hatlar.hat_id == 532).values.argmax()) if (hatlar.hat_id == 532).any() else 0
    hat_etiket = st.selectbox("Hat (numara veya ad yazarak arayabilirsin)", hatlar.etiket.tolist(),
                              index=varsayilan)
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
          help="Hattın aynı saat dilimindeki tipik beklemesinin 1,5 katından uzun süren "
               "aralıkların oranı. Planlı seyrek saatler (gece, akşam) haksız yere cezalandırılmaz.")
k4.metric("Üst üste gelme (yığılma)", yuzde(secili.yigilma_orani),
          help="Aynı saat dilimindeki tipik beklemenin yarısından kısa aralıkların oranı: iki otobüs neredeyse "
               "birlikte geliyor, ardından genelde uzun bir boşluk oluşuyor.")
st.markdown(
    f"**{hat} numaralı hatta, {yon_adi.replace(' yönü', '')} yönünde** durakta "
    f"{her_n_de_bir(secili.uzun_bekleme_orani)} normalden uzun sürüyor."
)

st.divider()

# ---------------------------------------------------------------- hat karşılaştırması

st.subheader(f"En düzensiz {SIRALAMA_N} hat-yön")
st.caption(f"Uzun bekleme oranı: aynı saat dilimindeki tipik beklemenin 1,5 katını aşan bekleyişlerin payı. Yalnızca en az "
           f"{MIN_OLCUM_HAT} ölçümü olan hat-yönler sıralandı. Seçtiğin hat listede yoksa en alta eklenir.")
k = ozet.assign(
    satir=ozet.hat_id.astype(str) + " · " + ozet.yon_adi,
    secili=(ozet.hat_id == hat) & (ozet.rota_yon == yon),
    oran_metin=ozet.uzun_bekleme_orani.map(yuzde),
    medyan_metin=ozet.medyan_dk.round().astype(int).astype(str) + " dk",
)
k = pd.concat([
    k[(k.olcum >= MIN_OLCUM_HAT) & (k.medyan_dk >= MIN_MEDYAN_DK)].nlargest(SIRALAMA_N, "uzun_bekleme_orani"),
    k[k.secili],
]).drop_duplicates(["hat_id", "rota_yon"])
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
                                                                  color=alt.value(YAZI))
st.altair_chart((cubuk + etiket).properties(height=len(k) * 30 + 20), width="stretch")

with st.expander(f"Tüm hatların tablosu ({ozet.hat_id.nunique()} hat)"):
    tablo = ozet[["hat_id", "hat_adi", "yon_adi", "olcum", "medyan_dk", "p90_dk",
                  "uzun_bekleme_orani", "yigilma_orani"]].sort_values("uzun_bekleme_orani", ascending=False)
    st.dataframe(
        tablo.assign(uzun_bekleme_orani=tablo.uzun_bekleme_orani * 100,
                     yigilma_orani=tablo.yigilma_orani * 100),
        hide_index=True, width="stretch", height=420,
        column_config={
            "hat_id": st.column_config.NumberColumn("Hat", format="%d"),
            "hat_adi": "Hat adı",
            "yon_adi": "Yön",
            "olcum": st.column_config.NumberColumn("Ölçüm", format="%d"),
            "medyan_dk": st.column_config.NumberColumn("Tipik bekleme (dk)", format="%.0f"),
            "p90_dk": st.column_config.NumberColumn("10'da 9'u en fazla (dk)", format="%.0f"),
            "uzun_bekleme_orani": st.column_config.NumberColumn("Uzun bekleme %", format="%.0f"),
            "yigilma_orani": st.column_config.NumberColumn("Yığılma %", format="%.0f"),
        })
    st.caption("Sütun başlığına tıklayarak sıralayabilirsin. Az ölçümlü satırlara temkinli bak.")

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
        # Tek renk, büyüklük = uzun bekleme oranı. Renk koyulaştırmak koyu haritada
        # en önemli noktaları kaybettiriyordu; büyüklük iki temada da okunur.
        enk, enb = d.uzun_bekleme_orani.min(), d.uzun_bekleme_orani.max()
        oran = ((d.uzun_bekleme_orani - enk) / (enb - enk if enb > enk else 1)).clip(0, 1)
        d["yaricap_px"] = 4 + 10 * oran
        katman = pdk.Layer(
            "ScatterplotLayer", data=d, get_position="[BOYLAM, ENLEM]",
            get_fill_color=[42, 120, 214, 230], get_line_color=[255, 255, 255],
            line_width_min_pixels=1.5, stroked=True, radius_units="pixels",
            get_radius="yaricap_px", pickable=True)
        st.pydeck_chart(pdk.Deck(
            layers=[katman],
            initial_view_state=pdk.ViewState(latitude=d.ENLEM.mean(), longitude=d.BOYLAM.mean(), zoom=12),
            tooltip={"html": "<b>{DURAK_ADI}</b><br/>Tipik bekleme: {medyan_metin}<br/>"
                             "Uzun bekleme: {oran_metin}"},
            map_style=None,
        ), height=420)
        st.caption("Nokta büyüdükçe o durakta uzun bekleme daha sık.")
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
a = dagilim[(dagilim.hat_id == hat) & (dagilim.rota_yon == yon)].copy()
a = a[a.kutu_dk <= secili.p99_dk]
a["kutu_bit"] = a.kutu_dk + 1.8   # kutular arasında ince boşluk
a["aralik_metin"] = a.kutu_dk.astype(str) + "–" + (a.kutu_dk + 2).astype(str) + " dk"
hist = alt.Chart(a).mark_rect(color=MAVI, cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
    x=alt.X("kutu_dk:Q", title="Ardışık iki otobüs arası (dk)", scale=alt.Scale(domainMin=0)),
    x2="kutu_bit:Q",
    y=alt.Y("adet:Q", title="Ölçüm sayısı"),
    y2=alt.datum(0),
    tooltip=[alt.Tooltip("aralik_metin:N", title="Aralık"), alt.Tooltip("adet:Q", title="Ölçüm")])
cizgi = alt.Chart(pd.DataFrame({"m": [secili.medyan_dk]})).mark_rule(
    strokeDash=[4, 4], color=YAZI, strokeWidth=2).encode(x="m:Q")
st.altair_chart((hist + cizgi).properties(height=260), width="stretch")
st.caption(f"Kesikli çizgi tipik beklemeyi ({secili.medyan_dk:.0f} dk) gösteriyor. Düzenli bir hatta "
           "çubuklar bu çizginin etrafında toplanır; yayıldıkça düzensizlik artar. "
           "En uzun %1'lik kısım grafikte gösterilmiyor.")
st.caption("Durak geçiş anları 5 dakika arayla alınan iki konum arasında oranlanarak bulunduğu "
           "için tek tek aralıklarda 1-2 dakikalık sapma olabilir; oranlar ve medyanlar bundan çok az etkilenir.")

# ---------------------------------------------------------------- yöntem

with st.expander("Bu ölçümler nasıl yapıldı?"):
    st.markdown(f"""
1. **Veri toplama.** İzmir Büyükşehir Belediyesi'nin açık veri API'sinden ESHOT'un tüm hatlarındaki
   otobüslerin konumları 5 dakikada bir çekiliyor (GitHub Actions üzerinde, otomatik). Sonuçlar takvim
   ayı bazında hesaplanıyor: içinde bulunulan ay her gece güncelleniyor, biten aylar ayın 1'inde
   kesinleşiyor. Bu ay için {meta.get('gun_sayisi', '?')} günlük veri kullanıldı. Dakikalık veriyle
   yapılan karşılaştırmada 5 dakikalık örnekleme, tipik bekleme ve uzun bekleme oranında ancak
   birkaç puanlık fark yarattı.
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

**Tanımlar:** *Tipik bekleme* = aralıkların medyanı. *Uzun bekleme* = hattın **aynı saat
dilimindeki** tipik beklemesinin 1,5 katından uzun. *Yığılma* = aynı dilimdeki tipik beklemenin
yarısından kısa (otobüsler üst üste geliyor). Saat dilimleri: gece (00-06), sabah zirvesi (06-10),
gün içi (10-16), akşam zirvesi (16-20), akşam (20-24). Böylece akşam ve gecenin planlı olarak
seyrek seferleri "uzun bekleme" sayılmıyor; ölçü tarifeden değil, düzensizlikten etkileniyor.

**Sınırlar:** Tarifedeki planlanan sefer aralıkları açık veride bulunmadığı için ölçümler
planlanana değil, hattın kendi tipik değerine göre yapılıyor. Hafta içi ve hafta sonu
tarifeleri henüz ayrılmıyor.

Kaynak: [İzmir Büyükşehir Belediyesi Açık Veri Portalı](https://acikveri.bizizmir.com) ·
İzmir Metropolitan Municipality Open Data License · Son güncelleme: {tarih(meta['guncelleme_utc'])}
""")
