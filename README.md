RTL-SDR Geolocation - conducta GSM
==================================

Estimeaza o pozitie prin centroid ponderat cu RSSI, pornind de la turnurile GSM
auzite de dongle-ul RTL-SDR si de la coordonatele lor cautate pe OpenCellID.
Rezultatul se afiseaza pe o harta Leaflet.

Conducta ADS-B (auto-geolocalizarea receptorului din avioane) este separata si se
afla in adsb/ (vezi adsb/README.md).


Cerinte
-------

- gr-gsm si rtl-sdr, la nivel de sistem:
    sudo apt install gr-gsm rtl-sdr
- un dongle RTL-SDR conectat pe USB
- dependinte Python:
    pip install -r requirements.txt
  (numpy este necesar; scipy este optional - fara el, estimarea foloseste o
  cautare pe grila)
- o cheie OpenCellID pentru pasul locate:
    export OPENCELLID_KEY=...
  Alternativ, pune linia "export OPENCELLID_KEY=..." in fisierul .env; Makefile-ul
  o incarca automat.


Comenzile make
--------------

  make scan       Scaneaza celulele GSM cu RTL-SDR              -> cells.csv
  make locate     Cauta pozitia turnurilor pe OpenCellID        -> cells_located.csv
  make position   Calculeaza estimarea ponderata cu RSSI        -> position.geojson
  make map        Randeaza o harta noua (auto-numerotata)       -> captures/captureN.html
  make open       Deschide cea mai recenta harta in browser
  make process    locate (daca e invechit) + position + map + open  (fara dongle)
  make all        Ruleaza tot lantul si deschide harta
  make export     Optional: towers.geojson + towers.kml (QGIS / Google Earth)
  make clean      Sterge artefactele de pozitie (pastreaza CSV-urile si hartile)
  make distclean  clean + sterge si CSV-urile scanate/localizate
  make help       Afiseaza lista de comenzi

Butoane reglabile (se dau pe linia de comanda):

  make scan GAIN=45 PPM=-2 BAND=GSM900
  make position N=8 SELECT=closest WEIGHT=amplitude

  BAND (implicit GSM900), GAIN (40), PPM (0)          - pentru scanare
  N (8), SELECT (closest), WEIGHT (amplitude)         - pentru estimare


Utilizare
---------

Cel mai simplu, tot lantul dintr-o comanda:

    make all

Pas cu pas:

    make scan       # cells.csv
    make locate     # cells_located.csv   (necesita OPENCELLID_KEY)
    make position   # position.geojson
    make map        # captures/captureN.html
    make open       # deschide harta

Daca ai deja o scanare si vrei doar sa reprocesezi, fara dongle:

    make process

Flux fisiere:
    cells.csv -> cells_located.csv -> position.geojson -> captures/captureN.html


