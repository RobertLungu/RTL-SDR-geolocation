ADS-B - auto-geolocalizarea receptorului
========================================

Estimeaza pozitia proprie a receptorului pornind doar de la difuzarile ADS-B ale
avioanelor din jur (1090 MHz). Este reference-free: nu se pornesc niciodata de la
coordonatele proprii; se folosesc doar pozitiile avioanelor (global-CPR) si RSSI
pentru o ajustare de path-loss.

Complet izolata de conducta GSM din folderul parinte.

Avertisment hardware: receptia pe 1090 MHz a dongle-ului RTL2838 este puternic
degradata, dar nenula. Pe langa avioane reale, dongle-ul emite si fantome de
zgomot (ICAO false / pozitii imposibile). Verifica fiecare ICAO + pozitie cu un
tracker live (ex. FlightRadar24) inainte sa te increzi in ea. Precizia actuala a
fix-ului este de ~12-30 km, limitata de cantitatea de date.


Cerinte
-------

- un dongle RTL-SDR conectat pe USB
- dump1090-mutability (decodeaza mesajele Mode-S / ADS-B):
    sudo apt install dump1090-mutability
- python3


Comenzile make
--------------

  make capture    Ruleaza capture.py timp de DUR secunde (implicit 600 = 10 min)
                    Necesita dongle + dump1090-mutability
  make filter     Filtreaza captura bruta -> tracks_pruned.csv + ranging_samples.csv
  make locate     Geolocalizeaza din esantioanele de ranging -> adsb_position.geojson
  make map        Randeaza o harta noua (auto-numerotata) -> captures/captureN.html
  make process    filter + locate + map + open  (reia o captura existenta, fara dongle)
  make all        capture + process             (conducta completa)
  make open       Deschide cea mai recenta harta in browser
  make clean      Sterge CSV/geojson derivate (pastreaza csv_raw.csv si hartile)
  make clean-all  clean + sterge si work/csv_raw.csv

Buton reglabil:

    make capture DUR=300     # capturează 5 minute in loc de 10


Utilizare
---------

Cel mai simplu, tot lantul dintr-o comanda:

    make all

Pas cu pas:

    make capture DUR=600     # work/csv_raw.csv
    make filter              # work/tracks_pruned.csv + work/ranging_samples.csv
    make locate              # work/adsb_position.geojson
    make map                 # captures/captureN.html
    make open                # deschide harta

Daca ai deja o captura si vrei doar sa o reprocesezi, fara dongle:

    make process

Flux fisiere:
    work/csv_raw.csv -> work/tracks_pruned.csv + work/ranging_samples.csv
    -> work/adsb_position.geojson -> captures/captureN.html

Captura bruta (work/csv_raw.csv) sunt date hardware pretioase si NU este stearsa
de clean; doar clean-all o elimina.
