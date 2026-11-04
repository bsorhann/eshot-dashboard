"""
İzmir otobüs düzenliliği dashboard'u (Streamlit).

Veriyi data/islenmis/ klasöründen okur; bu klasörü her gece analiz iş akışı günceller.
Sayfa Türkçe ve İngilizce gösterilebilir; linke ?lang=en eklenirse doğrudan İngilizce açılır.
Yerelde çalıştırmak için:  streamlit run app.py
"""

import glob
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

# ---------------------------------------------------------------- dil

EN = st.query_params.get("lang", "tr").lower() == "en"


def m(tr, en):
    """Seçili dile göre metin: m("Türkçe", "English")."""
    return en if EN else tr


st.set_page_config(page_title=m("İzmir otobüs düzenliliği", "İzmir bus regularity"),
                   page_icon="🚌", layout="wide")


# ---------------------------------------------------------------- veri

AY_ADLARI_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
                "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
AY_ADLARI_EN = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
AY_ADLARI = AY_ADLARI_EN if EN else AY_ADLARI_TR
MIN_GUN_BU_AY = 7     # içinde bulunulan ay bundan az gün içeriyorsa varsayılan olarak geçen ay açılır
DK = m("dk", "min")


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


def yon_cevir(yon_adi):
    """'Konak yönü' -> 'towards Konak' (İngilizcede). Yer adları Türkçe kalır."""
    if not EN:
        return yon_adi
    if yon_adi.endswith(" yönü"):
        return "towards " + yon_adi[: -len(" yönü")]
    return {"Gidiş": "Outbound", "Dönüş": "Inbound"}.get(yon_adi, yon_adi)


def yuzde(x):
    return f"{x * 100:.0f}%" if EN else f"%{x * 100:.0f}"


def sayi(n):
    """Binlik ayırıcı: Türkçede nokta, İngilizcede virgül."""
    s = f"{int(n):,}"
    return s if EN else s.replace(",", ".")


def her_n_de_bir(oran):
    if oran <= 0:
        return m("neredeyse hiç", "almost none of the waits")
    n = round(1 / oran)
    if n <= 1:
        return m("neredeyse her seferde", "almost all waits")
    return m(f"yaklaşık her {n} beklemeden 1'i", f"about 1 in {n} waits")


def tarih(iso):
    t = pd.Timestamp(iso).tz_convert("Europe/Istanbul")
    return f"{t.day} {AY_ADLARI[t.month - 1]} {t:%H:%M}"


def tr_baslik(metin):
    """'BOSTANLI İSKELE' -> 'Bostanlı İskele' (Türkçe I/İ kurallarıyla)."""
    kelimeler = []
    for k in str(metin).strip().split():
        kucuk = k.replace("I", "ı").replace("İ", "i").lower()
        kelimeler.append({"i": "İ", "ı": "I"}.get(kucuk[:1], kucuk[:1].upper()) + kucuk[1:])
    return " ".join(kelimeler)


REF_HAT_DESENI = os.path.join("data", "ref", "eshot-otobus-hat-listesi__*.csv")


def surum_ref():
    dosyalar = glob.glob(REF_HAT_DESENI)
    return os.path.getmtime(dosyalar[0]) if dosyalar else 0


@st.cache_data
def hat_listesini_yukle(surum_anahtari):
    """ESHOT'un resmi hat listesi (ölçülemeyen hatları göstermek için)."""
    dosyalar = glob.glob(REF_HAT_DESENI)
    if not dosyalar:
        return pd.DataFrame(columns=["hat_id", "hat_adi"])
    h = pd.read_csv(dosyalar[0], sep=";").drop_duplicates("HAT_NO")
    adlar = h.HAT_ADI.astype(str).str.split(r"\s*-\s*", regex=True).map(
        lambda p: " – ".join(tr_baslik(x) for x in p if x))
    return pd.DataFrame({"hat_id": h.HAT_NO.astype(int).values, "hat_adi": adlar.values})


def kadraj_zoom(d, genislik_px=600, yukseklik_px=400):
    """Güzergahın tamamı haritaya sığacak yakınlaştırma seviyesi (Web Mercator yaklaşımı)."""
    import math
    lon_aralik = max(d.BOYLAM.max() - d.BOYLAM.min(), 0.005)
    lat_aralik = max(d.ENLEM.max() - d.ENLEM.min(), 0.005) / math.cos(math.radians(d.ENLEM.mean()))
    # deck.gl dünyayı zoom 0'da 512 piksel kabul eder (256 değil).
    zoom = min(math.log2(360 * genislik_px / 512 / lon_aralik),
               math.log2(360 * yukseklik_px / 512 / lat_aralik)) - 0.3  # kenarlarda pay
    return max(8.0, min(15.0, zoom))


