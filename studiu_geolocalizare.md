---
title: "Geolocalizare fără GPS cu un receptor RTL-SDR: raport tehnic asupra celor două lanțuri de prelucrare (GSM și ADS-B)"
author: Lungu Robert
date: iulie 2026
---

## 1. Introducere

Acest raport documentează un experiment de geolocalizare prin mijloace secundare: estimarea poziției receptorului fără GPS, exclusiv din semnale radio recepționate pasiv. Studiul este de sine stătător — descrie ce a fost construit, cum funcționează fiecare etapă, ce rezultate au fost măsurate, care sunt limitările constatate și ce direcții de îmbunătățire au fost identificate.

Hardware-ul este un dongle RTL-SDR cu circuit RTL2838 și tuner R820T, folosit cu antena generică din pachet, neacordată pe vreuna dintre benzile de interes: receptor SDR ieftin, fără calibrare de amplitudine, cu performanțe slabe la 1090 MHz (detalii în secțiunea 3.3). Rezultatele de mai jos trebuie citite ca limită inferioară a metodei; cu echipament de recepție adecvat, toate cifrele ar trebui să se îmbunătățească. Pe acest hardware rulează două lanțuri de prelucrare complet separate, fiecare cu propriul Makefile și propriile fișiere de lucru:

- **Lanțul GSM** (rădăcina proiectului + `tools/`): scanează celulele GSM900, caută pozițiile turnurilor în OpenCellID și estimează poziția receptorului printr-un centroid ponderat cu RSSI.
- **Lanțul ADS-B** (`adsb/`): recepționează mesajele ADS-B ale aeronavelor pe 1090 MHz și estimează poziția receptorului printr-un model de atenuare ajustat pe RSSI-ul semnalelor recepționate de la aeronave cu poziții cunoscute.

Regulă de proiect valabilă în ambele lanțuri, strictă în cel ADS-B: estimarea este auto-geolocalizare fără referință — coordonatele reale ale receptorului nu intră niciodată în calcul și servesc exclusiv la evaluarea erorii finale.

### 1.1. Echipament și cerințe de sistem

- **Receptor:** dongle RTL-SDR (RTL2838 + tuner R820T) cu antena generică din pachet; un port USB 2.0 liber.
- **Sistem de operare și pachete:** Linux; la nivel de sistem sunt necesare `rtl-sdr` (drivere, plus blacklist-ul uzual al modulului de kernel DVB-T), `gr-gsm` pentru scanarea GSM și `dump1090-mutability` pentru recepția ADS-B.
- **Python:** Python 3 cu `numpy` (obligatoriu); `scipy` este opțional — în lipsa lui, `estimate_position.py` și `locate.py` revin la căutarea pe grilă. Restul codului folosește doar biblioteca standard.
- **Resurse de calcul:** modeste. Sarcina cea mai grea este demodularea în timp real la ~2 MS/s; un laptop obișnuit sau o placă de clasa Raspberry Pi este suficientă, cu un consum de memorie mult sub 1 GB.
- **Stocare:** neglijabilă — întregul proiect, inclusiv 7 capturi arhivate, ocupă circa 11 MB; CSV-ul brut al unei capturi ADS-B de 30 de minute are aproximativ 1 MB.
- **Internet:** necesar doar la instalarea pachetelor și la etapa *locate* a lanțului GSM (câteva zeci de cereri OpenCellID per captură sau, cu extrasul CSV din §2.4, o singură descărcare pe zi). Lanțul ADS-B rulează integral offline; singurul pas online suplimentar este verificarea manuală a fantomelor pe un tracker precum FR24.

## 2. Lanțul GSM

### 2.1. Etapele pipeline-ului

Pipeline-ul este definit în Makefile-ul din rădăcină: *scan* → *locate* → *position* → *map*.

**Scan** (`tools/gsm_scan_loop.sh` + `tools/grgsm_scanner`). Scriptul rulează o variantă locală, patch-uită, a utilitarului `grgsm_scanner` din gr-gsm (scanarea SDCCH/8 este dezactivată, astfel încât programul tipărește tabelul de celule și se termină în loc să se blocheze pe canale cu salt de frecvență). Parametri impliciți: banda GSM900, gain 40 dB, corecție PPM 0; toți sunt configurabili din linia de comandă. Pentru fiecare celulă detectată se extrag din canalul de difuzare: MCC, MNC, LAC, CID, ARFCN, frecvența și RSSI. Ieșirea este `cells.csv`, o linie pe celulă și pe instantaneu. Captura curentă conține 56 de observații din rețele românești (MCC 226), de la mai mulți operatori.

