# Kuuluttajanäyttö — asennus ja käyttö

Reaaliaikainen kuuluttaja- ja palkintonäyttö tulospalvelun rinnalle. `announcer_display.py`
kuuntelee tulospalvelun (HkKisaWin.exe) lähettämiä UDP-paketteja ja tarjoaa kolme
selainnäkymää: kuuluttajanäytön sekä palkintosivut sarjoittain ja matkoittain.

## Vaatimukset
- Python 3.8+
- Ei ulkoisia kirjastoja (vain stdlib)
- Tulospalvelu (HkKisaWin.exe) asennettuna

## Tiedostorakenne
- `C:\juoksu\` — ohjelman kotikansio (`announcer_display.py`, `start_kuuluttaja.bat`)
- `C:\kisa\data\KisanNimi.1\` — kisakohtainen datakansio (`KILP.DAT`, `KilpSrj.xml`)

## Ensimmäinen käyttökerta
1. Kloonaa repo: `git clone https://github.com/POL1951/Kuulutus.git C:\juoksu`
2. Aja `create_shortcut.ps1` — luo Kuuluttaja-kuvakkeen työpöydälle

## Per kisa
1. Muuta `start_kuuluttaja.bat`: `set KISA=C:\kisa\data\KisanNimi.1`
2. Tulospalvelussa: `YHTEYS9=BRO:0/127.0.0.1`
3. Kaksoisklikkaa Kuuluttaja-kuvaketta

> HkKisaWin käynnistetään erillisen sovelluksen kautta — tätä ei tarvitse tehdä käsin
> eikä `start_kuuluttaja.bat` käynnistä sitä.

## Selainikkunat (avautuvat automaattisesti)
- http://localhost:8081/ — Kuuluttajanäyttö (reaaliaikainen maaliin tulijat)
- http://localhost:8082/awards — Palkinnot sarjoittain (Top-N per sarja)
- http://localhost:8083/awards — Palkinnot matkoittain (Top-N per matka+sukupuoli)

## Kuuluttajanäytön sarakkeet (8081)
- **NO, NIMI, SEURA, SARJA** — kilpailijan tiedot (`KILP.DAT`:sta)
- **LÄHESTYMINEN + SIJA** — viimeinen väliaikapiste sarjakohtaisesti (`KilpSrj.xml`)
- **MAALI + SIJA** — maaliaika ja sija sarjassa
- Sarakeleveydet säädettävissä raahaamalla, tallennetaan selaimen muistiin

## Palkintosivu sarjoittain (8082)
- Näyttää kaikki sarjat joissa on vähintään 1 maaliintullut
- Top-N per sarja (N säädettävissä `[−]`/`[+]` napeilla, oletus 3)
- Palkinnot jaettu -ruksi: harmaa yliviivaus, sarja siirtyy listan loppuun
- Selain-ilmoitus jos jaetun sarjan tulokset muuttuvat
- Uusi sarja vilkkuu oranssina kun tarpeeksi tuloksia

## Palkintosivu matkoittain (8083)
- Yhdistää kaikki saman matkan ja sukupuolen sarjat (esim. kaikki "63,6 km miehet")
- Sama Top-N ja ruksi-toiminto kuin sarjoittain-näkymässä

## Tulospalvelun asetukset
- Asetus `YHTEYS9=BRO:0/127.0.0.1` ohjaa UDP-paketit (`KILPT`, `VAIN_TULOST`) Python-ohjelmalle porttiin 15901

## GitHub
- Repo: https://github.com/POL1951/Kuulutus
- Päivitä uusimpaan versioon: `cd C:\juoksu && git pull`
- Tiedostot repossa: `announcer_display.py`, `start_kuuluttaja.bat`, `create_shortcut.ps1`, `README.md`