def ay_adi(ay):
    yil, no = ay.split("-")
    return f"{AY_ADLARI[int(no) - 1]} {yil}"


def ay_etiketi(a):
    if a["tamamlandi"]:
        return ay_adi(a["ay"])
    return m(f"{ay_adi(a['ay'])} (devam ediyor · {a['gun_sayisi']} gün)",
             f"{ay_adi(a['ay'])} (in progress · {a['gun_sayisi']} days)")


# ---------------------------------------------------------------- başlık ve dil seçimi

bas, dil_kolon = st.columns([6, 1], vertical_alignment="center")
with dil_kolon:
    dil = st.radio("Dil / Language", ["TR", "EN"], index=1 if EN else 0, horizontal=True,
                   label_visibility="collapsed")
if (dil == "EN") != EN:
    st.query_params["lang"] = dil.lower()
    st.rerun()

with bas:
    st.title(m("İzmir otobüs düzenliliği", "İzmir bus regularity"))

try:
    aylar = aylari_yukle(surum("aylar.json"))
except FileNotFoundError:
    aylar = []
if not aylar:
    st.error(m("Henüz işlenmiş veri yok. Analiz iş akışı ilk kez çalıştığında burası dolacak.",
               "No processed data yet. This page fills up after the analysis workflow runs for the first time."))
    st.stop()

st.markdown(m(
    "ESHOT'un tüm otobüs hatlarındaki araçların canlı konumları 5 dakikada bir kaydediliyor. "
    "Bu sayfa, otobüslerin duraklara **ne kadar düzenli** geldiğini ölçüyor: durakta tipik olarak "
    "ne kadar beklendiğini, otobüslerin ne sıklıkla üst üste geldiğini ve ne sıklıkla uzun bekleme oluştuğunu.",
    "The live position of every bus on all ESHOT lines (İzmir's municipal bus operator) is recorded "
    "every 5 minutes. This page measures **how regularly** buses arrive at their stops: how long people "
    "typically wait, how often buses bunch together and how often unusually long waits occur."
))

# Ayın ilk günlerinde (az veri) varsayılan olarak tamamlanmış son ay açılır.
varsayilan_ay = 0
if not aylar[0]["tamamlandi"] and aylar[0]["gun_sayisi"] < MIN_GUN_BU_AY and len(aylar) > 1:
    varsayilan_ay = 1
secim = st.selectbox(m("Dönem", "Period"), aylar, index=varsayilan_ay, format_func=ay_etiketi)
ay = secim["ay"]
meta, ozet, saatlik, durak, dagilim = yukle(ay, surum(ay, "meta.json"))
ozet["yon_adi"] = ozet.yon_adi.map(yon_cevir)

st.caption(
    m(f"Veri: {tarih(meta['ilk_olcum_utc'])} – {tarih(meta['son_olcum_utc'])} · "
      f"{meta.get('hat_sayisi', ozet.hat_id.nunique())} hat · "
      f"{sayi(meta['aralik_olcumu'])} bekleme ölçümü · "
      "Kaynak: İzmir Büyükşehir Belediyesi Açık Veri Portalı",
      f"Data: {tarih(meta['ilk_olcum_utc'])} – {tarih(meta['son_olcum_utc'])} · "
      f"{meta.get('hat_sayisi', ozet.hat_id.nunique())} lines · "
      f"{sayi(meta['aralik_olcumu'])} measured waits · "
      "Source: İzmir Metropolitan Municipality Open Data Portal")
)
if not secim["tamamlandi"] and secim["gun_sayisi"] < MIN_GUN_BU_AY:
    st.warning(m(f"{ay_adi(ay)} için şimdilik yalnızca {secim['gun_sayisi']} günlük veri var. "
                 "Sonuçlar veri biriktikçe oturacak.",
                 f"{ay_adi(ay)} has only {secim['gun_sayisi']} days of data so far. "
                 "Results will settle as more data comes in."))

# ---------------------------------------------------------------- sonuçları nasıl okumalı