**Locate** (`tools/geolocate_cells.py`). Pentru fiecare combinație unică MCC/MNC/LAC/CID se interoghează API-ul OpenCellID, care întoarce latitudinea, longitudinea și o rază de incertitudine `range_m` (în datele proiectului: de la circa 1 000 m la câteva mii de metri). Rezultatele sunt memorate în cache pe durata rulării (o singură cerere per celulă fizică), rândurile cu decodare eșuată (CID sau MCC egal cu 0) sunt sărite, iar între cereri se aplică o pauză de 0,4 s. Ieșirea este `cells_located.csv`.

**Position** (`tools/estimate_position.py`). Etapa de calcul; descrisă în 2.2.

**Map** (`tools/make_map_html.py`). Generează o hartă Leaflet autonomă, cu turnurile colorate pe operator, cercurile de incertitudine OpenCellID și estimarea de poziție, arhivată incremental ca `captures/captureN.html`. `tools/export_map.py` poate exporta suplimentar `towers.geojson` și `towers.kml`.

### 2.2. Algoritmul de estimare

`estimate_position.py` calculează două estimări; doar prima este considerată rezultat.

**Estimarea primară: centroid ponderat (WCL).** Observațiile sunt deduplicate: dintre aparițiile repetate ale aceleiași celule fizice (aceeași cheie MCC/MNC/LAC/CID) se păstrează cea cu RSSI maxim. Fiecare turn primește ponderea w = 10^(RSSI/k), cu k = 20 (ponderare „în amplitudine", implicită) sau k = 10 (ponderare „în putere", care apropie estimarea de turnul cel mai puternic). Poziția estimată este media coordonatelor turnurilor, ponderată cu w.

**Selecția turnurilor.** În modul implicit (`--select closest`, N = 8): se pornește de la cele mai puternice N turnuri, se calculează un centroid-sămânță, se rețin cele N turnuri fizic cele mai apropiate de sămânță, se recalculează centroidul și se iterează (maximum 5 pași) până când mulțimea se stabilizează. Scopul este eliminarea turnurilor îndepărtate care ar deplasa centroidul. Modul `--select rssi` păstrează comportamentul vechi (cele mai puternice N).

**Controlul reziduurilor.** Pentru fiecare turn selectat se compară RSSI-ul măsurat cu predicția modelului log-distanță la poziția estimată. RSSI-ul fiind necalibrat, reziduurile se centrează prin scăderea mediei. Turnurile cu reziduu peste pragul max(2σ, 8 dB) — semnal prea slab pentru distanța lor sau invers, indiciu de coordonate greșite în baza colaborativă sau de cale obstrucționată — sunt eliminate, iar soluția se recalculează o singură dată. Pragul-podea de 8 dB previne eliminarea variațiilor normale de umbrire.

**Estimarea secundară: trilaterație log-distanță (necalibrată, doar orientativă).** Din modelul RSSI = P0 − 10·n·log10(d/d0), cu n = 3,5 (macro-celulă urbană), d0 = 100 m și P0 = −29 dB (derivat fizic: EIRP tipic GSM900 de 45 dBm minus pierderea în spațiu liber la d0, minus 2 dB pierderi de cablu), se estimează o distanță per turn; poziția rezultă prin cele mai mici pătrate ponderate (scipy Nelder–Mead sau, în lipsă, căutare pe grilă cu rafinare). Estimarea este afișată doar spre comparație.

Ambele estimări se scriu în `position.geojson`. Rezultat măsurat pe captura curentă: WCL și trilaterația diferă cu câteva zeci de metri; raza de precizie raportată (dispersia RMS ponderată a turnurilor în jurul estimării) este 842 m. Față de poziția reală a receptorului, cunoscută operatorului dar neînregistrată în proiect, eroarea efectivă a estimării WCL a fost de circa 300 m — mai bună decât intervalul realist de 0,5–1,5 km documentat în cod și în limitele razei raportate. Fișierul GeoJSON curent listează 27 de turnuri folosite, deci rularea comisă a folosit un N mai mare decât valoarea implicită 8.

### 2.3. Limitări

