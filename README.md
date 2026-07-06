# Kuuluttajanäyttö

Reaaliaikainen kuuluttajanäyttö juoksukisoihin. Osa Pekka Pirilän
tulospalvelu-järjestelmää (HkMaali). Näyttää kuuluttajalle selaimessa
kilpailijoiden lähestymis- ja maaliajat sitä mukaa kun ajat saapuvat
maalilaitteelta.

Ohjelma (`announcer_display.py`) kuuntelee tulospalvelun lähettämiä
UDP-sanomia, lukee kilpailijatiedot `KILP.DAT`-tiedostosta ja tarjoilee
näkymän selaimeen osoitteessa <http://localhost:8081/>. Selain päivittyy
automaattisesti 2 sekunnin välein.

## Vaatimukset

- **Python 3** (versio 3.6 tai uudempi) asennettuna ja `python`-komento
  löydettävissä `PATH`-polusta.
- **Ei ulkoisia kirjastoja** — ohjelma käyttää pelkästään Pythonin
  vakiokirjastoa (`socket`, `http.server`, `xml.etree` jne.), joten
  `pip install` ei ole tarpeen.
- **HkMaali.exe** (tulospalvelun maaliohjelma) samassa koneessa.
- Selain (Chrome, Edge, Firefox) kuuluttajan näytöllä.

## Kansiorakenne

```
C:\juoksu\                        ← ohjelmatiedostot (tämä repo)
    announcer_display.py          kuuluttajanäytön Python-ohjelma
    start_kuuluttaja.bat          käynnistysskripti
    create_shortcut.ps1           luo työpöydän "Kuuluttaja"-pikakuvakkeen
    KU.cfg                        HkMaali.exe:n asetustiedosto
    HkMaali.exe                   tulospalvelun maaliohjelma
    README.md

C:\kisa\data\KisanNimi.1\         ← yhden kisan datakansio
    KILP.DAT                      kilpailijatiedot (HkMaali kirjoittaa tänne)
    KilpSrj.xml                   sarjamääritykset
    ...                           muut kisan tiedostot
```

Ohjelmatiedostot pysyvät aina kansiossa `C:\juoksu`. Jokaisella kisalla on
oma datakansio polun `C:\kisa\data\` alla, esim. `Mikkeli.1`. HkMaali
kirjoittaa `KILP.DAT`:n ja muut kisatiedostot siihen kansioon, joka on
asetettu sen työhakemistoksi (ks. `start_kuuluttaja.bat`).

## Uuden kisan valmistelu

1. **Luo kisalle datakansio** polun `C:\kisa\data\` alle, esim.
   `C:\kisa\data\Mikkeli.1`.
2. **Kopioi sarjamääritykset** `KilpSrj.xml` kyseiseen kansioon.
3. **Muuta kansion nimi** `start_kuuluttaja.bat`-tiedostoon. Avaa tiedosto
   ja aseta `KISA`-muuttujaan uuden kisan kansiopolku:

   ```bat
   set KISA=C:\kisa\data\Mikkeli.1
   ```

   Tämä on ainoa rivi, joka pitää muuttaa kisaa vaihdettaessa. Sama polku
   ohjaa sekä HkMaali.exe:n työhakemiston että kuuluttajanäytön lukemat
   `KILP.DAT`- ja `KilpSrj.xml`-tiedostot samaan kansioon.

## Käynnistys

Käynnistä koko järjestelmä **työpöydän "Kuuluttaja"-pikakuvakkeesta**.

Pikakuvake luodaan ajamalla kerran (PowerShellissä):

```powershell
powershell -ExecutionPolicy Bypass -File C:\juoksu\create_shortcut.ps1
```

Pikakuvake ajaa `start_kuuluttaja.bat`:n, joka:

1. käynnistää `HkMaali.exe`:n (pyytää UAC-vahvistuksen) kisan datakansiota
   työhakemistona,
2. käynnistää kuuluttajanäytön (`announcer_display.py`),
3. avaa selaimen osoitteeseen <http://localhost:8081/>.

Näytön saa suljettua sulkemalla komentoikkunan.

## Mitä näyttö näyttää

Näkymä on taulukko, jossa on yksi rivi jokaiselle kilpailijalle, jolta on
saapunut aikasanoma. **Rivi ilmestyy vasta kun kilpailijalta on tullut
todellinen aikasanoma** (lähestymis- tai maaliaika) — pelkkä
kilpailijatieto `KILP.DAT`:ssa ei riitä.

| Sarake        | Selitys                                             |
|---------------|-----------------------------------------------------|
| **No**        | Kilpailunumero                                      |
| **Nimi**      | Kilpailijan nimi                                    |
| **Seura**     | Seura                                               |
| **Sarja**     | Sarja (KilpSrj.xml:n mukaan)                         |
| **Lähestyminen** | Viimeisen väliajan (lähestymisajan) aika         |
| **Sija**      | Sija sarjassa lähestymisajan mukaan                 |
| **Maali**     | Maaliaika                                           |
| **Sija**      | Lopullinen sija sarjassa maaliajan mukaan           |

Maaliin tulleet rivit korostuvat vihreällä. Lähestymisajan sija-solu
korostuu värillä: 1. sija vihreä, sijat 2–3 keltainen.

## Palkintopöytä (Top-3)

Palkintojenjakoa varten on erillinen näkymä osoitteessa
<http://localhost:8082/awards>. Sivu päivittyy itsestään 5 sekunnin välein.

Sivu näyttää **vain ne sarjat, joista on jo vähintään yksi maaliin tullut
kilpailija**. Kustakin sarjasta listataan kolme parasta maaliajan mukaan
(No, Nimi, Maali).

Jokaisen sarjan vieressä on valintaruutu **"✅ Palkinnot jaettu"**. Kun
palkinnot on jaettu, ruudun klikkaaminen merkitsee sarjan jaetuksi — sarja
himmenee ja teksti yliviivataan, jotta jäljellä olevat sarjat erottuvat
selkeästi. Tila säilyy palvelimen muistissa niin kauan kuin ohjelma on
käynnissä (nollautuu uudelleenkäynnistyksessä).

Portin voi tarvittaessa vaihtaa käynnistysvalitsimella
`--awards-port PORTTI` (oletus 8082).

## Riippuvuus tulospalvelusta

Kuuluttajanäyttö saa aikatiedot tulospalvelulta UDP-lähetyksinä. Jotta
HkMaali lähettää sanomat näytölle, sen asetustiedostossa (`KU.cfg`) on
oltava yhteysrivi:

```
YHTEYS9=BRO:0/127.0.0.1
```

Tämä ohjaa HkMaalin lähettämään sanomat samaan koneeseen (`127.0.0.1`)
porttiin, jota kuuluttajanäyttö kuuntelee (UDP 15901). Ilman tätä riviä
näyttö ei saa mitään dataa eikä yksikään rivi ilmesty.