tum_hat_listesi = hat_listesini_yukle(surum_ref())
olculen = set(ozet.hat_id)
olculemeyen = tum_hat_listesi[~tum_hat_listesi.hat_id.isin(olculen)].sort_values("hat_id")
n_tum, n_olculen = len(tum_hat_listesi), len(olculen & set(tum_hat_listesi.hat_id))
with st.expander(m(f"Sonuçları nasıl okumalı? (ESHOT'un {n_tum} hattından {n_olculen}'i bu ay ölçüldü)",
                   f"How to read the results ({n_olculen} of ESHOT's {n_tum} lines measured this month)")):
    st.markdown(m(f"""
**Neden {n_tum} hattın hepsi yok?** Toplayıcı tüm hatları düzenli olarak soruyor. Bir hattın
ölçülebilmesi için aynı durağa art arda en az iki sefer gelmesi gerekiyor. Ölçülemeyen
{len(olculemeyen)} hattın çoğu günde yalnızca birkaç sefer yapan ilçe ve köy hatları (Bayındır, Bergama,
Ödemiş, Seferihisar gibi). Bazıları ise yalnızca belirli günlerde, saatlerde ya da dönemlerde
çalışan hatlar (okul, mesai, sezon). Veri biriktikçe bu sayı azalır.

**Tek otobüslü hatlar.** Bazı hatlarda tek otobüs gidip geliyor. Bu hatlarda bekleme süresi, otobüsün
tam bir turu kadar uzun olabilir; bu yolcunun gerçekte yaşadığı beklemedir.

**"Uzun bekleme" neye göre?** Her bekleme, aynı hattın aynı saat dilimindeki tipik beklemesiyle
karşılaştırılıyor. Bu yüzden akşam ve gecenin planlı olarak seyrek seferleri "uzun" sayılmıyor.
Yani bu ölçü bir hattın sık mı seyrek mi olduğunu değil, **ne kadar öngörülebilir** olduğunu gösterir.
Uzun bekleme oranı %0 olan bir hat seyrek olabilir, ama tarifesine sadıktır.

**Sanayi ve vardiya hatları.** Organize sanayi bölgesi gibi vardiya saatlerine göre çalışan hatlarda
seferler gün içinde bilerek düzensiz planlanır. Bu hatların yüksek oranları kötü hizmetten değil,
tarife yapısından kaynaklanabilir.

**Bir otobüs seferden çekilince.** İki otobüslü bir hatta otobüslerden biri seferden çekildiğinde
(mola, arıza), aynı otobüs art arda gelir ve bekleme iki katına çıkar. Bu beklemeler de sayılıyor.

**Az ölçümlü satırlar.** Tablodaki "Ölçüm" sütunu düşük olan satırlar şansa bağlı olabilir.
Öne çıkanlar ve sıralama grafiği bu yüzden yalnızca en az {MIN_OLCUM_HAT} ölçümü olan hat-yönleri içeriyor.

**Ham verinin yaklaşık yarısı ayıklanıyor.** API, otobüslerin canlı konumunun yanında garaj, aktarma
merkezi ya da benzinlikte kalmış eski konumlarını da döndürüyor; bunlar analizden önce temizleniyor.
""", f"""
**Why aren't all {n_tum} lines here?** The collector polls every line regularly, but a line can only be
measured when at least two trips pass the same stop one after the other. Most of the {len(olculemeyen)}
unmeasured lines are district and village lines with only a few trips a day (Bayındır, Bergama, Ödemiş,
Seferihisar and others). Some only run on certain days, hours or seasons (school, shift or summer
lines). The number goes down as data accumulates.

**Single-bus lines.** Some lines are served by one bus going back and forth. The wait there can be as
long as a full round trip; that is the wait passengers actually experience.

**"Long wait" compared to what?** Every wait is compared with the typical wait of the same line in the
same time band. Planned sparse service in the evening and at night is therefore not counted as "long".
In other words, this measure shows how **predictable** a line is, not how frequent. A line with a 0%
long-wait share may run rarely, but it sticks to its rhythm.

**Industrial and shift lines.** Lines that serve industrial zones follow shift times, so their trips
are deliberately uneven during the day. High shares on these lines can come from the timetable, not
from poor service.

**When a bus is taken out of service.** On a two-bus line, if one bus leaves service (a break, a
breakdown), the same bus arrives twice in a row and the wait doubles. These waits are counted too.

**Rows with few measurements.** Rows with a low "Measurements" value in the table can be down to chance.
That is why the highlights and the ranking only include line-directions with at least {MIN_OLCUM_HAT}
measurements.

**About half of the raw data is filtered out.** Besides each bus's live position, the API returns old
positions left behind at garages, a transfer hub or a petrol station. These are removed before the analysis.

Line, stop and place names are kept in Turkish, as they appear on the buses and at the stops.
"""))
    if len(olculemeyen):
        st.markdown(m(f"**Bu ay ölçülemeyen {len(olculemeyen)} hat**",
                      f"**{len(olculemeyen)} lines not measured this month**"))
        st.dataframe(olculemeyen, hide_index=True, width="stretch", height=240,
                     column_config={"hat_id": st.column_config.NumberColumn(m("Hat", "Line"), format="%d"),
                                    "hat_adi": m("Hat adı", "Line name")})