- **Bias structural al WCL:** estimarea este deplasată către centrul geometric al norului de turnuri, nu către poziția reală a receptorului. Eroare realistă, conform documentației din cod: 500 m – 1,5 km.
- **Calitatea bazei OpenCellID:** pozițiile turnurilor sunt estimări colaborative, cu erori de la zeci de metri la kilometri; unele celule lipsesc complet din baza de date.
- **RSSI necalibrat:** dongle-ul raportează valori relative, afectate de umbrire, propagare multiplă și diferențe de putere de emisie între operatori. De aceea trilaterația nu este rezultat: fără calibrare, distanțele absolute pot fi greșite de 3–10×. Opțiunea `--rssi-cal` permite fixarea unui decalaj față de un turn aflat la distanță cunoscută, dar nu a fost încă folosită.
- **O singură bandă scanată:** capturile curente sunt doar GSM900.

### 2.4. Îmbunătățiri propuse

- Calibrare RSSI cu `--rssi-cal` față de un turn la distanță cunoscută, pentru a face trilaterația utilizabilă.
- Scanarea benzii DCS1800 în plus față de GSM900 (Makefile-ul o suportă deja prin variabila `BAND`).
- Acumularea mai multor instantanee, eventual din poziții ușor diferite, și fuziunea capturilor.
- Metode de estimare care exploatează geometria dincolo de centroid (ponderi dependente de model).
- **Acces offline la baza de date:** înlocuirea interogării API-ului OpenCellID celulă cu celulă cu descărcarea zilnică a extrasului CSV pe țară publicat de același serviciu (fișierul pentru MCC 226, România, cu aceeași cheie de acces) și căutări locale în acest extras. Avantaje: pipeline funcțional offline, fără dependență de rețea, fără limite de interogare și fără pauzele dintre cereri; căutări instantanee. Compromis: extrasul local poate omite un număr mic de celule pe care API-ul le-ar fi găsit prin seturile de date partenere.

## 3. Lanțul ADS-B

### 3.1. Etapele pipeline-ului

Pipeline-ul este definit în `adsb/Makefile`: *capture* → *filter* → *locate* → *map*. Sursele de semnal sunt aeronave care își difuzează poziția prin ADS-B pe 1090 MHz; RSSI-ul recepției servește ca măsură brută de distanță, iar poziția receptorului se estimează invers, din consecvența RSSI–distanță.

**Capture** (`adsb/capture.py`). Lansează dump1090-mutability și scrie fiecare mesaj decodat (flux SBS-1, portul 30003) într-un CSV brut (`work/csv_raw.csv`), cu timestamp de recepție, ICAO, tip de mesaj, altitudine, lat/lon, viteză, squawk și RSSI. Scriptul rulează dump1090 fără opțiunile `--lat/--lon` (și refuză să pornească dacă acestea apar în linia de comandă — `_assert_no_receiver_pos`), astfel încât pozițiile provin numai din decodarea CPR globală (pereche de cadre par/impar) sau din CPR local relativ la fixul anterior al aceleiași aeronave. Fluxul SBS nu transportă nivel de semnal, așa că un fir secundar citește fluxul binar Beast (portul 30005), extrage octetul de semnal al cadrelor Mode-S cu ICAO în clar (DF 11/17/18), îl convertește în dBFS și ștampilează fiecare rând CSV cu cel mai recent RSSI văzut pentru aeronava respectivă. Mecanismul este best-effort: dacă fluxul Beast lipsește, coloana `rssi` rămâne goală.

**Filter** (`adsb/filter.py`). Patru stadii:

1. **Sanitizare:** respinge rândurile malformate — ICAO care nu e hexazecimal pe 6 caractere, coordonate în afara domeniului, poziții exact (0, 0), altitudini în afara benzii −1 500 … 60 000 ft; contorizează motivele de respingere.
2. **Poartă cinematică per aeronavă:** fixurile fiecărei aeronave se sortează în timp; duplicatele exacte și fixurile cu dt ≤ 0 se elimină; orice fix care implică peste 1 200 km/h față de ultimul fix acceptat se respinge. Aeronavele cu cel puțin 2 fixuri supraviețuitoare sunt „localizate".
3. **Control inter-aeronave (fantome):** două aeronave auzite în ferestre de timp suprapuse de același receptor nu pot fi la mai mult de 800 km una de alta (dublul razei de vizibilitate directă ADS-B). Pentru perechile care încalcă limita, traiectoria cu mai puține fixuri se elimină ca fantomă.
4. **Îmbogățire RSSI:** pentru fiecare cadru purtător de RSSI al unei aeronave localizate, poziția la momentul cadrului se obține prin interpolare liniară între fixurile care îl încadrează; nu se extrapolează în afara intervalului de fixuri.

Ieșiri: `work/tracks_pruned.csv` (fixurile curate) și `work/ranging_samples.csv` (eșantioanele poziție + RSSI, intrarea locatorului).

**Locate** (`adsb/locate.py`). Descris în 3.2. Ieșire: `work/adsb_position.geojson`.

