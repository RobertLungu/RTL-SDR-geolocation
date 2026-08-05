# RTL-SDR Geolocation — conducta GSM

Experimente de geolocalizare radio folosind un dongle RTL-SDR (RTL2838).

Acest folder conține conducta **GSM**: estimează o poziție prin **centroid ponderat cu
RSSI** pornind de la turnurile de telefonie GSM „auzite” de dongle și de la coordonatele
lor căutate pe OpenCellID. Rezultatul este afișat pe o hartă Leaflet interactivă.

> Conducta **ADS-B** (auto-geolocalizarea receptorului din difuzările avioanelor) este
> complet separată și trăiește în [`adsb/`](adsb/README.md).

## Cerințe

- **Sistem (apt):** `gr-gsm` și `rtl-sdr`
  ```bash
  sudo apt install gr-gsm rtl-sdr
  ```
- **Hardware:** un dongle RTL-SDR conectat pe USB.
- **Python:** `pip install -r requirements.txt` (necesită `numpy`; `scipy` este opțional —
  fără el, estimarea poziției folosește o căutare pe grilă).
- **Cheie OpenCellID** (pentru `locate`):
  ```bash
  export OPENCELLID_KEY=...
  ```
  Alternativ, pune linia `export OPENCELLID_KEY=...` în fișierul `.env` — `Makefile` îl
  încarcă automat (`.env` este ignorat de Git, nu se urcă pe GitHub).

## Țintele `make`

Rulează `make help` oricând pentru lista scurtă. Fluxul complet:

| Țintă        | Ce face                                                                  | Ieșire                          |
|--------------|--------------------------------------------------------------------------|---------------------------------|
| `scan`       | 1. Scanează celulele GSM cu RTL-SDR                                       | `cells.csv`                     |
| `locate`     | 2. Caută poziția turnurilor pe OpenCellID                                 | `cells_located.csv`             |
| `position`   | 3. Calculează estimarea de poziție ponderată cu RSSI                      | `position.geojson`              |
| `map`        | 4. Randează o hartă Leaflet nouă (arhivă auto-numerotată)                 | `captures/captureN.html`        |
| `open`       | Deschide cea mai recentă `captures/captureN.html` în browser             | —                               |
| `process`    | `locate` (dacă e învechit) + `position` + `map` + `open` (fără dongle)    | reia analiza scanării existente |
| `all`        | Rulează tot lanțul și deschide harta                                      | —                               |
| `export`     | Opțional: exportă `towers.geojson` + `towers.kml` (QGIS / Google Earth)  | `towers.geojson`, `towers.kml`  |
| `clean`      | Șterge artefactele de poziție (păstrează CSV-urile + arhiva de hărți)     | —                               |
| `distclean`  | `clean` + șterge și CSV-urile scanate/localizate                          | —                               |
| `help`       | Afișează ajutorul                                                         | —                               |

### Butoane reglabile (override pe linia de comandă)

```bash
make scan GAIN=45 PPM=-2 BAND=GSM900       # reglaje scanare
make position N=8 SELECT=closest WEIGHT=amplitude   # reglaje estimare poziție
```

- `BAND` (implicit `GSM900`), `GAIN` (`40`), `PPM` (`0`) — pentru scanare.
- `N` (`8`), `SELECT` (`closest`), `WEIGHT` (`amplitude`) — pentru estimarea poziției.

## Tutorial rapid

1. Conectează dongle-ul RTL-SDR și exportă cheia OpenCellID (sau pune-o în `.env`).
2. Rulează întregul lanț:
   ```bash
   make all
   ```
   Aceasta scanează, localizează, estimează poziția, randează harta și o deschide.

Sau, pas cu pas:

```bash
make scan       # cells.csv
make locate     # cells_located.csv   (necesită OPENCELLID_KEY)
make position   # position.geojson
make map        # captures/captureN.html
make open       # deschide harta în browser
```

Dacă ai deja o scanare și vrei doar să reprocesezi (fără dongle):

```bash
make process
```

Fluxul de fișiere: `cells.csv` → `cells_located.csv` → `position.geojson` →
`captures/captureN.html`.

## Studiu tehnic

`studiu_geolocalizare.md` (și `.odt` generat cu `pandoc`) conține un studiu tehnic în
limba română al ambelor conducte — algoritmul de centroid ponderat GSM și ajustarea
path-loss pe RSSI pentru ADS-B.