# ---------------------------------------------------------------- ayın öne çıkanları

aday = ozet[(ozet.olcum >= MIN_OLCUM_HAT) & (ozet.medyan_dk >= MIN_MEDYAN_DK)]
if len(aday) >= 3:
    st.subheader(m(f"{ay_adi(ay)}: öne çıkanlar", f"{ay_adi(ay)}: highlights")
                 + ("" if secim["tamamlandi"] else m(" (şimdilik)", " (so far)")))

    def dakik_cumle(r):
        if r.uzun_bekleme_orani < 0.005:
            return m("Hiçbir bekleyiş normalden uzun sürmedi", "No wait was longer than usual")
        return m(f"Bekleyişlerin yalnızca {yuzde(r.uzun_bekleme_orani)}'i uzun sürdü",
                 f"Only {yuzde(r.uzun_bekleme_orani)} of waits were long")

    kartlar = [
        (m("En dakik", "Most punctual"), aday.nsmallest(1, "uzun_bekleme_orani").iloc[0], dakik_cumle),
        (m("En çok bekleten", "Most long waits"), aday.nlargest(1, "uzun_bekleme_orani").iloc[0],
         lambda r: m(f"{her_n_de_bir(r.uzun_bekleme_orani).capitalize()} normalden uzun sürdü",
                     f"{her_n_de_bir(r.uzun_bekleme_orani).capitalize()} were longer than usual")),
        (m("El ele gezenler", "Most bunching"), aday.nlargest(1, "yigilma_orani").iloc[0],
         lambda r: m(f"Otobüsler {yuzde(r.yigilma_orani)} oranında üst üste geldi",
                     f"Buses arrived bunched together {yuzde(r.yigilma_orani)} of the time")),
    ]
    for kolon, (baslik, r, cumle) in zip(st.columns(3), kartlar):
        with kolon.container(border=True):
            st.caption(baslik)
            st.markdown(f"### {int(r.hat_id)} · {r.yon_adi}")
            st.markdown(f"{r.hat_adi}  \n{cumle(r)}. "
                        + m(f"Tipik bekleme {r.medyan_dk:.0f} dk.", f"Typical wait {r.medyan_dk:.0f} min."))
    st.caption(m(f"Yalnızca en az {MIN_OLCUM_HAT} ölçümü olan ve tipik beklemesi en az {MIN_MEDYAN_DK} dk "
                 "olan hat-yönler yarışıyor. Oranlar her hattın kendi tipik beklemesine göre hesaplandığı "
                 "için seyrek ve sık hatlar adil biçimde karşılaştırılıyor.",
                 f"Only line-directions with at least {MIN_OLCUM_HAT} measurements and a typical wait of at "
                 f"least {MIN_MEDYAN_DK} min compete. Shares are calculated against each line's own typical "
                 "wait, so frequent and infrequent lines are compared fairly."))

st.divider()

# ---------------------------------------------------------------- filtreler

ozet["etiket"] = ozet.hat_id.astype(str) + " · " + ozet.hat_adi
hatlar = ozet.drop_duplicates("hat_id").sort_values("hat_id")
c1, c2 = st.columns([2, 2])
with c1:
    varsayilan = int((hatlar.hat_id == 532).values.argmax()) if (hatlar.hat_id == 532).any() else 0
    hat_etiket = st.selectbox(m("Hat (numara veya ad yazarak arayabilirsin)",
                                "Line (type a number or name to search)"),
                              hatlar.etiket.tolist(), index=varsayilan)
hat = int(hat_etiket.split(" · ")[0])
yonler = ozet[ozet.hat_id == hat].sort_values("rota_yon")
with c2:
    yon_adi = st.radio(m("Yön", "Direction"), yonler.yon_adi.tolist(), horizontal=True)