**Map** (`adsb/make_map_html.py`). Hartă Leaflet autonomă cu traiectoriile aeronavelor și estimarea de poziție, arhivată ca `adsb/captures/captureN.html` (7 capturi existente).

### 3.2. Algoritmul de localizare

Modelul este log-distanță: RSSI_i = P0 − 10·n·log10(d_i), unde d_i este distanța 3D de la receptorul necunoscut (altitudine presupusă 100 m, teren tipic pentru câmpia Bucureștiului) la aeronava i. Aeronavele zboară tipic la peste 10 km altitudine, deci componenta verticală domină geometria; distanța 2D ar fi greșită.

Structura soluției: pentru orice poziție-candidat a receptorului, modelul este liniar în (P0, n) — o regresie a RSSI pe log10(d) le dă în formă închisă, împreună cu suma pătratelor reziduurilor (SSE). Optimizatorul exterior caută deci numai poziția 2D la sol:

1. **Căutare pe grilă coarse-to-fine** peste dreptunghiul acoperit de aeronave, extins cu ±100 km, în trei treceri cu rafinare (41×41, apoi 31×31 pe zone restrânse).
2. **Rafinare** cu `scipy.optimize.least_squares`, metoda `trf`, pierdere robustă `soft_l1`, care reduce influența RSSI-urilor aberante din propagare multiplă. Efect măsurat pe setul de 27 de aeronave: eroarea scade de la 2,90 km (L2 simplu) la 2,07 km.

Mecanisme de protecție:

- **Prag inferior de distanță** (implicit 1 500 m, opțiunea `--dist-floor`), aplicat înainte de log10(d). Fără el, o aeronavă aproape de verticala receptorului duce log10(d) spre −∞ și optimizarea plasează receptorul pe traiectoria semnalului celui mai puternic. Pragul a fost calibrat empiric pe captura de referință (70 000 de eșantioane, 27 de aeronave, 20 min): praguri ≤ 3 km dau eroarea minimă (1,98 km), 5 km dă 2,07 km, ≥ 8 km degradează la 4–5 km.
- **Control de acoperire în azimut:** din poziția estimată se calculează ocuparea celor 8 octante de direcție, cea mai mare lacună unghiulară și un indice de echilibru (ponderea eșantioanelor din semicercul cel mai gol). Soluția este marcată `geom_ok` numai dacă: octante ocupate ≥ 6, lacună maximă ≤ 150°, echilibru ≥ 0,15. Pragul de echilibru a fost calibrat pe capturi reale: două estimări cu geometrie unilaterală au ratat cu circa 8 km deși treceau vechiul test de octante; fixul bun de 1,74 km avea echilibru ≥ 0,3.

Programul raportează și o incertitudine 1σ din covarianța iacobianului, parametrii ajustați (P0, n, reziduu RMS) și eroarea față de punctul de adevăr GSM (44,4467 N, 26,0400 E), folosit doar la scorarea finală. Punctul de adevăr fiind el însuși estimarea GSM — aflată la circa 300 m de poziția reală (măsurătoare a operatorului, neînregistrată în proiect) — cifrele de eroare ADS-B poartă această incertitudine de bază: neglijabilă la 12–30 km, minoră la ~2 km.

### 3.3. Rezultate măsurate și limitări

Rezultate:

- Pe setul de referință (captura de 20 min, ~70 000 de eșantioane, 27 de aeronave): eroare de 1,7–2,1 km față de adevărul GSM, în funcție de configurație.
- Pe capturile curente, sărace în aeronave: precizie de ordinul 12–30 km. Metoda este limitată de date, nu de algoritm.
- **Compunerea capturilor:** agregarea datelor din mai multe capturi succesive a produs erori de poziție substanțial mai mici decât orice captură individuală, confirmând că limita este volumul de date, nu modelul. Rezultatul nu a fost însă adoptat ca soluție: îmbunătățirea a necesitat compunerea a cel puțin o oră și jumătate de recepție și presupune un receptor staționar pe toată durata — ipoteză care golește de sens scenariul de localizare vizat. Din acest motiv mecanismul nu a fost implementat în pipeline.

Limitări:

