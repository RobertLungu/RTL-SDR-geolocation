# ADS-B — auto-geolocalizarea receptorului

Această conductă estimează **poziția proprie a receptorului** pornind exclusiv de la
difuzările ADS-B ale avioanelor din jur (frecvența 1090 MHz). Este *reference-free*: nu
se pornește niciodată de la coordonatele proprii ale receptorului — se folosesc doar
pozițiile avioanelor decodate global (global-CPR) și puterea semnalului (RSSI) pentru o
ajustare de path-loss.

Este complet izolată de conducta GSM din folderul părinte.

> ⚠️ **Avertisment hardware.** Recepția pe 1090 MHz a dongle-ului RTL2838 este puternic
> degradată, dar nenulă. Pe lângă avioane reale (confirmate față de FR24), dongle-ul emite
> și „fantome” de zgomot — ICAO-uri false / poziții imposibile. **Verifică fiecare
> combinație ICAO + poziție cu un tracker live (ex. FlightRadar24) înainte să te încrezi în
> ea.** Precizia actuală a fix-ului este de ~12–30 km, limitată de cantitatea de date.

## Cerințe

- **Hardware:** un dongle RTL-SDR conectat pe USB.
- **Sistem:** `dump1090-mutability` (decodează mesajele Mode-S / ADS-B).
  ```bash
  sudo apt install dump1090-mutability
  ```
- **Python:** `python3`.

## Țintele `make`

Rulează `make help` pentru lista scurtă. Fluxul:

| Țintă        | Ce face                                                                       |
|--------------|-------------------------------------------------------------------------------|
| `capture`    | Rulează `capture.py` timp de `DUR` secunde (implicit 600 s / 10 min). Necesită dongle + `dump1090-mutability`. |
| `filter`     | Filtrează captura brută → `tracks_pruned.csv` + `ranging_samples.csv`         |
| `locate`     | Geolocalizează din eșantioanele de ranging → `adsb_position.geojson`          |
| `map`        | Randează o hartă Leaflet nouă (arhivă auto-numerotată `captures/captureN.html`) |
| `process`    | `filter` + `locate` + `map` + `open` (reia o captură existentă, fără dongle)   |
| `all`        | `capture` + `process` (conducta completă, cap-coadă)                           |
| `open`       | Deschide cea mai recentă `captures/captureN.html` în browser                   |
| `clean`      | Șterge CSV/geojson derivate (păstrează `csv_raw.csv` + arhiva de hărți)        |
| `clean-all`  | `clean` + șterge și `work/csv_raw.csv` (arhiva de hărți NU se șterge niciodată) |

### Buton reglabil

```bash
make capture DUR=300     # capturează 5 minute în loc de 10
```

## Tutorial rapid

1. Conectează dongle-ul RTL-SDR și asigură-te că `dump1090-mutability` este instalat.
2. Conducta completă (capturează + procesează + deschide harta):
   ```bash
   make all
   ```

Sau, pas cu pas:

```bash
make capture DUR=600     # work/csv_raw.csv
make filter              # work/tracks_pruned.csv + work/ranging_samples.csv
make locate              # work/adsb_position.geojson
make map                 # captures/captureN.html
make open                # deschide harta în browser
```

Dacă ai deja o captură și vrei doar să o reprocesezi (fără dongle):

```bash
make process
```

Fluxul de fișiere: `work/csv_raw.csv` → `work/tracks_pruned.csv` +
`work/ranging_samples.csv` → `work/adsb_position.geojson` → `captures/captureN.html`.

Captura brută (`work/csv_raw.csv`) este date hardware prețioase și **nu** este ștearsă de
`clean` — doar `clean-all` o elimină.