yon = int(yonler[yonler.yon_adi == yon_adi].rota_yon.iloc[0])
secili = yonler[yonler.rota_yon == yon].iloc[0]

# ---------------------------------------------------------------- KPI'lar

k1, k2, k3, k4 = st.columns(4)
k1.metric(m("Tipik bekleme", "Typical wait"), f"{secili.medyan_dk:.0f} {DK}",
          help=m("Ardışık iki otobüs arasındaki sürenin medyanı. Durakta yolcunun en kötü "
                 "durumda bekleyeceği tipik süre.",
                 "Median time between two consecutive buses: the typical longest wait "
                 "a passenger can face at a stop."))
k2.metric(m("10 beklemeden 9'u en fazla", "9 in 10 waits are at most"), f"{secili.p90_dk:.0f} {DK}",
          help=m("Aralıkların %90'ı bu süreden kısa.", "90% of gaps are shorter than this."))
k3.metric(m("Uzun bekleme", "Long waits"), yuzde(secili.uzun_bekleme_orani),
          help=m("Hattın aynı saat dilimindeki tipik beklemesinin 1,5 katından uzun süren "
                 "aralıkların oranı. Planlı seyrek saatler (gece, akşam) haksız yere cezalandırılmaz.",
                 "Share of gaps longer than 1.5× the line's typical wait in the same time band. "
                 "Planned sparse hours (night, evening) are not penalised."))
k4.metric(m("Üst üste gelme (yığılma)", "Bunching"), yuzde(secili.yigilma_orani),
          help=m("Aynı saat dilimindeki tipik beklemenin yarısından kısa aralıkların oranı: iki otobüs neredeyse "
                 "birlikte geliyor, ardından genelde uzun bir boşluk oluşuyor.",
                 "Share of gaps shorter than half the typical wait in the same time band: two buses arrive "
                 "almost together, usually followed by a long gap."))
st.markdown(m(
    f"**{hat} numaralı hatta, {yon_adi.replace(' yönü', '')} yönünde** durakta "
    f"{her_n_de_bir(secili.uzun_bekleme_orani)} normalden uzun sürüyor.",
    f"**On line {hat}, {yon_adi}**, {her_n_de_bir(secili.uzun_bekleme_orani)} at a stop "
    "take longer than usual."
))

st.divider()

# ---------------------------------------------------------------- hat karşılaştırması

st.subheader(m(f"En düzensiz {SIRALAMA_N} hat-yön", f"The {SIRALAMA_N} least regular line-directions"))
st.caption(m(f"Uzun bekleme oranı: aynı saat dilimindeki tipik beklemenin 1,5 katını aşan bekleyişlerin payı. "
             f"Yalnızca en az {MIN_OLCUM_HAT} ölçümü olan hat-yönler sıralandı. Seçtiğin hat listede yoksa "
             "en alta eklenir.",
             f"Long-wait share: the share of waits longer than 1.5× the typical wait in the same time band. "
             f"Only line-directions with at least {MIN_OLCUM_HAT} measurements are ranked. If your selected "
             "line is not in the list, it is added at the bottom."))
k = ozet.assign(
    satir=ozet.hat_id.astype(str) + " · " + ozet.yon_adi,
    secili=(ozet.hat_id == hat) & (ozet.rota_yon == yon),
    oran_metin=ozet.uzun_bekleme_orani.map(yuzde),
    medyan_metin=ozet.medyan_dk.round().astype(int).astype(str) + f" {DK}",
)
k = pd.concat([
    k[(k.olcum >= MIN_OLCUM_HAT) & (k.medyan_dk >= MIN_MEDYAN_DK)].nlargest(SIRALAMA_N, "uzun_bekleme_orani"),
    k[k.secili],
]).drop_duplicates(["hat_id", "rota_yon"])
taban = alt.Chart(k).encode(
    y=alt.Y("satir:N", sort="-x", title=None, axis=alt.Axis(labelLimit=320)),
    x=alt.X("uzun_bekleme_orani:Q", title=m("Uzun bekleme oranı", "Long-wait share"),
            axis=alt.Axis(format="%", grid=True, tickCount=5)),
)
cubuk = taban.mark_bar(cornerRadiusEnd=4, height=18).encode(
    color=alt.condition("datum.secili", alt.value(MAVI), alt.value(GRI)),
    tooltip=[alt.Tooltip("satir:N", title=m("Hat · yön", "Line · direction")),
             alt.Tooltip("oran_metin:N", title=m("Uzun bekleme", "Long waits")),
             alt.Tooltip("medyan_metin:N", title=m("Tipik bekleme", "Typical wait")),
             alt.Tooltip("olcum:Q", title=m("Ölçüm", "Measurements"), format=",")],
)
etiket = taban.mark_text(align="left", dx=4, fontSize=12).encode(text="oran_metin:N",
                                                                  color=alt.value(YAZI))
