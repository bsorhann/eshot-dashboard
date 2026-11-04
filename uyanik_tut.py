"""
Streamlit Community Cloud, bir süre ziyaret edilmeyen uygulamaları uyutuyor. Bu betik
sayfayı başsız bir tarayıcıyla açar; uyuyorsa "uyandır" düğmesine basar ve uygulamanın
açılmasını bekler. İş akışı (.github/workflows/uyanik-tut.yml) 6 saatte bir çalıştırır.

Uygulama 4 dakikada açılmazsa betik hata koduyla çıkar; böylece GitHub'da iş kırmızı görünür.

Kullanım: python uyanik_tut.py https://...streamlit.app
"""

import sys
import time

from playwright.sync_api import sync_playwright

UYANDIR = "Yes, get this app back up!"
HAZIR_METIN = ("İzmir otobüs düzenliliği", "İzmir bus regularity")


def hazir_mi(sayfa):
    """Uygulamanın kendi başlığı (h1) iframe içinde göründü mü?"""
    for f in sayfa.frames:
        try:
            basliklar = f.locator("h1").all_inner_texts()
        except Exception:
            continue
        if any(m in b for b in basliklar for m in HAZIR_METIN):
            return True
    return False


def main(url):
    with sync_playwright() as p:
        try:
            # GitHub'ın sunucusunda Chrome zaten kurulu; ayrıca tarayıcı indirmeye gerek yok.
            tarayici = p.chromium.launch(channel="chrome")
        except Exception:
            tarayici = p.chromium.launch()
        sayfa = tarayici.new_page()
        sayfa.goto(url, timeout=60_000)
        time.sleep(10)
        dugme = sayfa.get_by_text(UYANDIR)
        if dugme.count():
            print("Uygulama uyuyordu, uyandırılıyor...", flush=True)
            dugme.first.click()
        else:
            print("Uyku ekranı yok.", flush=True)
        for i in range(48):
            if hazir_mi(sayfa):
                print(f"Uygulama hazır ({10 + i * 5} sn).", flush=True)
                tarayici.close()
                return 0
            time.sleep(5)
        print("Uygulama 4 dakikada açılmadı.", flush=True)
        tarayici.close()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
