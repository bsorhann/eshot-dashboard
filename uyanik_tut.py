"""
Streamlit Community Cloud, bir süre ziyaret edilmeyen uygulamaları uyutuyor. Bu betik
sayfayı başsız bir tarayıcıyla açar; uyuyorsa "uyandır" düğmesine basar ve uygulamanın
açılmasını bekler. İş akışı (.github/workflows/uyanik-tut.yml) 6 saatte bir çalıştırır.

Kullanım: python uyanik_tut.py https://...streamlit.app
"""

import sys
import time

from playwright.sync_api import sync_playwright

UYANDIR = "Yes, get this app back up!"
HAZIR_METIN = "İzmir otobüs düzenliliği"


def main(url):
    with sync_playwright() as p:
        tarayici = p.chromium.launch()
        sayfa = tarayici.new_page()
        sayfa.goto(url, timeout=60_000)
        time.sleep(10)
        dugme = sayfa.get_by_text(UYANDIR)
        if dugme.count():
            print("Uygulama uyuyordu, uyandırılıyor...", flush=True)
            dugme.first.click()
        else:
            print("Uygulama zaten açık.", flush=True)
        # Uygulama iframe içinde açılır; başlık görünene kadar en fazla 4 dakika bekle.
        for _ in range(48):
            if any(HAZIR_METIN in (f.content() or "") for f in sayfa.frames):
                print("Uygulama hazır.", flush=True)
                break
            time.sleep(5)
        else:
            print("Uygulama 4 dakikada açılmadı (bir sonraki çalışmada tekrar denenecek).", flush=True)
        tarayici.close()


if __name__ == "__main__":
    main(sys.argv[1])