st.altair_chart((cubuk + etiket).properties(height=len(k) * 30 + 20), width="stretch")

with st.expander(m(f"Tüm hatların tablosu ({ozet.hat_id.nunique()} hat)",
                   f"Table of all lines ({ozet.hat_id.nunique()} lines)")):
    tablo = ozet[["hat_id", "hat_adi", "yon_adi", "olcum", "medyan_dk", "p90_dk",
                  "uzun_bekleme_orani", "yigilma_orani"]].sort_values("uzun_bekleme_orani", ascending=False)
    st.dataframe(
        tablo.assign(uzun_bekleme_orani=tablo.uzun_bekleme_orani * 100,
                     yigilma_orani=tablo.yigilma_orani * 100),
        hide_index=True, width="stretch", height=420,
        column_config={
            "hat_id": st.column_config.NumberColumn(m("Hat", "Line"), format="%d"),
            "hat_adi": m("Hat adı", "Line name"),
            "yon_adi": m("Yön", "Direction"),
            "olcum": st.column_config.NumberColumn(m("Ölçüm", "Measurements"), format="%d"),
            "medyan_dk": st.column_config.NumberColumn(m("Tipik bekleme (dk)", "Typical wait (min)"),
                                                       format="%.0f"),
            "p90_dk": st.column_config.NumberColumn(m("10'da 9'u en fazla (dk)", "9 in 10 at most (min)"),
                                                    format="%.0f"),
            "uzun_bekleme_orani": st.column_config.NumberColumn(m("Uzun bekleme %", "Long waits %"),
                                                                format="%.0f"),
            "yigilma_orani": st.column_config.NumberColumn(m("Yığılma %", "Bunching %"), format="%.0f"),
        })
    st.caption(m("Sütun başlığına tıklayarak sıralayabilirsin. Az ölçümlü satırlara temkinli bak.",
                 "Click a column header to sort. Treat rows with few measurements with caution."))

st.divider()

# ---------------------------------------------------------------- saatlere göre

st.subheader(m("Günün saatine göre", "By hour of day"))
s = saatlik[(saatlik.hat_id == hat) & (saatlik.rota_yon == yon) & (saatlik.olcum >= MIN_OLCUM_SAAT)].copy()
if s.empty:
    st.info(m("Bu hat ve yön için saatlik analiz yapacak kadar veri yok.",
              "Not enough data for an hourly breakdown of this line and direction."))
else:
    s["saat_etiket"] = s.saat.map(lambda h: f"{h:02d}:00")
    s["oran_metin"] = s.uzun_bekleme_orani.map(yuzde)
    s["medyan_metin"] = s.medyan_dk.round().astype(int).astype(str) + f" {DK}"
    ipucu = [alt.Tooltip("saat_etiket:N", title=m("Saat", "Hour")),
             alt.Tooltip("medyan_metin:N", title=m("Tipik bekleme", "Typical wait")),
             alt.Tooltip("oran_metin:N", title=m("Uzun bekleme", "Long waits")),
             alt.Tooltip("olcum:Q", title=m("Ölçüm", "Measurements"))]
    x = alt.X("saat:O", title=m("Saat", "Hour"), axis=alt.Axis(labelAngle=0))
    sol, sag = st.columns(2)
    with sol:
        st.markdown(m("**Tipik bekleme (dk)**", "**Typical wait (min)**"))
        st.altair_chart(
            alt.Chart(s).mark_line(point=alt.OverlayMarkDef(size=60, filled=True),
                                   strokeWidth=2, color=MAVI)
            .encode(x=x, y=alt.Y("medyan_dk:Q", title=None, scale=alt.Scale(zero=True)), tooltip=ipucu)
            .properties(height=260), width="stretch")
    with sag:
        st.markdown(m("**Uzun bekleme oranı**", "**Long-wait share**"))
        st.altair_chart(
            alt.Chart(s).mark_bar(cornerRadiusEnd=4, color=MAVI, size=16)
            .encode(x=x, y=alt.Y("uzun_bekleme_orani:Q", title=None, axis=alt.Axis(format="%")),
                    tooltip=ipucu)
            .properties(height=260), width="stretch")
    en_kotu = s.loc[s.uzun_bekleme_orani.idxmax()]
    st.caption(m(f"En sorunlu saat: **{int(en_kotu.saat):02d}:00** — bu saatte bekleyişlerin "
                 f"{yuzde(en_kotu.uzun_bekleme_orani)}'i uzun sürüyor. "
                 f"{MIN_OLCUM_SAAT}'den az ölçümü olan saatler gösterilmiyor.",
                 f"Most problematic hour: **{int(en_kotu.saat):02d}:00**, when "
                 f"{yuzde(en_kotu.uzun_bekleme_orani)} of waits are long. "
                 f"Hours with fewer than {MIN_OLCUM_SAAT} measurements are not shown."))