- **Dependența de date:** precizia crește cu numărul de aeronave și cu diversitatea lor în azimut și altitudine. Trafic rar sau concentrat pe o singură parte a cerului lasă soluția slab constrânsă (erori multi-kilometrice), chiar dacă incertitudinea formală 1σ pare mică — de aceea există controlul de echilibru.
- **Hardware:** recepția pe 1090 MHz — cu dongle-ul RTL2838 și antena generică nespecializată — este sever degradată, deși nenulă. S-a confirmat recepția reală a aeronavelor apropiate (~80 de pachete, verificate cu Flightradar24), dar debitul de mesaje valide este mic și adesea insuficient pentru perechile CPR par/impar necesare unui fix global.
- **Raportul mesaje utile / total:** pierderile sunt mari la fiecare nivel al lanțului. La nivelul demodulatorului, statisticile dump1090 ale unei rulări înregistrate arată 143 de mesaje acceptate cu CRC corect din 6 913 184 de preambuluri Mode-S detectate (≈ 1 la 48 000); restul au fost respinse pentru format invalid sau CRC greșit (4 406 342) ori pentru adresă ICAO nerecunoscută (2 506 699). Raportul este specific acelei capturi, dar ilustrează ordinul de mărime al pierderilor la demodulare. Dintre mesajele decodate, doar o mică parte produc poziții globale valide: 752 din 22 433 (~3,4 %) pe captura curentă, respectiv 2 081 din 57 881 (~3,6 %) pe captura de referință. La nivel de aeronavă, doar 12 din 26 de adrese ICAO recepționate (captura curentă), respectiv 18 din 31 (referință), au produs poziții utilizabile; restul au fost recepții incomplete (fără perechea CPR par/impar) sau fantome respinse de filtre.
- **Fantome de zgomot:** receptorul produce și adrese ICAO false sau poziții imposibile. Regulă de lucru: fiecare pereche ICAO–poziție se verifică față de un tracker în timp real (de exemplu FR24) înainte de a fi folosită. Poarta cinematică și controlul inter-aeronave din `filter.py` elimină automat o parte din aceste cazuri.
- **RSSI grosier:** nivelul Beast este un singur octet per cadru, necalibrat, atribuit per ICAO prin „cel mai recent văzut", deci cu o anumită imprecizie temporală.

### 3.4. Îmbunătățiri propuse

- Capturi mai lungi și programate în momente cu trafic pe direcțiile deficitare (coridoarele S/SV), pentru a umple octantele goale.
- Antenă acordată pe 1090 MHz; eventual un receptor cu front-end mai bun.
- Acumularea și combinarea mai multor capturi pentru diversitate de geometrie — testată și funcțională (vezi 3.3), dar viabilă doar pentru un receptor staționar pe durate lungi, motiv pentru care a rămas în afara pipeline-ului standard.

## 4. Comparație și concluzii

Comparația celor două lanțuri:

| Criteriu | GSM | ADS-B |
|---|---|---|
| Surse de semnal | turnuri fixe, dense, mereu active | aeronave în mișcare, trafic variabil |
| Dependență externă | baza de date OpenCellID | niciuna (pozițiile vin din mesaje) |
| Algoritm | centroid ponderat + control de reziduuri | regresie path-loss (P0, n) + grilă + least squares robust |
| Precizie măsurată | eroare reală constatată ~300 m; rază raportată ~842 m | 1,7–2,1 km cu date bune; 12–30 km pe capturile curente |
| Mod principal de eșec | bias spre centrul norului de turnuri | geometrie unilaterală a traficului |
| Limitare hardware | minoră (GSM900 se recepționează bine) | majoră (1090 MHz sever degradat) |

Ambele lanțuri folosesc RSSI necalibrat, dar îl tratează diferit: lanțul GSM îl folosește doar ca pondere relativă și evită conversia în distanțe absolute; lanțul ADS-B estimează parametrii de propagare (P0, n) simultan cu poziția, absorbind necalibrarea în regresie. Ambele își raportează explicit modul de eșec (biasul de centroid, respectiv indicatorii de geometrie), astfel încât o cifră de precizie aparent bună să poată fi verificată.

Concluzii. Auto-geolocalizarea pasivă, fără GPS, cu un receptor de câteva zeci de euro este realizabilă la precizie de sute de metri până la kilometri — suficientă pentru identificarea cartierului sau a orașului, nu pentru navigație. Lanțul GSM este cel matur și servește ca referință de adevăr; lanțul ADS-B funcționează cap-coadă, dar este limitat de volumul de date și de hardware. Dacă experimentul ar fi continuat, direcțiile cu cel mai mare impact ar fi, în ordine: capturi ADS-B mai bogate (mai multe aeronave, direcții diverse), antenă/receptor mai bun pe 1090 MHz, extrasul CSV OpenCellID pentru funcționare offline a lanțului GSM, calibrarea RSSI și, ulterior, fuziunea celor două estimări independente.