st.divider()

# ---------------------------------------------------------------- duraklar

st.subheader(m("Güzergah boyunca", "Along the route"))
d = durak[(durak.hat_id == hat) & (durak.rota_yon == yon)].dropna(subset=["ENLEM"]).copy()
if d.empty:
    st.info(m("Bu hat ve yön için durak verisi yok.", "No stop data for this line and direction."))
else:
    d = d.sort_values("durak_sira")
    d["oran_metin"] = d.uzun_bekleme_orani.map(yuzde)
    d["medyan_metin"] = d.medyan_dk.round().astype(int).astype(str) + f" {DK}"
    sol, sag = st.columns([3, 2])
    with sol:
        # Tek renk; nokta ALANI mutlak uzun bekleme oranıyla orantılı (hatlar arasında karşılaştırılabilir).
        # Eskiden boyut hattın kendi en düşük/en yüksek durağına göre ölçekleniyordu; %20 ile %25
        # arasındaki küçük fark en küçükten en büyük noktaya uzanıyor ve yanıltıyordu.
        d["yaricap_px"] = 3 + 9 * (d.uzun_bekleme_orani.clip(0, 0.5) / 0.5) ** 0.5
        katman = pdk.Layer(
            "ScatterplotLayer", data=d, get_position="[BOYLAM, ENLEM]",
            get_fill_color=[42, 120, 214, 210], get_line_color=[255, 255, 255],
            line_width_min_pixels=1, stroked=True, get_radius="yaricap_px",
            radius_units="'pixels'",  # tırnak şart: yoksa pydeck "pixels"i değişken sanıyor
            pickable=True)
        st.pydeck_chart(pdk.Deck(
            layers=[katman],
            initial_view_state=pdk.ViewState(
                latitude=(d.ENLEM.min() + d.ENLEM.max()) / 2,
                longitude=(d.BOYLAM.min() + d.BOYLAM.max()) / 2,
                zoom=kadraj_zoom(d)),
            tooltip={"html": "<b>{DURAK_ADI}</b><br/>"
                             + m("Tipik bekleme", "Typical wait") + ": {medyan_metin}<br/>"
                             + m("Uzun bekleme", "Long waits") + ": {oran_metin}"},
            map_style=None,
        ), height=420)
        st.caption(m("Nokta büyüdükçe o durakta uzun bekleme daha sık: en küçük nokta %0, "
                     "en büyük nokta %50 ve üzeri. Ölçek tüm hatlarda aynı.",
                     "Bigger dots mean more frequent long waits at that stop: the smallest dot is 0%, "
                     "the biggest 50% or more. The scale is the same for every line."))
    with sag:
        st.markdown(m("**Durak sırasına göre uzun bekleme**", "**Long waits by stop order**"))
        st.altair_chart(
            alt.Chart(d).mark_line(strokeWidth=2, color=MAVI,
                                   point=alt.OverlayMarkDef(size=40, filled=True))
            .encode(x=alt.X("durak_sira:Q", title=m("Durak sırası (başlangıçtan)", "Stop number (from start)")),
                    y=alt.Y("uzun_bekleme_orani:Q", title=None, axis=alt.Axis(format="%")),
                    tooltip=[alt.Tooltip("DURAK_ADI:N", title=m("Durak", "Stop")),
                             alt.Tooltip("medyan_metin:N", title=m("Tipik bekleme", "Typical wait")),
                             alt.Tooltip("oran_metin:N", title=m("Uzun bekleme", "Long waits"))])
            .properties(height=380), width="stretch")
        st.caption(m("Çizgi yükseliyorsa düzensizlik yol boyunca birikiyor demektir.",
                     "A rising line means irregularity builds up along the route."))

st.divider()

# ---------------------------------------------------------------- dağılım

st.subheader(m("Bekleme sürelerinin dağılımı", "Distribution of waiting times"))
a = dagilim[(dagilim.hat_id == hat) & (dagilim.rota_yon == yon)].copy()
a = a[a.kutu_dk <= secili.p99_dk]
a["kutu_bit"] = a.kutu_dk + 1.8   # kutular arasında ince boşluk
a["aralik_metin"] = a.kutu_dk.astype(str) + "–" + (a.kutu_dk + 2).astype(str) + f" {DK}"
hist = alt.Chart(a).mark_rect(color=MAVI, cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
    x=alt.X("kutu_dk:Q", title=m("Ardışık iki otobüs arası (dk)", "Time between consecutive buses (min)"),
            scale=alt.Scale(domainMin=0)),
    x2="kutu_bit:Q",
    y=alt.Y("adet:Q", title=m("Ölçüm sayısı", "Measurements")),
    y2=alt.datum(0),
    tooltip=[alt.Tooltip("aralik_metin:N", title=m("Aralık", "Range")),
             alt.Tooltip("adet:Q", title=m("Ölçüm", "Measurements"))])
cizgi = alt.Chart(pd.DataFrame({"m": [secili.medyan_dk]})).mark_rule(
    strokeDash=[4, 4], color=YAZI, strokeWidth=2).encode(x="m:Q")
st.altair_chart((hist + cizgi).properties(height=260), width="stretch")
st.caption(m(f"Kesikli çizgi tipik beklemeyi ({secili.medyan_dk:.0f} dk) gösteriyor. Düzenli bir hatta "
             "çubuklar bu çizginin etrafında toplanır; yayıldıkça düzensizlik artar. "
             "En uzun %1'lik kısım grafikte gösterilmiyor.",
             f"The dashed line marks the typical wait ({secili.medyan_dk:.0f} min). On a regular line the "
             "bars cluster around it; the more they spread, the less regular the line. "
             "The longest 1% is not shown."))
st.caption(m("Durak geçiş anları 5 dakika arayla alınan iki konum arasında oranlanarak bulunduğu "
             "için tek tek aralıklarda 1-2 dakikalık sapma olabilir; oranlar ve medyanlar bundan çok az etkilenir.",
             "Stop passage times are interpolated between two positions taken 5 minutes apart, so a single "
             "gap can be off by 1–2 minutes; shares and medians are barely affected."))

# ---------------------------------------------------------------- yöntem

with st.expander(m("Bu ölçümler nasıl yapıldı?", "How were these measured?")):
    st.markdown(m(f"""
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
""", f"""
1. **Collection.** The positions of all buses on every ESHOT line are fetched every 5 minutes from
   İzmir Metropolitan Municipality's open data API (automatically, on GitHub Actions). Results are
   calculated per calendar month: the current month is updated every night and finished months are
   finalised on the 1st. This month uses {meta.get('gun_sayisi', '?')} days of data. Compared with
   1-minute data, 5-minute sampling changed the typical wait and the long-wait share by only a few points.
2. **Ghost-record cleaning.** Besides each bus's live position, the API returns old positions left
   behind at places such as garages, a transfer hub and a petrol station. **{yuzde(meta['hayalet_orani'])}**
   of records with GPS are such ghost records; coordinates that never change over time are removed.
3. **Map-matching.** Each position is projected onto the line's official route and converted into
   "metres from the start of the route".
4. **Stop passages.** The moment a bus passes a stop is interpolated between two measurements.
5. **Wait (headway).** The time between two consecutive buses at the same stop. Gaps that fall into
   periods without data (night, outages) and unreliable stops at the ends of a route are excluded.

**Definitions:** *Typical wait* = median of the gaps. *Long wait* = longer than 1.5× the line's typical
wait **in the same time band**. *Bunching* = shorter than half the typical wait in the same band (buses
arrive together). Time bands: night (00–06), morning peak (06–10), daytime (10–16), evening peak (16–20),
evening (20–24). Planned sparse service in the evening and at night therefore does not count as a
long wait; the measure reflects irregularity, not the timetable.

**Limitations:** Planned headways are not available as open data, so measurements are made against
each line's own typical value, not the timetable. Weekday and weekend timetables are not separated yet.

Source: [İzmir Metropolitan Municipality Open Data Portal](https://acikveri.bizizmir.com) ·
İzmir Metropolitan Municipality Open Data License · Last updated: {tarih(meta['guncelleme_utc'])}
"""))
